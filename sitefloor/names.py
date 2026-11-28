"""建物／建案名稱 → 代表門牌（名稱搜尋用）：bldg_names(key, name, county, town, base, source, n)。

來源（同名時人工別名優先）：
- alias：人工別名 curated/building_names.csv（名稱,地址）——資料裡找不到的知名建案（例：台北之星＝Diamond Towers）
- presale：實價登錄預售屋的建案名稱（季檔，101 年起），代表門牌＝該建案最常出現的門牌
- osm：OpenStreetMap 有名稱的建物，代表門牌＝棟內實價紀錄最多的門牌（沒有門牌的建物不收）
key 是 landmarks.key()（去空白、全形轉半形、台→臺、英文小寫），查詢時用同一套正規化比對。

建表：osm.py 最後呼叫 build()（要 door_osm、door_trade）；單獨重建：uv run python -m sitefloor.names
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    import pandas as pd

from sitefloor import landmarks
from sitefloor.address import parse

ROOT = Path(__file__).resolve().parent.parent
CURATED = ROOT / "curated" / "building_names.csv"
PLVR_DIR = ROOT / "data" / "raw" / "plvr"
SOURCE_LABEL = {"alias": "建案別名", "presale": "預售建案", "osm": "建物名稱"}
SUGGEST_MAX = 6
ODD_BASE = re.compile(r"地號$|[、，,與]")  # 地號、多個地段號、描述文字（「…與…轉角」）都不是門牌
MULTI_NO = re.compile(r"(\d+)(?:[、，,及]\d+)+號")


def _presale() -> pd.DataFrame:
    """預售屋季檔的（建案名稱, 縣市, 鄉鎮, 門牌）：只收有門牌號的紀錄。"""
    import pandas as pd

    from sitefloor.trades import PLVR_COUNTY

    parts = []
    for z in sorted((PLVR_DIR / "seasons").glob("*.zip")) + sorted(PLVR_DIR.glob("*.zip")):
        try:
            zf = zipfile.ZipFile(z)
        except zipfile.BadZipFile:
            continue
        with zf:
            for name in zf.namelist():
                m = re.fullmatch(r"([a-z])_lvr_land_b\.csv", Path(name).name)
                if not m or m.group(1) not in PLVR_COUNTY:
                    continue
                with zf.open(name) as f:
                    df = pd.read_csv(f, dtype=str, encoding="utf-8-sig", skiprows=[1], on_bad_lines="skip",
                                     quoting=csv.QUOTE_NONE,  # 少數檔案有落單的引號，會讓後面整份讀不到
                                     usecols=lambda c: c in ("建案名稱", "鄉鎮市區", "土地位置建物門牌"))
                if "建案名稱" in df:
                    parts.append(df.assign(county=PLVR_COUNTY[m.group(1)]))
    if not parts:
        return pd.DataFrame(columns=["name", "county", "town", "base"])
    df = pd.concat(parts, ignore_index=True).dropna(subset=["建案名稱", "土地位置建物門牌"])
    df = df[df["土地位置建物門牌"].str.contains("號") & ~df["建案名稱"].str.contains("?", regex=False)]
    rows = []
    for name, county, town, raw in df[["建案名稱", "county", "鄉鎮市區", "土地位置建物門牌"]].itertuples(index=False):
        raw = MULTI_NO.sub(r"\1號", raw)  # 「寧夏路144、146、148號」取第一個號碼
        a = parse(raw, county=county, town=town or "")
        if a.town and a.base.endswith("號") and not ODD_BASE.search(a.base):
            rows.append((name.strip(), county, a.town, a.base))
    return pd.DataFrame(rows, columns=["name", "county", "town", "base"])


def _aliases() -> pd.DataFrame:
    import pandas as pd

    rows = []
    if CURATED.exists():
        with CURATED.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                a = parse(r["地址"])
                if a.county and a.town and a.base.endswith("號"):
                    rows.append((r["名稱"].strip(), a.county, a.town, a.base))
    return pd.DataFrame(rows, columns=["name", "county", "town", "base"])


BUILD_SQL = """
CREATE OR REPLACE TABLE bldg_names AS
WITH osm AS (
    SELECT b.name, o.county, o.town, o.base, coalesce(t.n, 0) AS n FROM osm_buildings b
    JOIN door_osm o ON o.osm_uid = b.uid
    LEFT JOIN door_trade t USING (county, town, base)
    WHERE coalesce(b.name, '') <> ''
    QUALIFY row_number() OVER (PARTITION BY b.uid ORDER BY coalesce(t.n, 0) DESC, o.base) = 1
),
pre AS (
    SELECT * FROM (SELECT name, county, town, base, count(*) AS n FROM presale GROUP BY name, county, town, base)
    QUALIFY row_number() OVER (PARTITION BY name, county ORDER BY n DESC, base) = 1
),
alias AS (SELECT name, county, town, base, 0 AS n FROM aliases),
allsrc AS (
    SELECT *, 'alias' AS source FROM alias
    UNION ALL SELECT *, 'presale' FROM pre
    UNION ALL SELECT *, 'osm' FROM osm
)
SELECT name_key(name) AS key, name, county, town, base, source, n FROM allsrc
QUALIFY row_number() OVER (PARTITION BY name_key(name), county, town, base
                           ORDER BY source = 'alias' DESC, source = 'presale' DESC) = 1
"""


def build(con: duckdb.DuckDBPyConnection) -> int:
    presale, aliases = _presale(), _aliases()
    cur = con.cursor()  # 獨立的游標：呼叫端連線上有沒讀完的結果時，DuckDB 2.0 會拒絕 register
    cur.register("presale", presale)
    cur.register("aliases", aliases)
    cur.create_function("name_key", landmarks.key, ["VARCHAR"], "VARCHAR", side_effects=False)
    try:
        cur.execute(BUILD_SQL)
        return cur.execute("SELECT count(*) FROM bldg_names").fetchall()[0][0]
    finally:
        cur.close()


def search(con, text: str, limit: int = SUGGEST_MAX) -> list[dict]:
    """名稱包含輸入文字的建物／建案（完全相符、開頭相符、實價筆數多的優先）。沒有名稱表時回空。"""
    k = landmarks.key(text)
    if len(k) < 2:
        return []
    try:
        rows = con.execute("""
            SELECT name, county, town, base, source FROM bldg_names WHERE contains(key, $k)
            ORDER BY key = $k DESC, source = 'alias' DESC, starts_with(key, $k) DESC, n DESC, length(key)
            LIMIT $lim""", {"k": k, "lim": limit}).fetchall()
    except duckdb.CatalogException:
        return []
    return [{"name": n, "addr": f"{c}{t}{b}", "town": t, "base": b, "source": s} for n, c, t, b, s in rows]


def resolve(con, text: str) -> tuple[str, str] | None:
    """名稱完全相符時回傳 (名稱, 地址)：人工別名優先；其餘只有一個門牌才算（同名的好幾棟就不猜）。"""
    k = landmarks.key(text)
    if len(k) < 2:
        return None
    try:
        rows = con.execute("SELECT name, county || town || base, source FROM bldg_names WHERE key = ?", [k]).fetchall()
    except duckdb.CatalogException:
        return None
    alias = [r for r in rows if r[2] == "alias"]
    if alias:
        return alias[0][0], alias[0][1]
    addrs = {r[1] for r in rows}
    return (rows[0][0], rows[0][1]) if len(addrs) == 1 else None


if __name__ == "__main__":
    from sitefloor.db import connect

    print(f"  bldg_names {build(connect(write=True)):,}")
