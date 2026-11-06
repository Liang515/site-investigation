import os
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "sitefloor.duckdb"
# 部署用 Parquet（export.py 產生）；Vercel 上或設 SITEFLOOR_PARQUET=1 時讀這裡
SERVE_DIR = Path(__file__).resolve().parent.parent / "data" / "serve"

SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    uid VARCHAR PRIMARY KEY, county VARCHAR, source VARCHAR,
    floors_above SMALLINT, floors_below SMALLINT,
    structure VARCHAR, btype VARCHAR, year SMALLINT, year_kind VARCHAR,
    lon DOUBLE, lat DOUBLE, minlon DOUBLE, minlat DOUBLE, maxlon DOUBLE, maxlat DOUBLE,
    wkb BLOB
);
CREATE TABLE IF NOT EXISTS addresses (
    county VARCHAR, town VARCHAR, base VARCHAR, "full" VARCHAR, floor SMALLINT,
    lon DOUBLE, lat DOUBLE,
    bldg_uid VARCHAR, match VARCHAR, dist_m DOUBLE
);
-- 實價登錄（全國批次）與臺北實價週報共用；一列＝一筆交易／租賃
CREATE TABLE IF NOT EXISTS trades (
    county VARCHAR, town VARCHAR, base VARCHAR, "full" VARCHAR, floor SMALLINT,
    total_floors SMALLINT, structure VARCHAR, btype VARCHAR, built_year SMALLINT,
    trade_date DATE, kind VARCHAR, source VARCHAR, raw_address VARCHAR,
    usage VARCHAR, zoning VARCHAR,  -- 主要用途（住家用、商業用…）、都市土地使用分區
    unit_price DOUBLE  -- 單價（元／m²；租賃為租金單價）
);
-- 舊資料庫補欄位（CREATE TABLE IF NOT EXISTS 不會加新欄位）
ALTER TABLE trades ADD COLUMN IF NOT EXISTS usage VARCHAR;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS zoning VARCHAR;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS unit_price DOUBLE;
"""


def use_parquet() -> bool:
    return bool(os.environ.get("SITEFLOOR_PARQUET") or os.environ.get("VERCEL"))


def connect(write: bool = False) -> duckdb.DuckDBPyConnection:
    if not write and use_parquet():
        return _connect_parquet()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=not write)
    if write:
        con.execute(SCHEMA)
    return con


def _connect_parquet() -> duckdb.DuckDBPyConnection:
    """記憶體 DuckDB，每張表是指向 data/serve/<表>/*.parquet 的 view（唯讀環境：暫存目錄改到 /tmp）。"""
    con = duckdb.connect(config={"temp_directory": "/tmp/duckdb", "home_directory": "/tmp"})
    tables = sorted(p for p in SERVE_DIR.iterdir() if p.is_dir()) if SERVE_DIR.exists() else []
    if not tables:
        raise FileNotFoundError(f"找不到部署用 Parquet：{SERVE_DIR}（先跑 python -m sitefloor.export）")
    for d in tables:
        con.execute(f"CREATE VIEW {d.name} AS SELECT * FROM read_parquet('{d}/*.parquet')")
    return con
