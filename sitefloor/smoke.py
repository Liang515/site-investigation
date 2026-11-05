"""部署前檢查：用部署用的 Parquet（data/serve）實際查幾筆，確認查詢、模糊搜尋、地圖都正常。

本機資料庫正常、Parquet 少了欄位時，部署後所有查詢都會 500（曾發生過），所以 export 之後一定要跑。
用法：uv run python -m sitefloor.smoke   （失敗時 exit 1）
"""

import json
import os
import sys

CASES = {"台北101": 101, "85大樓": 85, "臺北市中山區松江路169號": 17}


def main() -> int:
    os.environ["SITEFLOOR_PARQUET"] = "1"
    from sitefloor.db import connect
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
    n = con.execute("SELECT count(*) FROM buildings WHERE maxlon >= 121.56 AND minlon <= 121.57").fetchone()[0]
    if not n:
        bad.append("地圖建物查不到")
    print("部署前檢查：" + ("通過" if not bad else "失敗\n  " + "\n  ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
