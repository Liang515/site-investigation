"""建物風險資訊（第一期）：每個門牌的災害潛勢（E）與使用性質（O），構造（C）沿用查詢結果。

災害潛勢圖（政府資料開放授權第 1 版），都是 TWD97 (EPSG:3826)：
- 淹水潛勢：經濟部水利署 淹水潛勢圖（data.gov.tw/dataset/25766）全國版，定量降雨 24 小時 500mm、650mm 兩種情境。
  水利署說明：僅供防救災參考，不適用於土地使用管制或土地利用之依據。
- 活動斷層地質敏感區、山崩與地滑地質敏感區：經濟部地質調查及礦業管理中心 地質敏感區範圍數值檔
  （data.gov.tw/dataset/27744）。實際範圍以公告圖資為準。
- 土石流潛勢溪流影響範圍：農業部農村發展及水土保持署（data.gov.tw/dataset/176526，115 年度 1,753 條）。
- 土壤液化潛勢：地調所全國 API 已失效（2026-11 確認 404），臺北市的 soil.taipei 也連不上；目前用
  臺南市政府（data.gov.tw/dataset/100221）與臺中市政府（data.gov.tw/dataset/138707，Google 雲端硬碟 GeoJSON）的圖資。

使用性質：門牌所有實價紀錄的「主要用途」「都市土地使用分區」眾數（trades.usage／zoning）。

安全防護（第二期）：
- 最近的消防分隊（名稱、直線距離）：內政部消防署 救援與應變單位點位（data.gov.tw/dataset/5969），只取「分隊」。
- 最近的消防栓距離、100 公尺內消防栓數：台灣自來水公司（data.gov.tw/dataset/44642，臺北自來水事業處轄區以外）
  ＋臺北自來水事業處 大臺北地區消防栓（data.gov.tw/dataset/128639）。金門、連江沒有資料。
距離都是直線距離，不是道路距離。

輸出：
- door_risk：county, town, base, flood500, flood650, liquefaction, fault, landslide, debris, usage, zoning,
  fire_station, fire_station_m, hydrant_m, hydrants_100m
  （災害欄位：落在範圍內才有值；沒有值＝不在範圍內或該區沒有圖資，見 risk_coverage）
- hazard_zones：kind, value, rank, 外接矩形, wkb — 地圖圖層用（外框簡化 5 公尺、經緯度）
- risk_coverage：kind, county — 各圖資涵蓋的縣市（查詢時區分「不在範圍內」與「此區無圖資」）
用法：uv run python -m sitefloor.risk   （trades、addresses 之後；資料放在 data/raw/hazards/，見 README）
"""

import csv
import io
import time
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

from sitefloor.db import connect

RAW = Path("data/raw/hazards")
CRS = 3826  # TWD97 TM2（121°）
FLOOD = {"flood500": "定量降雨24小時500mm", "flood650": "定量降雨24小時650mm"}
FLOOD_ORDER = ["0.3-0.5", "0.5-1.0", "1.0-2.0", "2.0-3.0", ">3.0"]
LIQ_LEVELS = ["低潛勢", "中潛勢", "高潛勢"]
ALL_COUNTIES = ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣", "臺中市", "彰化縣", "南投縣",
                "雲林縣", "嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣",
                "金門縣", "連江縣"]
# 山崩與地滑地質敏感區的劃定範圍名稱 → 縣市（有劃定的縣市才算有圖資）
NO_INFO = ("見其他登記事項", "見其它登記事項", "其他", "")  # 主要用途／使用分區裡沒有資訊的值
_LANDSLIDE_AREA = {"嘉義縣市": ["嘉義縣", "嘉義市"], "新竹縣市": ["新竹縣", "新竹市"]}


def _geosens_list() -> list[dict]:
    with open(RAW / "geosens" / "list.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read(path, crs: int = CRS, **kw) -> gpd.GeoDataFrame:
    """crs：檔案沒附 .prj 時用的座標系統（地質敏感區有幾個檔沒附，清單註明 TWD97）。"""
    g = pyogrio.read_dataframe(path, **kw)
    g = (g.set_crs(crs, allow_override=True) if g.crs is None else g).to_crs(CRS)
    g["geometry"] = g.geometry.force_2d()
    return g.explode(index_parts=False).reset_index(drop=True)  # 拆成單一多邊形，空間索引才有效率


def flood_layer(name: str) -> gpd.GeoDataFrame:
    scen = FLOOD[name]
    folder = scen.replace("定量降雨24小時", "Flood_").replace("mm", "mm_24HR")
    # 深度用 Class（1–5）換算：原始 flood_dept 文字有 15 筆是亂碼（例：「0.3-0.>3.0」），Class 都正確
    g = _read(f"/vsizip/{RAW}/flood/{scen}.zip/{folder}/{folder}.shp", columns=["Class"])
    g["rank"] = g.Class.astype(int) - 1
    g["value"] = g["rank"].map(dict(enumerate(FLOOD_ORDER)))
    return g[["value", "rank", "geometry"]]


def geosens_layer(prefix: str) -> gpd.GeoDataFrame:
    """F＝活動斷層（value＝斷層名稱，例：車籠埔斷層）、L＝山崩與地滑（value＝「位於地質敏感區」）。"""
    names = {r["地質敏感區編號"]: r["地質敏感區名稱"] for r in _geosens_list()}
    parts = []
    for d in sorted(p for p in (RAW / "geosens").iterdir() if p.is_dir() and p.name.startswith(prefix)):
        for shp in d.rglob("*.shp"):
            g = _read(shp, read_geometry=True, columns=[])
            code = d.name.split("-")[0]
            g["value"] = names.get(code, code) if prefix == "F" else "位於地質敏感區"
            parts.append(g[["value", "geometry"]])
    g = pd.concat(parts, ignore_index=True)
    return gpd.GeoDataFrame(g, crs=CRS).assign(rank=0)


def debris_layer() -> gpd.GeoDataFrame:
    shp = next((RAW / "debris").rglob("*.shp"))
    g = _read(shp, columns=["Debrisno", "Risk"])
    g["value"] = g.Debrisno + "（" + g.Risk.fillna("") + "風險）"
    return g.assign(rank=g.Risk.map({"低": 0, "中": 1, "高": 2}).fillna(0))[["value", "rank", "geometry"]]


# 臺中市圖資的 class 沒有官方說明：class 1 分布在沿海（清水、梧棲、龍井、臺中港）、class 3 面積最大且最內陸，
# 與「沿海海埔地為高潛勢」的公開說明一致，判斷為 1＝高、2＝中、3＝低（2026-11-05）
TAICHUNG_LIQ = {"1": "高潛勢", "2": "中潛勢", "3": "低潛勢"}
LIQ_COUNTIES = ["臺南市", "臺中市"]


def liquefaction_layer() -> gpd.GeoDataFrame:
    rank = {v: i for i, v in enumerate(LIQ_LEVELS)}
    # 臺南市政府的檔沒附 .prj，座標是 Web Mercator（x 約 1,336 萬）
    tn = _read(RAW / "liquefaction" / "tainan" / "tainan.shp", crs=3857, columns=["分級"], encoding="utf-8")
    tn["value"] = tn["分級"].str.strip()
    tc = _read(RAW / "liquefaction" / "taichung" / "taichung.json", columns=["class"])
    tc["value"] = tc["class"].astype(str).map(TAICHUNG_LIQ)
    g = pd.concat([tn[["value", "geometry"]], tc[["value", "geometry"]]], ignore_index=True)
    g = gpd.GeoDataFrame(g, crs=CRS).dropna(subset=["value"])
    return g.assign(rank=g.value.map(rank))[["value", "rank", "geometry"]]


HYDRANT_RADIUS = 100  # 公尺
_NO_HYDRANT_DATA = ("金門縣", "連江縣")


def _csv(raw: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp950"):
        try:
            return pd.read_csv(io.BytesIO(raw), dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("無法判斷編碼")


def fire_stations() -> gpd.GeoDataFrame:
    """消防分隊（座標欄名寫 TWD97，實際是經緯度）。"""
    u = _csv((RAW / "fire" / "units.csv").read_bytes()).iloc[:, :5]
    u.columns = ["name", "addr", "tel", "x", "y"]
    u = u[u.name.str.contains("分隊", na=False)].dropna(subset=["x", "y"])
    g = gpd.GeoDataFrame(u[["name"]], geometry=gpd.points_from_xy(u.x.astype(float), u.y.astype(float)), crs=4326)
    return g.to_crs(CRS).reset_index(drop=True)


def hydrants() -> gpd.GeoSeries:
    pts = []
    with zipfile.ZipFile(RAW / "fire" / "hydrant_twc.zip") as z:
        for n in z.namelist():
            if n.endswith("消防栓.csv") and not n.startswith("schema"):
                d = _csv(z.read(n))
                pts.append(gpd.points_from_xy(d["栓位X坐標(TWD97)"].astype(float), d["栓位Y坐標(TWD97)"].astype(float)))
    tp = _csv((RAW / "fire" / "hydrant_taipei.csv").read_bytes())
    pts.append(gpd.points_from_xy(tp["97X座標"].astype(float), tp["97Y座標"].astype(float)))
    return gpd.GeoSeries(np.concatenate([np.asarray(p) for p in pts]), crs=CRS)


def protection(pts: gpd.GeoDataFrame) -> pd.DataFrame:
    """每個門牌：最近消防分隊與距離、最近消防栓距離、100 公尺內消防栓數（直線距離，公尺）。"""
    geoms = pts.geometry.values
    st = fire_stations()
    i, d = shapely.STRtree(st.geometry.values).query_nearest(geoms, return_distance=True, all_matches=False)
    out = pd.DataFrame(index=pts.index)
    out.loc[pts.index[i[0]], "fire_station"] = st.name.values[i[1]]
    out.loc[pts.index[i[0]], "fire_station_m"] = np.round(d).astype(int)
    hy = hydrants()
    tree = shapely.STRtree(hy.values)
    i, d = tree.query_nearest(geoms, return_distance=True, all_matches=False, max_distance=2000)
    out.loc[pts.index[i[0]], "hydrant_m"] = np.round(d).astype(int)
    near = tree.query(geoms, predicate="dwithin", distance=HYDRANT_RADIUS)
    out["hydrants_100m"] = np.bincount(near[0], minlength=len(pts))
    no_data = pts.county.isin(_NO_HYDRANT_DATA).values
    out.loc[no_data, ["hydrant_m", "hydrants_100m"]] = None
    return out.astype({"fire_station_m": "Int32", "hydrant_m": "Int32", "hydrants_100m": "Int32"})


def coverage() -> pd.DataFrame:
    """各圖資涵蓋的縣市。"""
    ls = []
    for r in _geosens_list():
        if r["地質敏感區編號"].startswith("L"):
            area = r["地質敏感區名稱"].split("-")[0]
            ls += _LANDSLIDE_AREA.get(area, [area])
    cov = {k: ALL_COUNTIES for k in (*FLOOD, "fault", "debris")}
    cov["landslide"] = sorted(set(ls))
    cov["liquefaction"] = LIQ_COUNTIES
    cov["hydrant"] = [c for c in ALL_COUNTIES if c not in _NO_HYDRANT_DATA]
    return pd.DataFrame([(k, c) for k, cs in cov.items() for c in cs], columns=["kind", "county"])


ZONE_LAYERS = ["flood500", "liquefaction", "fault", "landslide", "debris"]  # 地圖圖層（淹水只畫 500mm 情境）
SIMPLIFY_M = 5  # 地圖用外框簡化的容許誤差（公尺）


def zones(layers: dict) -> pd.DataFrame:
    """地圖圖層用的範圍：簡化外框、轉經緯度，附外接矩形供依畫面範圍查詢。"""
    parts = []
    for k in ZONE_LAYERS:
        g = layers[k]()
        g["geometry"] = g.geometry.simplify(SIMPLIFY_M, preserve_topology=True)
        g = g[~g.geometry.is_empty].to_crs(4326)
        b = g.geometry.bounds
        parts.append(pd.DataFrame({"kind": k, "value": g.value.values, "rank": g["rank"].astype(int).values,
                                   "minlon": b.minx.values, "minlat": b.miny.values,
                                   "maxlon": b.maxx.values, "maxlat": b.maxy.values,
                                   "wkb": shapely.to_wkb(g.geometry.values)}))
    st = fire_stations().to_crs(4326)  # 消防分隊（點）
    parts.append(pd.DataFrame({"kind": "fire_station", "value": st.name.values, "rank": 0,
                               "minlon": st.geometry.x.values, "minlat": st.geometry.y.values,
                               "maxlon": st.geometry.x.values, "maxlat": st.geometry.y.values,
                               "wkb": shapely.to_wkb(st.geometry.values)}))
    return pd.concat(parts, ignore_index=True)


def _match(pts: gpd.GeoDataFrame, layer: gpd.GeoDataFrame) -> pd.Series:
    """每個門牌落在哪個範圍；同時落在多個時取最嚴重的（rank 最大）。"""
    j = gpd.sjoin(pts[["geometry"]], layer[["value", "rank", "geometry"]], predicate="within")
    j = j.sort_values("rank", ascending=False)
    return j[~j.index.duplicated()].value


def main():
    t = time.time()
    con = connect(write=True)
    doors = con.execute("""
        SELECT county, town, base, avg(lon) AS lon, avg(lat) AS lat FROM addresses
        WHERE lon IS NOT NULL AND town IS NOT NULL GROUP BY ALL""").df()
    pts = gpd.GeoDataFrame(doors, geometry=gpd.points_from_xy(doors.lon, doors.lat), crs=4326).to_crs(CRS)
    print(f"  門牌 {len(pts):,}（{time.time() - t:.0f}s）")
    layers = {"flood500": lambda: flood_layer("flood500"), "flood650": lambda: flood_layer("flood650"),
              "fault": lambda: geosens_layer("F"), "landslide": lambda: geosens_layer("L"),
              "debris": debris_layer, "liquefaction": liquefaction_layer}
    out = doors[["county", "town", "base"]].copy()
    for k, load in layers.items():
        layer = load()
        out[k] = _match(pts, layer)
        print(f"  {k:12s} 範圍 {len(layer):,} 塊 → 門牌 {out[k].notna().sum():,}（{time.time() - t:.0f}s）")

    prot = protection(pts)
    for c in prot.columns:
        out[c] = prot[c]
    print(f"  安全防護：最近分隊中位數 {prot.fire_station_m.median():.0f} m、"
          f"100m 內有消防栓 {(prot.hydrants_100m > 0).mean():.0%}（{time.time() - t:.0f}s）")

    # 使用性質：門牌所有實價紀錄的主要用途、使用分區眾數（平手取筆數多、再取字典序，結果固定）
    # 「見其他登記事項」「其他」沒有資訊；使用分區縮寫（住、商…）與「都市：其他:住宅區」統一成「住宅區」
    use = con.execute(f"""
        WITH t AS (
            SELECT county, town, base,
                   CASE WHEN usage IN {NO_INFO} THEN NULL ELSE usage END AS usage,
                   CASE WHEN z IN {NO_INFO} THEN NULL WHEN z = '住' THEN '住宅區' WHEN z = '商' THEN '商業區'
                        WHEN z = '工' THEN '工業區' WHEN z = '農' THEN '農業區' ELSE z END AS zoning
            FROM (SELECT *, regexp_replace(trim(zoning), '^(都市|非都市)：其他:', '') AS z FROM trades)
            WHERE town IS NOT NULL),
        u AS (SELECT county, town, base, usage, count(*) AS k FROM t
                   WHERE usage IS NOT NULL GROUP BY 1, 2, 3, 4
                   QUALIFY row_number() OVER (PARTITION BY county, town, base ORDER BY k DESC, usage) = 1),
             z AS (SELECT county, town, base, zoning, count(*) AS k FROM t
                   WHERE zoning IS NOT NULL GROUP BY 1, 2, 3, 4
                   QUALIFY row_number() OVER (PARTITION BY county, town, base ORDER BY k DESC, zoning) = 1)
        SELECT county, town, base, u.usage, z.zoning FROM u FULL JOIN z USING (county, town, base)""").df()
    out = out.merge(use, on=["county", "town", "base"], how="outer")
    hazard = [*layers]
    # 有座標的門牌都有安全防護資料，所以全部都存
    con.execute("CREATE OR REPLACE TABLE door_risk AS SELECT * FROM out")
    z = zones(layers)
    con.register("zones_df", z)
    con.execute("CREATE OR REPLACE TABLE hazard_zones AS SELECT * FROM zones_df")
    print(f"  hazard_zones {len(z):,} 塊（地圖圖層，{z.wkb.map(len).sum() / 1e6:.0f} MB）")
    con.register("cov_df", coverage())
    con.execute("CREATE OR REPLACE TABLE risk_coverage AS SELECT * FROM cov_df")
    print(f"  door_risk {len(out):,} 筆（有災害潛勢 {out[hazard].notna().any(axis=1).sum():,}、"
          f"有使用性質 {out.usage.notna().sum():,}）（{time.time() - t:.0f}s）")


if __name__ == "__main__":
    main()
