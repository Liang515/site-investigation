"""建庫：各縣市建物圖＋門牌點位 → data/db/sitefloor.duckdb

用法：uv run python -m sitefloor.build taichung taipei
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


BUILDERS = {"taichung": build_taichung, "taipei": build_taipei}
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
