"""建物風險資訊（第一期）：每個門牌的災害潛勢（E）與使用性質（O），構造（C）沿用查詢結果。

災害潛勢圖（政府資料開放授權第 1 版），都是 TWD97 (EPSG:3826)：
- 淹水潛勢：經濟部水利署 淹水潛勢圖（data.gov.tw/dataset/25766）全國版，定量降雨 24 小時 500mm、650mm 兩種情境。
  水利署說明：僅供防救災參考，不適用於土地使用管制或土地利用之依據。
- 活動斷層地質敏感區、山崩與地滑地質敏感區：經濟部地質調查及礦業管理中心 地質敏感區範圍數值檔
  （data.gov.tw/dataset/27744）。實際範圍以公告圖資為準。
- 土石流潛勢溪流影響範圍：農業部農村發展及水土保持署（data.gov.tw/dataset/176526，115 年度 1,753 條）。
- 土壤液化潛勢：地調所全國 API 已失效（2026-11 確認 404），目前只有臺南市政府的圖資（data.gov.tw/dataset/100221）。

使用性質：門牌所有實價紀錄的「主要用途」「都市土地使用分區」眾數（trades.usage／zoning）。

輸出：
- door_risk：county, town, base, flood500, flood650, liquefaction, fault, landslide, debris, usage, zoning
  （災害欄位：落在範圍內才有值；沒有值＝不在範圍內或該區沒有圖資，見 risk_coverage）
- risk_coverage：kind, county — 各圖資涵蓋的縣市（查詢時區分「不在範圍內」與「此區無圖資」）
用法：uv run python -m sitefloor.risk   （trades、addresses 之後；資料放在 data/raw/hazards/，見 README）
"""

import csv
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

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


def liquefaction_layer() -> gpd.GeoDataFrame:
    # 臺南市政府的檔沒附 .prj，座標是 Web Mercator（x 約 1,336 萬）
    g = _read(RAW / "liquefaction" / "tainan" / "tainan.shp", crs=3857, columns=["分級"], encoding="utf-8")
    g["value"] = g["分級"].str.strip()
    return g.assign(rank=g.value.map({v: i for i, v in enumerate(LIQ_LEVELS)}))[["value", "rank", "geometry"]]


def coverage() -> pd.DataFrame:
    """各圖資涵蓋的縣市。"""
    ls = []
    for r in _geosens_list():
        if r["地質敏感區編號"].startswith("L"):
            area = r["地質敏感區名稱"].split("-")[0]
            ls += _LANDSLIDE_AREA.get(area, [area])
    cov = {k: ALL_COUNTIES for k in (*FLOOD, "fault", "debris")}
    cov["landslide"] = sorted(set(ls))
    cov["liquefaction"] = ["臺南市"]
    return pd.DataFrame([(k, c) for k, cs in cov.items() for c in cs], columns=["kind", "county"])


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
    out = out[out[hazard + ["usage", "zoning"]].notna().any(axis=1)]  # 全部空白的門牌不存
    con.execute("CREATE OR REPLACE TABLE door_risk AS SELECT * FROM out")
    con.register("cov_df", coverage())
    con.execute("CREATE OR REPLACE TABLE risk_coverage AS SELECT * FROM cov_df")
    print(f"  door_risk {len(out):,} 筆（有災害潛勢 {out[hazard].notna().any(axis=1).sum():,}、"
          f"有使用性質 {out.usage.notna().sum():,}）（{time.time() - t:.0f}s）")


if __name__ == "__main__":
    main()
