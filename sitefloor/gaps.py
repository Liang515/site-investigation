"""缺口盤點：依建物類型統計哪些建物還沒有樓層，列出樓層未知的地標，供日後補資料。

類型由 OSM 標籤判斷（百貨／購物中心、量販／賣場、運動場館、交通、醫院、學校、公家機關…）。
之後接上新的樓層來源（例：人工整理的百貨樓層表），重跑這支就能看到每一類補了多少、還缺多少。

輸出：
- data/reports/gaps.md               各類型的樓層有無統計
- data/reports/missing_landmarks.csv 有名稱、樓層未知的地標類建物（OSM 資料，ODbL）
用法：uv run python -m sitefloor.gaps   （osm 之後；也會在每週更新時自動跑）
"""

import re
from pathlib import Path

import pandas as pd
import pyogrio

from sitefloor.db import connect

ROOT = Path(__file__).resolve().parent.parent
PBF = ROOT / "data" / "raw" / "osm" / "taiwan-latest.osm.pbf"
REPORT_DIR = ROOT / "data" / "reports"
LANDMARK_CATS = ["百貨／購物中心", "飯店", "樂園", "電影院", "展覽館", "博物館／美術館", "量販／賣場", "運動場館",
                 "交通（車站／航廈）", "醫院", "公家機關"]

MALL_NAME = re.compile("百貨|購物|商場|廣場|outlet|Outlet|三越|SOGO|遠百|微風|京站|環球|大魯閣|高島屋")
STORE_NAME = re.compile("家樂福|好市多|Costco|COSTCO|大潤發|IKEA|宜家|愛買|特力屋|全聯")
HOTEL_NAME = re.compile("飯店|酒店|[Hh]otel|HOTEL")
PARK_NAME = re.compile("樂園|遊樂園|主題園區")
CINEMA_NAME = re.compile("影城|電影院|戲院|[Cc]inema")
EXPO_NAME = re.compile("展覽館|展覽中心|會展中心|世貿|國際會議中心")
MUSEUM_NAME = re.compile("博物館|博物院|美術館|紀念館|科學館|藝術館")


def _tag(tags: pd.Series, k: str) -> pd.Series:
    return tags.str.extract(r'"' + re.escape(k) + r'"=>"([^"]*)"')[0]


def category(name, building, shop, amenity, leisure, aeroway, railway, tourism=None) -> str:
    b, s, a, n, t = str(building), str(shop), str(amenity), str(name or ""), str(tourism)
    if s in ("mall", "department_store") or (b == "retail" and MALL_NAME.search(n)):
        return "百貨／購物中心"
    if b == "hotel" or t == "hotel" or HOTEL_NAME.search(n):
        return "飯店"
    if t == "theme_park" or str(leisure) == "water_park" or PARK_NAME.search(n):
        return "樂園"
    if a == "cinema" or CINEMA_NAME.search(n):
        return "電影院"
    if a in ("exhibition_centre", "conference_centre") or EXPO_NAME.search(n):
        return "展覽館"
    if b == "museum" or t in ("museum", "gallery") or MUSEUM_NAME.search(n):
        return "博物館／美術館"
    if s in ("supermarket", "wholesale", "doityourself", "furniture", "hardware") or STORE_NAME.search(n):
        return "量販／賣場"
    if b in ("retail", "commercial", "supermarket", "kiosk") or s not in ("None", "nan"):
        return "其他商業"
    if b in ("stadium", "sports_hall", "sports_centre", "grandstand") or str(leisure) in ("stadium", "sports_centre"):
        return "運動場館"
    if b in ("transportation", "train_station") or str(aeroway) == "terminal" or str(railway) == "station":
        return "交通（車站／航廈）"
    if b == "hospital" or a in ("hospital", "clinic"):
        return "醫院"
    if b in ("school", "university", "college", "kindergarten") or a in ("school", "university", "college", "kindergarten"):
        return "學校"
    if b in ("public", "government", "civic", "townhall", "fire_station", "police") or a in (
        "townhall", "police", "fire_station", "courthouse", "library"):
        return "公家機關"
    if b in ("temple", "church", "shrine", "religious", "mosque") or a == "place_of_worship":
        return "宗教"
    if b in ("industrial", "warehouse", "factory", "manufacture", "hangar"):
        return "工廠／倉庫"
    if b in ("house", "residential", "apartments", "detached", "dormitory", "terrace", "semidetached_house"):
        return "住宅"
    if b in ("roof", "carport", "parking", "garage", "garages", "shed", "hut", "construction", "ruins", "greenhouse"):
        return "附屬／臨時構造"
    return "其他／未分類"


def read_osm_tags() -> pd.DataFrame:
    """OSM 建物的分類用標籤：uid, name, building, wikidata, wikipedia, cat（建物類型）。tiers.py 也用。"""
    d = pyogrio.read_dataframe(
        PBF, layer="multipolygons", read_geometry=False,
        columns=["osm_id", "osm_way_id", "name", "building", "leisure", "amenity", "shop", "other_tags"],
        where="building IS NOT NULL OR leisure = 'stadium'",
    )
    d["uid"] = "osm:" + d.osm_way_id.fillna("r" + d.osm_id.astype(str)).astype(str)
    tags = d.other_tags.fillna("")
    d["aeroway"], d["railway"], d["tourism"] = _tag(tags, "aeroway"), _tag(tags, "railway"), _tag(tags, "tourism")
    d["wikidata"], d["wikipedia"] = _tag(tags, "wikidata"), _tag(tags, "wikipedia")
    d["cat"] = [category(*x) for x in zip(d.name, d.building, d.shop, d.amenity, d.leisure, d.aeroway, d.railway,
                                          d.tourism)]
    return d[["uid", "name", "building", "wikidata", "wikipedia", "cat"]].drop_duplicates("uid")


def main():
    d = read_osm_tags()
    ob = connect().execute("SELECT uid, coalesce(curated_floors, trade_floors, permit_floors, levels) AS floors FROM osm_buildings").df()
    d = d.merge(ob, on="uid", how="inner")  # osm.py 收的建物（已排除地下構造）
    d["known"] = d.floors.notna()

    r = d.groupby("cat").agg(總數=("uid", "size"), 有樓層=("known", "sum"),
                             有名稱=("name", lambda x: x.notna().sum()), 有維基數據=("wikidata", lambda x: x.notna().sum()))
    r["樓層未知"] = r.總數 - r.有樓層
    r["未知比例"] = (r.樓層未知 / r.總數 * 100).round().astype(int).astype(str) + "%"
    r = r.sort_values("總數", ascending=False)[["總數", "有樓層", "樓層未知", "未知比例", "有名稱", "有維基數據"]]

    miss = d[~d.known & d.name.notna() & d.cat.isin(LANDMARK_CATS)]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    miss[["uid", "name", "cat", "building", "wikidata"]].sort_values(["cat", "name"]).to_csv(
        REPORT_DIR / "missing_landmarks.csv", index=False, encoding="utf-8-sig")
    lines = ["# 樓層缺口盤點", "", "## 各類型的樓層有無（OSM 建物）", "",
             "| 類型 | 總數 | 有樓層 | 樓層未知 | 未知比例 | 有名稱 | 有維基數據 |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {k} | {v.總數:,} | {v.有樓層:,} | {v.樓層未知:,} | {v.未知比例} | {v.有名稱:,} | {v.有維基數據:,} |"
              for k, v in r.iterrows()]
    lines += ["", f"## 有名稱、樓層未知的地標類：{len(miss):,} 棟（清單：missing_landmarks.csv）", ""]
    for k in LANDMARK_CATS:
        g = miss[miss.cat == k]
        lines.append(f"- {k}：{len(g):,}（例：{'、'.join(g.name.head(6))}）")
    (REPORT_DIR / "gaps.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
