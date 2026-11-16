"""部署前檢查：用部署用的 Parquet（data/serve）實際查幾筆，確認查詢、模糊搜尋、地圖都正常。

本機資料庫正常、Parquet 少了欄位時，部署後所有查詢都會 500（曾發生過），所以 export 之後一定要跑。
也擋下用舊資料部署（曾從另一台電腦拿舊資料蓋掉正式站）：
- 資料比正式站舊（最新成交日期較早）→ 拒絕；確定要部署時加 SITEFLOOR_FORCE_DEPLOY=1
- 舊版匯出（缺成交價格、缺地圖方柱表、row group 沒照排序寫出）→ 拒絕
用法：uv run python -m sitefloor.smoke   （失敗時 exit 1）
"""

import json
import os
import sys
import urllib.request
from itertools import pairwise
from pathlib import Path

PROD_URL = "https://site-investigation.vercel.app"
# 依排序欄位查詢的主要表：row group 的值域不能重疊（重疊代表平行寫檔打亂了順序，查詢會變慢）
SORTED = {"addresses": "base", "trades": "base", "door_risk": "base", "pillars": "lat"}

CASES = {"台北101": 101, "85大樓": 85, "臺北市中山區松江路169號": 17}


def overlapping_row_groups(serve: Path, table: str, col: str) -> int:
    """相鄰 row group 值域重疊的次數（照排序寫出時為 0）。"""
    import duckdb

    rows = duckdb.sql(f"""
        SELECT stats_min_value, stats_max_value FROM parquet_metadata('{serve / table}/*.parquet')
        WHERE path_in_schema = '{col}' ORDER BY file_name, row_group_id""").fetchall()
    conv = float if col in ("lat", "lon") else str
    return sum(1 for a, b in pairwise(rows) if None not in (a[1], b[0]) and conv(b[0]) < conv(a[1]))


def prod_version() -> str | None:
    """正式站資料的最新成交日期；連不到或舊版沒有這個 API 時回 None（不擋）。"""
    try:
        with urllib.request.urlopen(PROD_URL + "/api/version", timeout=20) as r:
            return json.loads(r.read()).get("latest_trade")
    except (OSError, ValueError):
        return None


def main() -> int:
    os.environ["SITEFLOOR_PARQUET"] = "1"
    from sitefloor import names
    from sitefloor.db import SERVE_DIR, connect
    from sitefloor.lookup import lookup, lookup_many
    from sitefloor.suggest import Suggester

    con = connect()
    bad = []
    for q, want in CASES.items():
        got = lookup(con, q)["floors_above"]
        if got != want:
            bad.append(f"{q}：預期 {want} 層，得到 {got}")
    r = lookup(con, "台北101")  # 建物風險資訊：有座標的門牌一定會判斷（在範圍內、未在範圍內或此區無圖資）
    try:  # 網站回傳 JSON：numpy 型別會讓所有查詢 500（曾發生過）
        json.dumps(r)
    except TypeError as e:
        bad.append(f"查詢結果無法轉成 JSON：{e}")
    if not r.get("flood500") or not r.get("zoning"):
        bad.append(f"建物風險資訊缺漏：淹水 {r.get('flood500')}、使用分區 {r.get('zoning')}")
    if len(lookup_many(con, list(CASES) * 3)) != len(CASES) * 3:
        bad.append("整批查詢筆數不符")
    if not Suggester(con).suggest(con, "市正路386"):
        bad.append("模糊搜尋沒有結果")
    n = con.execute("SELECT count(*) FROM buildings WHERE maxlon >= 121.56 AND minlon <= 121.57").fetchall()[0][0]
    if not n:
        bad.append("地圖建物查不到")
    if not names.search(con, "台北之星"):
        bad.append("名稱搜尋查不到（部署檔缺 bldg_names：先跑 python -m sitefloor.names 再 export）")
    for t in ("stats_price", "stats_bldg", "stats_risk"):  # 統計頁
        if not (SERVE_DIR / t).exists():
            bad.append(f"部署檔缺 {t} 表（先跑 python -m sitefloor.stats 再 export）")
    # 舊版匯出：缺成交價格、缺地圖方柱表、row group 順序亂掉
    if not lookup(con, "臺北市大安區辛亥路三段3號").get("trades"):
        bad.append("查不到成交紀錄（部署檔缺成交價格欄位，可能是舊版匯出）")
    for t, col in SORTED.items():
        if not (SERVE_DIR / t).exists():
            bad.append(f"部署檔缺 {t} 表（舊版匯出？請先跑 python -m sitefloor.export）")
        elif n := overlapping_row_groups(SERVE_DIR, t, col):
            bad.append(f"{t} 有 {n} 個 row group 順序重疊（舊版匯出，查詢會變慢）")
    # 資料不能比正式站舊
    local = con.execute("SELECT max(trade_date) FILTER (WHERE trade_date <= current_date) FROM trades").fetchall()[0][0]
    local = local.isoformat() if local else None
    prod = prod_version()
    if prod and local and local < prod:
        msg = f"本機資料比正式站舊（最新成交 {local}，正式站 {prod}）：可能是另一台電腦的資料比較新"
        if os.environ.get("SITEFLOOR_FORCE_DEPLOY"):
            print(f"注意：{msg}；已用 SITEFLOOR_FORCE_DEPLOY 強制部署")
        else:
            bad.append(msg + "；確定要部署請加 SITEFLOOR_FORCE_DEPLOY=1")
    print(f"資料版本：最新成交 {local}（正式站 {prod or '不明'}）")
    print("部署前檢查：" + ("通過" if not bad else "失敗\n  " + "\n  ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
