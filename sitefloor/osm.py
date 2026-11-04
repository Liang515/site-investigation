"""OpenStreetMap 建物 → DuckDB（osm_buildings、door_osm）。

- osm_buildings：OSM 建物外框。levels＝OSM 標註的地上樓層（building:levels）；height＝OSM 標註高度（公尺）；
  trade_floors＝落在這棟裡的門牌的實價總樓層眾數。三者都沒有的建物不存。
  height 換算樓層只有 30% 相符（樓高差異大），所以只拿來畫地圖，不當樓層。
- door_osm：門牌號（county, town, base）落在哪棟 OSM 建物裡。
- same_osm：OSM 同棟推估。同一 OSM 外框內有實價的門牌的總樓層眾數（n＝有實價的門牌數、spread＝最大減最小、
  btype＝建物型態眾數）。留一驗證（2026-11-04，17.9 萬門牌）：spread=0 時完全相符 97.0%、差一層內 98.6%；
  spread>0 時 62.4%。查詢只用 spread=0 的（見 lookup.py）。

OSM building:levels 的準確度（2026-11-04，6.4 萬個有實價的門牌）：完全相符 71.5%、差一層內 89%。
查詢時當信心「低」的備援，排在同巷推估之後、建物圖之前（見 lookup.py）。

來源：Geofabrik taiwan-latest.osm.pbf（© OpenStreetMap 貢獻者，ODbL）
用法：uv run python -m sitefloor.osm   （放在 derive 之後、coverage 之前；要先停掉 server）
"""

import re
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

from sitefloor.db import connect

PBF = Path("data/raw/osm/taiwan-latest.osm.pbf")


def _tag(s: str, k: str) -> str:
    m = re.search(r'"' + re.escape(k) + r'"=>"([^"]*)"', s or "")
    return m[1] if m else ""


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.extract(r"^\s*(\d+(?:\.\d+)?)")[0], errors="coerce")


def read_buildings() -> gpd.GeoDataFrame:
    df = pyogrio.read_dataframe(
        PBF,
        layer="multipolygons",
        columns=["osm_id", "osm_way_id", "name", "building", "other_tags"],
        where="building IS NOT NULL",
    )
    tags = df.other_tags.fillna("")
    levels = _num(tags.map(lambda s: _tag(s, "building:levels")))
    height = _num(tags.map(lambda s: _tag(s, "height")))
    # 不合理的值當作沒標
    df["levels"] = levels.where((levels >= 1) & (levels <= 110)).round().astype("Int16")
    df["height"] = height.where((height >= 2) & (height <= 600))
    df["uid"] = "osm:" + df.osm_way_id.fillna("r" + df.osm_id.astype(str)).astype(str)
    return df[["uid", "name", "building", "levels", "height", "geometry"]].set_crs(4326, allow_override=True)


def main():
    t = time.time()
    con = connect(write=True)
    b = read_buildings()
    print(f"  OSM 建物 {len(b):,}，有樓層 {b.levels.notna().sum():,}（{time.time() - t:.0f}s）")

    doors = con.execute("""
        SELECT county, town, base, any_value(lon) AS lon, any_value(lat) AS lat
        FROM addresses WHERE lon IS NOT NULL AND town IS NOT NULL GROUP BY 1, 2, 3""").df()
    pts = gpd.GeoDataFrame(doors, geometry=gpd.points_from_xy(doors.lon, doors.lat), crs=4326)
    j = gpd.sjoin(pts, b[["uid", "geometry"]], predicate="within")
    j = j[~j.index.duplicated()]
    door_osm = pd.DataFrame({"county": j.county, "town": j.town, "base": j.base, "osm_uid": j.uid})
    con.execute("CREATE OR REPLACE TABLE door_osm AS SELECT * FROM door_osm")
    print(f"  門牌落在 OSM 建物內 {len(door_osm):,} / {len(doors):,}")

    tf = con.execute("""
        SELECT o.osm_uid AS uid, mode(t.floors) AS trade_floors
        FROM door_osm o JOIN door_trade t USING (county, town, base) GROUP BY 1""").df()
    b = b.merge(tf, on="uid", how="left")
    b = b[b.levels.notna() | b.trade_floors.notna() | b.height.notna()]
    bounds = b.geometry.bounds
    odf = pd.DataFrame(
        {
            "uid": b.uid,
            "name": b.name,
            "building": b.building,
            "levels": b.levels,
            "height": b.height,
            "trade_floors": b.trade_floors.astype("Int16"),
            "minlon": bounds.minx,
            "minlat": bounds.miny,
            "maxlon": bounds.maxx,
            "maxlat": bounds.maxy,
            "wkb": b.geometry.to_wkb(),
        }
    )
    con.execute("CREATE OR REPLACE TABLE osm_buildings AS SELECT * FROM odf")
    con.execute("DELETE FROM door_osm WHERE osm_uid NOT IN (SELECT uid FROM osm_buildings)")
    print(
        f"  寫入 osm_buildings {len(odf):,}（OSM 樓層 {odf.levels.notna().sum():,}、"
        f"有實價樓層 {odf.trade_floors.notna().sum():,}）（{time.time() - t:.0f}s）"
    )

    con.execute("""
        CREATE OR REPLACE TABLE same_osm AS
        SELECT o.county, o.town, o.base, g.floors, g.n, g.spread, g.btype
        FROM door_osm o JOIN (
            SELECT o.osm_uid, mode(t.floors) AS floors, count(DISTINCT o.county || o.town || o.base) AS n,
                   max(t.floors) - min(t.floors) AS spread, mode(t.btype) AS btype
            FROM door_osm o JOIN door_trade t USING (county, town, base) GROUP BY 1
        ) g USING (osm_uid)""")
    n = con.execute("SELECT count(*), count(*) FILTER (WHERE spread = 0) FROM same_osm").fetchone()
    print(f"  same_osm {n[0]:,}（棟內樓層一致 {n[1]:,}）")


if __name__ == "__main__":
    main()
