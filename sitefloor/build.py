"""建庫：各縣市建物圖＋門牌點位 → data/db/sitefloor.duckdb

用法：uv run python -m sitefloor.build taichung taipei
      uv run python -m sitefloor.build osm_addr   （基隆、宜蘭、南投、嘉義市、連江：用 OSM 地址點當門牌座標）
      uv run python -m sitefloor.build road_towns （嘉義市：用 OSM 道路判斷每條路在哪一區，補實價登錄缺的區）
"""
import json
import sys
import time
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
import pyogrio

from sitefloor.address import from_parts
from sitefloor.db import DB_PATH, connect
from sitefloor.trades import norm_structure

TOWN_SHP = "data/raw/town/TOWN_MOI_1140318.shp"
NEAREST_MAX_M = 10

TC_TYPE = {"1": "獨戶", "2": "雙併", "3": "連棟透天", "4": "連棟公寓", "5": "獨棟公寓", "6": "大廈"}
TC_STRUCT = {"1": "木造", "2": "磚造", "3": "鐵架搭建", "4": "RC/鋼骨", "5": "其他"}


def _towns() -> gpd.GeoDataFrame:
    t = pyogrio.read_dataframe(TOWN_SHP, columns=["COUNTYNAME", "TOWNNAME"])
    return t.to_crs(3826)


def _addr_points(df: pd.DataFrame, x: str, y: str, county: str, street_col: str, crs: int = 3826) -> gpd.GeoDataFrame:
    g = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(pd.to_numeric(df[x], errors="coerce"), pd.to_numeric(df[y], errors="coerce")), crs=crs
    ).to_crs(3826)
    towns = _towns()
    g = gpd.sjoin(g, towns[towns.COUNTYNAME == county], how="left", predicate="within")
    g = g[~g.index.duplicated()].drop(columns="index_right")
    parsed = [
        from_parts(county, t if isinstance(t, str) else "", s, a, l, al, n)
        for t, s, a, l, al, n in zip(g.TOWNNAME, g[street_col], g["地區"], g["巷"], g["弄"], g["號"])
    ]
    g["county"] = county
    g["town"] = g.TOWNNAME
    g["base"] = [p.base for p in parsed]
    g["full"] = [p.full for p in parsed]
    g["floor"] = pd.array([p.floor for p in parsed], dtype="Int16")
    return g[["county", "town", "base", "full", "floor", "geometry"]]


def _match(addr: gpd.GeoDataFrame, bldg: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    b = bldg[["uid", "geometry"]]
    inside = gpd.sjoin(addr, b, how="left", predicate="within")
    inside = inside[~inside.index.duplicated()]
    addr["bldg_uid"] = inside["uid"]
    addr["match"] = pd.Series("within", index=addr.index).where(addr.bldg_uid.notna())
    addr["dist_m"] = 0.0
    miss = addr[addr.bldg_uid.isna()]
    near = gpd.sjoin_nearest(miss[["geometry"]], b, how="left", max_distance=NEAREST_MAX_M, distance_col="d")
    near = near[~near.index.duplicated()]
    ok = near.uid.notna()
    addr.loc[near.index[ok], "bldg_uid"] = near.loc[ok, "uid"]
    addr.loc[near.index[ok], "match"] = "nearest"
    addr.loc[near.index[ok], "dist_m"] = near.loc[ok, "d"].round(1)
    addr.loc[addr.bldg_uid.isna(), "dist_m"] = None
    return addr


def _write(con: duckdb.DuckDBPyConnection, county: str, bldg: gpd.GeoDataFrame, addr: gpd.GeoDataFrame):
    bw = bldg.to_crs(4326)
    bounds = bw.geometry.bounds
    cen = bw.geometry.representative_point()
    bdf = pd.DataFrame({
        "uid": bw.uid, "county": county, "source": bw.source,
        "floors_above": bw.floors_above, "floors_below": bw.floors_below,
        "structure": bw.structure, "btype": bw.btype, "year": bw.year, "year_kind": bw.year_kind,
        "lon": cen.x, "lat": cen.y,
        "minlon": bounds.minx, "minlat": bounds.miny, "maxlon": bounds.maxx, "maxlat": bounds.maxy,
        "wkb": bw.geometry.to_wkb(),
        "permit_no": bw.get("permit_no"),
    })
    aw = addr.to_crs(4326)
    adf = pd.DataFrame({
        "county": aw.county, "town": aw.town, "base": aw.base, "full": aw.full, "floor": aw.floor,
        "lon": aw.geometry.x, "lat": aw.geometry.y,
        "bldg_uid": aw.bldg_uid, "match": aw["match"], "dist_m": aw.dist_m,
    })
    con.execute("DELETE FROM buildings WHERE county = ?", [county])
    con.execute("DELETE FROM addresses WHERE county = ?", [county])
    con.execute("INSERT INTO buildings SELECT * FROM bdf")
    con.execute("INSERT INTO addresses SELECT * FROM adf")
    print(f"  寫入 建物 {len(bdf):,}、門牌 {len(adf):,}（對到建物 {adf.bldg_uid.notna().mean():.1%}）")


def build_taichung(con):
    b = pyogrio.read_dataframe("data/raw/tc_gis/01-SHP/114期末_B0001_1216n.shp", columns=["ID", "B1B", "B1C", "B1D", "B1E"])
    b.geometry = b.geometry.force_2d()
    b["uid"] = "tc:" + b.ID.astype(str)
    b["source"] = "臺中市建物圖114"
    b["floors_above"] = b.B1D.where(b.B1D > 0).astype("Int16")
    b["floors_below"] = pd.array([None] * len(b), dtype="Int16")
    b["structure"] = b.B1C.map(TC_STRUCT)
    b["btype"] = b.B1B.map(TC_TYPE)
    b["year"] = (b.B1E.where(b.B1E > 0) + 1911).astype("Int16")
    b["year_kind"] = "圖資更新年"
    a = pd.read_csv("data/raw/tc_gis/addr11509.csv", dtype=str)
    a = _addr_points(a, "TWD97橫坐標", "TWD97縱坐標", "臺中市", "街、路段")
    _write(con, "臺中市", b, _match(a, b))


def taipei_permit_no(s: pd.Series) -> pd.Series:
    """套繪圖執照號 → 建照號（「107.0073.21F」「107.0073」都是 107 年第 0073 號）。"""
    return s.str.extract(r"^(\d+\.\d+)")[0]


def build_taipei(con):
    b = pyogrio.read_dataframe("data/raw/tp_gis/taipei_1151005.shp")
    b = b[b.bud_floor > 0].reset_index(drop=True)
    b["uid"] = "tp:" + b.index.astype(str)
    b["source"] = "臺北市建照套繪圖"
    b["floors_above"] = b.bud_floor.astype("Int16")
    b["floors_below"] = pd.array([None] * len(b), dtype="Int16")
    b["structure"] = b.bud_const.map(norm_structure)
    b["btype"] = None
    # 執照字號開頭為民國年（"90." 或 "101."）
    b["year"] = (pd.to_numeric(b.budatt_no.str.extract(r"^(\d+)")[0], errors="coerce") + 1911).astype("Int16")
    b["year_kind"] = "建照年"
    b["permit_no"] = taipei_permit_no(b.budatt_no)
    a = pd.read_csv("data/raw/tp_gis/addr.csv", dtype=str)
    a = _addr_points(a, "橫座標", "縱座標", "臺北市", "街路段")
    _write(con, "臺北市", b, _match(a, b))


# ── 只有門牌點位、沒有建物圖的縣市 ──────────────────────────────
ADDR_DIR = Path("data/raw/addr")
_ALIASES = {
    "street": ["街、路段", "街路段", "街_路段", "街或路段", "street、road、section", "streetRoadSection"],
    "street1": ["街"], "street2": ["路段"],
    "地區": ["地區", "area"], "巷": ["巷", "lane"], "弄": ["弄", "alley"],
    "號": ["號", "號樓", "number", "houseNumber"],
    "x": ["橫座標", "橫坐標", "x_3826", "coordinateX", "橫座標[E 坐標]"],
    "y": ["縱座標", "縱坐標", "y_3826", "coordinateY", "縱座標[N 坐標]"],
    "full": ["地址"],
}


def _read_any(p: Path) -> pd.DataFrame:
    if p.suffix == ".xlsx":
        return pd.read_excel(p, dtype=str)
    if p.suffix == ".json":
        return pd.DataFrame(json.loads(p.read_text(encoding="utf-8-sig")))
    for enc in ("utf-8-sig", "big5hkscs"):
        try:
            return pd.read_csv(p, dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    # Big5 檔偶有罕用字（造字區）解不開：換成替代字元，不影響門牌號
    return pd.read_csv(p, dtype=str, encoding="cp950", encoding_errors="replace")


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip().lstrip("\ufeff") for c in df.columns]
    out = pd.DataFrame(index=df.index)
    for std, names in _ALIASES.items():
        col = next((n for n in names if n in df.columns), None)
        out[std] = df[col] if col else None
    if out.street.isna().all() and out.street1.notna().any():
        out["street"] = out.street1.fillna("") + out.street2.fillna("")
    if out.street.isna().all() and out.full.notna().any():
        # 只有整串地址（新竹市）：整串放在 street，其餘欄空白
        out["street"] = out.full
    # 新竹縣原始檔把「號巷弄鄰」轉成「I- G- H- D-」（例：１５０G-、３６G-１I-２樓）
    garbled = {"I": "號", "G": "巷", "H": "弄", "D": "鄰"}
    for c in ("street", "地區", "巷", "弄", "號"):
        col = out[c].astype("string")
        out[c] = col.str.replace(r"(?<=[0-9０-９])([IGHD])-", lambda m: garbled[m.group(1)], regex=True)
    return out[["street", "地區", "巷", "弄", "號", "x", "y"]]


def _guess_crs(df: pd.DataFrame, county: str) -> int:
    """離島（澎湖、金門）可能用 TM2 119 分帶，取樣看哪個分帶落在縣界內。"""
    towns = _towns()
    poly = towns[towns.COUNTYNAME == county].union_all()
    s = df.dropna(subset=["x", "y"]).sample(min(500, len(df)), random_state=0)
    best = max((3826, 3825), key=lambda c: gpd.GeoSeries(
        gpd.points_from_xy(pd.to_numeric(s.x, errors="coerce"), pd.to_numeric(s.y, errors="coerce")), crs=c
    ).to_crs(3826).within(poly).mean())
    return best


def build_addr_only(con, county: str):
    files = sorted(p for p in (ADDR_DIR / county).iterdir() if p.suffix in (".csv", ".xlsx", ".json"))
    df = pd.concat([_standardize(_read_any(p)) for p in files], ignore_index=True)
    n0 = len(df)
    df = df.dropna(subset=["x", "y"]).drop_duplicates()
    crs = _guess_crs(df, county)
    a = _addr_points(df, "x", "y", county, "street", crs=crs)
    a = a[a.town.notna()]  # 落在縣界外的（座標錯誤）丟掉
    a = a.drop_duplicates(subset=["town", "full"])
    print(f"  {len(files)} 檔、原始 {n0:,} 列、EPSG:{crs}、去重後 {len(a):,}")
    a["bldg_uid"], a["match"], a["dist_m"] = None, None, None
    empty = gpd.GeoDataFrame(columns=["uid", "source", "floors_above", "floors_below", "structure",
                                      "btype", "year", "year_kind"], geometry=[], crs=3826)
    _write(con, county, empty, a)


# ── 沒有開放門牌點位的縣市：改用 OpenStreetMap 的地址點（addr:full；基隆市幾乎是完整匯入） ──
OSM_PBF = Path("data/raw/osm/taiwan-latest.osm.pbf")
OSM_ADDR_COUNTIES = ["基隆市", "宜蘭縣", "南投縣", "嘉義市", "連江縣"]


def build_osm_addr(con):
    import re

    where = " OR ".join(f"other_tags LIKE '%\"addr:city\"=>\"{c}\"%'" for c in OSM_ADDR_COUNTIES)
    parts = []
    for layer in ("points", "multipolygons"):
        g = pyogrio.read_dataframe(OSM_PBF, layer=layer, columns=["other_tags"], where=where)
        if layer == "multipolygons":
            g["geometry"] = g.geometry.representative_point()
        parts.append(g)
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
    tags = g.other_tags.fillna("")

    def tag(k):
        return tags.str.extract(r'"' + re.escape(k) + r'"=>"([^"]*)"')[0]

    city, full = tag("addr:city"), tag("addr:full")
    # 沒有 addr:full 的用 縣市＋區＋路＋號 組起來
    full = full.fillna(city.fillna("") + tag("addr:district").fillna("") + tag("addr:street").fillna("")
                       + tag("addr:housenumber").fillna(""))
    rows = []
    for c, text, geom in zip(city, full, g.geometry):
        a = from_parts(c, "", text, "", "", "", "") if isinstance(text, str) else None
        if a and a.base.endswith("號") and a.town and a.county == c:
            rows.append((c, a.town, a.base, a.full, a.floor, geom))
    a = gpd.GeoDataFrame(pd.DataFrame(rows, columns=["county", "town", "base", "full", "floor", "geometry"]),
                         geometry="geometry", crs=4326).to_crs(3826)
    a = a.drop_duplicates(subset=["county", "town", "full"])
    a["floor"] = pd.array(a["floor"], dtype="Int16")
    a["bldg_uid"], a["match"], a["dist_m"] = None, None, None
    empty = gpd.GeoDataFrame(columns=["uid", "source", "floors_above", "floors_below", "structure",
                                      "btype", "year", "year_kind"], geometry=[], crs=3826)
    for c in OSM_ADDR_COUNTIES:
        sub = a[a.county == c]
        print(f"  {c}：OSM 地址點 {len(sub):,}（門牌號 {sub.base.nunique():,}）")
        if len(sub):
            _write(con, c, empty, sub)


# ── 實價登錄沒寫區、又沒有門牌點位可對的縣市（嘉義市）：用 OSM 道路線與行政區界判斷每條路在哪一區 ──
ROAD_TOWN_COUNTIES = ["嘉義市"]
ROAD_TOWN_SHARE = 0.9  # 道路長度九成以上在同一區才採用


def build_road_towns(con):
    towns = _towns()
    rows = []
    for county in ROAD_TOWN_COUNTIES:
        t = towns[towns.COUNTYNAME == county]
        x0, y0, x1, y1 = t.to_crs(4326).total_bounds
        roads = pyogrio.read_dataframe(OSM_PBF, layer="lines", columns=["name", "highway"], bbox=(x0, y0, x1, y1),
                                       where="highway IS NOT NULL AND name IS NOT NULL").to_crs(3826)
        seg = gpd.overlay(roads[["name", "geometry"]], t[["TOWNNAME", "geometry"]], how="intersection",
                          keep_geom_type=True)
        seg["len"] = seg.geometry.length
        g = seg.groupby(["name", "TOWNNAME"]).len.sum().reset_index()
        g["share"] = g.len / g.groupby("name").len.transform("sum")
        ok = g[g.share >= ROAD_TOWN_SHARE]
        rows += [(county, r.name, r.TOWNNAME, round(r.share, 3)) for r in ok.itertuples()]
        print(f"  {county}：道路 {g.name.nunique():,} 條，可判斷區別 {len(ok):,} 條")
    df = pd.DataFrame(rows, columns=["county", "road", "town", "share"])
    con.execute("CREATE OR REPLACE TABLE road_towns AS SELECT * FROM df")


BUILDERS = {"taichung": build_taichung, "taipei": build_taipei, "osm_addr": build_osm_addr,
            "road_towns": build_road_towns}
for _c in ["新北市", "桃園市", "臺南市", "高雄市", "新竹市", "新竹縣", "苗栗縣", "彰化縣",
           "雲林縣", "嘉義縣", "屏東縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣"]:
    BUILDERS[_c] = lambda con, c=_c: build_addr_only(con, c)

if __name__ == "__main__":
    con = connect(write=True)
    for name in sys.argv[1:] or BUILDERS:
        t = time.time()
        print(f"[{name}]")
        BUILDERS[name](con)
        print(f"  {time.time() - t:.0f}s")
    con.close()
    print(f"→ {DB_PATH}")
