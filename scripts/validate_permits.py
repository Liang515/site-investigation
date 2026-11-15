"""用使用執照當答案驗證推估準確率：補足 Phase 1（答案只能來自有實價的門牌，以公寓大樓為主）看不到的透天、低樓層。

做法：取有使照樓層、但自己沒有實價的門牌（網站要靠推估的那些），在記憶體資料庫裡把這些門牌所屬使照的資料全部拿掉
（同一張使照的其他門牌也拿掉，避免答案從同棟外洩），OSM 建物由使照推來的樓層清掉，人工校正清空，再查一次，
比對回傳的樓層和使照記載的地上樓層。其他資料（鄰居的實價、同棟、同巷、附近、圖資）照常使用。

限制：使照只有臺北市、新北市、新竹市有門牌（臺南公寓大廈清冊只收公寓大樓，不用）；樣本幾乎都是 2001 年後的新建物，
新建物常和周圍的舊房子樓層不同，鄰居類推估的準確率會比一般門牌低，結果不能直接套到老透天。
使照樓層本身對實價相符約 92–96%，答案也有少量誤差。沒有建物型態欄位，以樓層分組（1–3 層多為透天）。

用法：PYTHONPATH=. uv run python scripts/validate_permits.py   → docs/permit-validation.md
"""
import datetime as dt
import time
from pathlib import Path

import duckdb
import pandas as pd
from validate_phase1 import CONF_ORDER, md, source_group, summarize

from sitefloor.db import DB_PATH
from sitefloor.lookup import lookup_many

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "permit-validation.md"
BANDS = ["1–3 層", "4–5 層", "6–10 層", "11 層以上"]


def band(f: int) -> str:
    return BANDS[0] if f <= 3 else BANDS[1] if f <= 5 else BANDS[2] if f <= 10 else BANDS[3]


def connect_loo() -> tuple[duckdb.DuckDBPyConnection, pd.DataFrame]:
    """記憶體資料庫：正式資料表都是指向原資料庫的 view；樣本門牌所屬使照拿掉、OSM 建物的使照樓層清掉。回傳 (連線, 樣本)。"""
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB_PATH}' AS src (READ_ONLY)")
    for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'").fetchall():
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM src.{t}")
    con.execute("""
        CREATE TABLE s AS SELECT county, town, base, any_value(floors_above) AS truth, any_value(source) AS psrc,
               any_value(usage) AS usage, min("year") AS py
        FROM src.permits p
        WHERE source <> '臺南市公寓大廈清冊' AND floors_above IS NOT NULL AND town IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM src.door_trade d WHERE d.county = p.county AND d.town = p.town AND d.base = p.base)
        GROUP BY ALL HAVING count(DISTINCT floors_above) = 1 AND NOT bool_or(coalesce(small, false))""")
    con.execute("""
        DROP VIEW permits;
        CREATE TABLE permits AS SELECT * FROM src.permits p WHERE NOT EXISTS (
            SELECT 1 FROM s WHERE s.county = p.county AND s.town = p.town AND s.base = p.base)
          AND (p.permit_no IS NULL OR p.permit_no NOT IN (
            SELECT q.permit_no FROM src.permits q JOIN s USING (county, town, base) WHERE q.permit_no IS NOT NULL));
        DROP VIEW osm_buildings;
        CREATE TABLE osm_buildings AS SELECT * REPLACE (NULL::SMALLINT AS permit_floors) FROM src.osm_buildings;
        DROP VIEW corrections;
        CREATE TABLE corrections AS SELECT * FROM src.corrections WHERE false;""")
    return con, con.execute("SELECT * FROM s").df()


def main():
    t0 = time.time()
    con, s = connect_loo()
    print(f"樣本 {len(s):,} 門牌，準備 {time.time() - t0:.0f}s")
    r = lookup_many(con, (s.county + s.town + s.base).tolist())
    df = s.reset_index(drop=True).join(r[["floors_above", "confidence", "source"]])
    df["floors_above"] = pd.to_numeric(df.floors_above, errors="coerce")
    df["來源"] = df.source.map(source_group)
    df["樓層"] = pd.Categorical(df.truth.map(band), BANDS, ordered=True)
    df["confidence"] = pd.Categorical(df.confidence, CONF_ORDER, ordered=True)
    df["縣市"] = df.psrc.str.replace("使用執照", "").str.replace("建築執照存查", "")
    u = df.usage.fillna("")
    df["用途"] = u.map(lambda x: "住宅" if "住宅" in x else "未記載" if not x else "非住宅")
    home = df[df["用途"] == "住宅"]
    by_use = summarize(df, "用途").sort_values("樣本", ascending=False)
    by_src_home = summarize(home, "來源").sort_values("樣本", ascending=False)
    by_conf_home = summarize(home.sort_values("confidence"), "confidence")
    years = df.py.describe()
    print(f"查詢 {time.time() - t0:.0f}s")

    hit = df[df.floors_above.notna()]
    low = df[df.truth <= 3]
    low_hit = low[low.floors_above.notna()]
    by_band = summarize(df.sort_values("樓層"), "樓層")
    by_conf = summarize(df.sort_values("confidence"), "confidence")
    by_conf_low = summarize(low.sort_values("confidence"), "confidence")
    by_src = summarize(df, "來源").sort_values("樣本", ascending=False)
    by_src_low = summarize(low, "來源").sort_values("樣本", ascending=False)
    by_county = summarize(df, "縣市").sort_values("樣本", ascending=False)

    text = f"""# 使用執照驗證：沒有實價的門牌（{dt.date.today()}）

`scripts/validate_permits.py` 產生。補足 [Phase 1 驗證](phase1-validation.md) 看不到的部分：Phase 1 的答案只能來自有實價的門牌
（以公寓、大樓為主），這裡改用**使用執照的地上樓層**當答案（排除建築面積不到 OSM 外框 25% 的增建小使照），量**自己沒有實價、網站要靠推估**的門牌（共 {len(df):,} 個）。

做法：把這些門牌所屬使照的資料全部拿掉（同一張使照的其他門牌也拿掉）、OSM 建物由使照推來的樓層清掉、人工校正不用，
再查一次；鄰居的實價、同棟、同巷、附近、圖資照常使用。

**限制**：使照只有臺北市、新北市、新竹市有門牌，臺北、新北只有民國 90 年後的使照，鄉村透天沒有涵蓋；
使照樓層本身對實價相符約 92–96%，答案也有少量誤差。沒有建物型態，以樓層分組（1–3 層多為透天）。

## 總結

- 有回傳樓層：{len(hit) / len(df) * 100:.1f}%；完全相符 {(hit.floors_above == hit.truth).mean() * 100:.1f}%、差一層內 {((hit.floors_above - hit.truth).abs() <= 1).mean() * 100:.1f}%
- 使照年份：中位數 {years['50%']:.0f} 年，2001 年前只有 {(df.py < 2001).sum():,} 個（{(df.py < 2001).mean() * 100:.1f}%）——量的是**新建物**
- 1–3 層（{len(low):,} 個）：有回傳 {len(low_hit) / len(low) * 100:.1f}%；完全相符 {(low_hit.floors_above == low_hit.truth).mean() * 100:.1f}%、差一層內 {((low_hit.floors_above - low_hit.truth).abs() <= 1).mean() * 100:.1f}%

## 依樓層

{md(by_band, "使照樓層")}

## 依信心等級

{md(by_conf, "信心")}

### 只看 1–3 層

{md(by_conf_low, "信心")}

## 依使照用途

工廠、店鋪、學校、宗教建築等非住宅，本來就不適合用鄰居的樓層推估。

{md(by_use, "用途")}

### 住宅：依信心等級

{md(by_conf_home, "信心")}

### 住宅：依推估來源

{md(by_src_home, "來源")}

## 依推估來源（全部）

{md(by_src, "來源")}

### 只看 1–3 層

{md(by_src_low, "來源")}

## 依縣市

{md(by_county, "縣市")}
"""
    OUT.write_text(text)
    print(text)
    print(f"→ {OUT}（{time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
