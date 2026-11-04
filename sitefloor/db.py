from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "sitefloor.duckdb"

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
    trade_date DATE, kind VARCHAR, source VARCHAR, raw_address VARCHAR
);
"""


def connect(write: bool = False) -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=not write)
    if write:
        con.execute(SCHEMA)
    return con
