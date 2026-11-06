"""建物分級 → DuckDB building_tiers 表：越容易被查的建物越優先確保正確（audit.py 依級別排檢查頻率）。

| 級 | 條件（符合任一） |
|---|---|
| 1 地標／指標 | 有立體模型或在地標表；OSM 有 wikidata／wikipedia 且為地標類別或 MID_FLOORS 層以上；TALL_FLOORS 層以上或 TALL_M 公尺以上；
|               | 有名稱且 NAMED_FLOORS 層以上；實價單價在該縣市前 PRICE_TOP（PRICE_MIN_FLOORS 層以上、有 PRICE_MIN_TRADES 筆以上買賣） |
| 2 公共與場館 | 公家機關、運動場館、醫院、交通、學校、百貨／購物中心（gaps.category）；其他有維基連結的 |
| 3 有名稱或中高樓 | 其他有名稱的建物，或 MID_FLOORS 層以上 |
| 4 一般 | 其他 |

一列＝一棟：OSM 建物（osm:…）＋臺北建照套繪圖、臺中建物圖外框（tp:…、tc:…）。
縣市、鄉鎮：棟內門牌的縣市；沒有門牌的用外框中心落在哪個鄉鎮（內政部鄉鎮界）。
欄位：uid, name, county, town, tier, reasons, floors, height, cat, n_doors, unit_price
用法：uv run python -m sitefloor.tiers   （osm、permits 之後；每週更新會自動跑）
"""

import time

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

from sitefloor import landmarks
from sitefloor.address import parse
from sitefloor.db import connect
from sitefloor.gaps import read_osm_tags
from sitefloor.models import MODELS

TOWN_SHP = "data/raw/town/TOWN_MOI_1140318.shp"
TALL_FLOORS, TALL_M = 25, 100
NAMED_FLOORS = 15
MID_FLOORS = 8
PRICE_TOP, PRICE_MIN_TRADES, PRICE_MIN_FLOORS = 0.01, 3, 6  # 高單價只算大樓（店面單價高但不是指標）
TIER2_CATS = {"公家機關", "運動場館", "醫院", "交通（車站／航廈）", "學校", "百貨／購物中心"}
WIKI_CATS = TIER2_CATS - {"學校"}  # 有維基連結又是這些類別（或 MID_FLOORS 層以上）才算第 1 級；其他有維基的算第 2 級


def assign_tier(r: dict) -> tuple[int, list[str]]:
    """一棟建物的級別與理由。r 需要：model, landmark, wiki, floors, height, name, cat, top_price。"""
    why1 = [label for label, hit in (
        ("立體模型", r.get("model")),
        ("地標", r.get("landmark")),
        ("維基", r.get("wiki") and (r.get("cat") in WIKI_CATS or (r.get("floors") or 0) >= MID_FLOORS)),
        (f"{TALL_FLOORS} 層以上", (r.get("floors") or 0) >= TALL_FLOORS),
        (f"{TALL_M} m 以上", (r.get("height") or 0) >= TALL_M),
        (f"有名稱且 {NAMED_FLOORS} 層以上", bool(r.get("name")) and (r.get("floors") or 0) >= NAMED_FLOORS),
        ("高單價", r.get("top_price") and (r.get("floors") or 0) >= PRICE_MIN_FLOORS),
    ) if hit]
    if why1:
        return 1, why1
    if r.get("cat") in TIER2_CATS or r.get("wiki"):
        return 2, [r["cat"] if r.get("cat") in TIER2_CATS else "維基"]
    why3 = [label for label, hit in (("有名稱", bool(r.get("name"))),
                                     (f"{MID_FLOORS} 層以上", (r.get("floors") or 0) >= MID_FLOORS)) if hit]
    if why3:
        return 3, why3
    return 4, []


def _landmark_osm(con) -> set[str]:
    """地標表的門牌落在哪些 OSM 建物。"""
    keys = []
    for addr, _ in landmarks.LANDMARKS:
        a = parse(addr)
        keys.append((a.county, a.town or "", a.base))
    con.register("lm", pd.DataFrame(keys, columns=["county", "town", "base"]))
    out = {u for (u,) in con.execute(
        "SELECT DISTINCT o.osm_uid FROM lm JOIN door_osm o USING (county, town, base)").fetchall()}
    con.unregister("lm")
    return out


def _town_of(points: gpd.GeoDataFrame) -> pd.DataFrame:
    """點 → 縣市、鄉鎮（落在哪個鄉鎮界）。"""
    t = pyogrio.read_dataframe(TOWN_SHP, columns=["COUNTYNAME", "TOWNNAME"]).to_crs(4326)
    j = gpd.sjoin(points, t, predicate="within", how="left")
    j = j[~j.index.duplicated()]
    return pd.DataFrame({"county": j.COUNTYNAME, "town": j.TOWNNAME}, index=j.index)


def main():
    t0 = time.time()
    con = connect(write=True)
    tags = read_osm_tags()
    osm = con.execute("""
        SELECT uid, coalesce(curated_floors, trade_floors, permit_floors, levels) AS floors,
               coalesce(curated_height, height) AS height, (minlon + maxlon) / 2 AS lon, (minlat + maxlat) / 2 AS lat
        FROM osm_buildings""").df()
    osm = osm.merge(tags.drop(columns="building"), on="uid", how="left")
    # 棟內門牌：縣市鄉鎮、門牌數、買賣單價中位數（實價買賣 PRICE_MIN_TRADES 筆以上才算）
    doors = con.execute(f"""
        SELECT o.osm_uid AS uid, any_value(o.county) AS county, any_value(o.town) AS town,
               count(DISTINCT o.base) AS n_doors,
               CASE WHEN count(t.unit_price) >= {PRICE_MIN_TRADES} THEN median(t.unit_price) END AS unit_price
        FROM door_osm o
        LEFT JOIN trades t ON t.county = o.county AND t.town = o.town AND t.base = o.base AND t.kind = '買賣'
        GROUP BY 1""").df()
    osm = osm.merge(doors, on="uid", how="left")
    city = con.execute("""
        SELECT b.uid, b.county, NULL AS name, b.floors_above AS floors, NULL::DOUBLE AS height,
               (b.minlon + b.maxlon) / 2 AS lon, (b.minlat + b.maxlat) / 2 AS lat,
               count(DISTINCT a.base) AS n_doors, any_value(a.town) AS town
        FROM buildings b LEFT JOIN addresses a ON a.bldg_uid = b.uid GROUP BY ALL""").df()
    city["n_doors"] = city.n_doors.astype("Int64")
    d = pd.concat([osm, city], ignore_index=True)
    # 沒有門牌的：外框中心落在哪個鄉鎮
    miss = d.county.isna() | d.town.isna()
    if miss.any():
        pts = gpd.GeoDataFrame(index=d.index[miss], geometry=shapely.points(d.lon[miss], d.lat[miss]), crs=4326)
        loc = _town_of(pts)
        d.loc[miss, "county"] = d.county[miss].fillna(loc.county)
        d.loc[miss, "town"] = d.town[miss].fillna(loc.town)
    # 高單價：各縣市有單價的建物中前 PRICE_TOP
    cut = d.dropna(subset=["unit_price"]).groupby("county").unit_price.quantile(1 - PRICE_TOP)
    d["top_price"] = d.unit_price >= d.county.map(cut)
    lm = _landmark_osm(con)
    rows = []
    for r in d.to_dict("records"):
        r = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in r.items()}
        r.update(model=r["uid"] in MODELS, landmark=r["uid"] in lm, wiki=bool(r.get("wikidata") or r.get("wikipedia")),
                 top_price=bool(r.get("top_price")))
        tier, why = assign_tier(r)
        rows.append({"uid": r["uid"], "name": r.get("name"), "county": r.get("county"), "town": r.get("town"),
                     "tier": tier, "reasons": "、".join(why), "floors": r.get("floors"), "height": r.get("height"),
                     "cat": r.get("cat"), "n_doors": r.get("n_doors"), "unit_price": r.get("unit_price")})
    out = pd.DataFrame(rows).astype({"tier": "Int8", "floors": "Int16", "n_doors": "Int32"})
    con.register("tiers_df", out)
    con.execute("CREATE OR REPLACE TABLE building_tiers AS SELECT * FROM tiers_df")
    con.unregister("tiers_df")
    print(con.sql("SELECT tier AS 級, count(*) AS 棟數, count(floors) AS 有樓層 FROM building_tiers GROUP BY 1 ORDER BY 1"))
    print(f"  （{time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
