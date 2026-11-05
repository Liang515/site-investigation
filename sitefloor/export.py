"""DuckDB → 部署用 Parquet（data/serve/<表>/part-*.parquet）。

Vercel 上傳單一檔案上限 100 MB，資料庫（1.1 GB）放不上去；改成每張表切成 80 MB 左右的 Parquet，
伺服器在部署環境用 DuckDB 直接讀這些檔案（見 db.connect）。
每張表依查詢最常用的欄位排序，讓 Parquet 的 row group 統計值可以跳過不相關的區塊。

用法：uv run python -m sitefloor.export   （所有建庫步驟之後；要先停掉 server）
"""

import shutil
from pathlib import Path

from sitefloor.db import connect

SERVE_DIR = Path(__file__).resolve().parent.parent / "data" / "serve"
FILE_SIZE = "80MB"

# 表 → (欄位, 排序)；欄位 None＝全部
TABLES = {
    "addresses": (None, "base, county, town"),
    "buildings": (None, "minlon, minlat"),
    "trades": (
        "county, town, base, floor, total_floors, structure, btype, built_year, trade_date, source",
        "base, county, town",
    ),
    "door_trade": (None, "county, town, base"),
    "trade_doors": (None, "county, town, prefix, num"),
    "same_bldg": (None, "base, county, town"),
    "roads": (None, "county, town, road"),
    "coverage": (None, "county"),
    "permits": (None, "base, county, town"),
    "osm_buildings": (None, "minlon, minlat"),
    "osm_parts": (None, "parent_uid"),
    "door_osm": (None, "base, county, town"),
    "corrections": (None, "base, county, town"),
    "same_osm": (None, "base, county, town"),
    "door_risk": (None, "base, county, town"),
    "risk_coverage": (None, "kind, county"),
    "hazard_zones": (None, "kind, minlon, minlat"),
}


def main():
    con = connect()
    have = {t for (t,) in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if SERVE_DIR.exists():
        shutil.rmtree(SERVE_DIR)
    SERVE_DIR.mkdir(parents=True)
    total = 0
    for t, (cols, order) in TABLES.items():
        if t not in have:
            print(f"  {t}：資料庫裡沒有，略過")
            continue
        out = SERVE_DIR / t
        con.execute(f"""COPY (SELECT {cols or "*"} FROM {t} ORDER BY {order})
                        TO '{out}' (FORMAT parquet, COMPRESSION zstd, FILE_SIZE_BYTES '{FILE_SIZE}',
                                    ROW_GROUP_SIZE 100000)""")
        files = sorted(out.glob("*.parquet"))
        size = sum(f.stat().st_size for f in files)
        total += size
        biggest = max(f.stat().st_size for f in files)
        print(f"  {t:14s} {len(files)} 檔  {size / 1e6:7.1f} MB（最大單檔 {biggest / 1e6:.1f} MB）")
    print(f"  合計 {total / 1e6:.0f} MB → {SERVE_DIR}")


if __name__ == "__main__":
    main()
