"""Phase 1 全國驗證：用網站實際的查詢邏輯（lookup_many）做留一驗證，量各縣市、各信心等級、各來源的準確率。

做法：每縣市從有實價的門牌抽 PER_COUNTY 個當答案（door_trade 的總樓層），在記憶體資料庫裡把這些門牌的
實價全部拿掉（trades、door_trade、trade_doors），同棟推估表（same_bldg、same_osm）用拿掉後的資料重算，
人工校正清空，再查一次這些門牌，比對回傳的樓層和答案。主號的實價（之號門牌）、同巷鄰居、使照、圖資照常使用。
另外從全部門牌（不限有沒有實價）抽樣，量正式網站的涵蓋率（各信心等級的比例）。

限制：答案只能來自有實價的門牌（多為公寓大樓），沒有買賣紀錄的透天、老屋準確率可能較低。

用法：uv run python scripts/validate_phase1.py [每縣市樣本數]   → docs/phase1-validation.md
"""
import datetime as dt
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

from sitefloor.db import DB_PATH
from sitefloor.derive import SAME_BLDG_SQL
from sitefloor.lookup import lookup_many
from sitefloor.osm import SAME_OSM_SQL

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "phase1-validation.md"
PER_COUNTY = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
SEED = 42
CONF_ORDER = ["高", "中", "低", "待確認", "無"]


def connect_loo() -> tuple[duckdb.DuckDBPyConnection, pd.DataFrame]:
    """記憶體資料庫：正式資料表都是指向原資料庫的 view，抽樣門牌的實價拿掉、推估表重算。回傳 (連線, 樣本)。"""
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB_PATH}' AS src (READ_ONLY)")
    for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'").fetchall():
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM src.{t}")
    con.execute(f"""
        CREATE TABLE s AS SELECT county, town, base, floors AS truth, btype FROM (
            SELECT *, row_number() OVER (PARTITION BY county ORDER BY hash(county || town || base || {SEED})) AS k
            FROM src.door_trade) WHERE k <= {PER_COUNTY}""")
    # 實價缺鄉鎮的紀錄只比縣市＋門牌（lookup._TMATCH），一併拿掉
    con.execute("""
        DROP VIEW trades;
        CREATE TABLE trades AS SELECT t.* FROM src.trades t WHERE NOT EXISTS (
            SELECT 1 FROM s WHERE s.county = t.county AND s.base = t.base AND (t.town IS NULL OR t.town = s.town));
        DROP VIEW door_trade;
        CREATE TABLE door_trade AS SELECT * FROM src.door_trade ANTI JOIN s USING (county, town, base);
        DROP VIEW trade_doors;
        CREATE TABLE trade_doors AS SELECT * FROM src.trade_doors ANTI JOIN s USING (county, town, base);
        DROP VIEW same_bldg; DROP VIEW same_osm; DROP VIEW corrections;
        CREATE TABLE corrections AS SELECT * FROM src.corrections WHERE false;""")
    con.execute(SAME_BLDG_SQL)
    con.execute(SAME_OSM_SQL)
    return con, con.execute("SELECT * FROM s").df()


def source_group(src) -> str:
    if not isinstance(src, str):
        return "（無）"
    if src.startswith("推估："):
        return src
    if "使用執照" in src or "清冊" in src or "執照存查" in src:
        return "使用執照"
    if "實價" in src or "週報" in src:
        return "主號的實價"
    return src


def summarize(df: pd.DataFrame, by: str) -> pd.DataFrame:
    hit = df[df.floors_above.notna()]
    g = df.groupby(by, sort=False)
    out = pd.DataFrame({
        "樣本": g.size(),
        "有回傳": hit.groupby(by, sort=False).size(),
        "完全相符%": hit.groupby(by, sort=False).apply(lambda x: (x.floors_above == x.truth).mean() * 100),
        "差一層內%": hit.groupby(by, sort=False).apply(lambda x: ((x.floors_above - x.truth).abs() <= 1).mean() * 100),
        "低估%": hit.groupby(by, sort=False).apply(lambda x: (x.floors_above < x.truth).mean() * 100),
    }).fillna({"有回傳": 0})
    out["有回傳"] = out["有回傳"].astype(int)
    return out.round(1)


def md(df: pd.DataFrame, index_name: str) -> str:
    cols = [index_name] + list(df.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for k, r in df.iterrows():
        rows.append("| " + " | ".join([str(k)] + [f"{v:,}" if isinstance(v, int) else (
            "–" if pd.isna(v) else f"{v:g}") for v in r]) + " |")
    return "\n".join(rows)


def main():
    t0 = time.time()
    con, s = connect_loo()
    print(f"樣本 {len(s):,} 門牌（{s.county.nunique()} 縣市），準備 {time.time() - t0:.0f}s")
    texts = (s.county + s.town + s.base).tolist()
    r = lookup_many(con, texts)
    df = s.reset_index(drop=True).join(r[["floors_above", "confidence", "source"]])
    df["floors_above"] = pd.to_numeric(df.floors_above, errors="coerce")
    df["來源"] = df.source.map(source_group)
    df["confidence"] = pd.Categorical(df.confidence, CONF_ORDER, ordered=True)
    print(f"留一查詢 {time.time() - t0:.0f}s")

    # 涵蓋率：正式資料庫、全部門牌抽樣（不限有實價）
    prod = duckdb.connect(str(DB_PATH), read_only=True)
    doors = prod.execute(f"""
        SELECT county, town, base FROM (
            SELECT DISTINCT county, town, base FROM addresses WHERE town IS NOT NULL AND base LIKE '%號')
        QUALIFY row_number() OVER (PARTITION BY county ORDER BY hash(county || town || base || {SEED})) <= {PER_COUNTY}
    """).df()
    cov = lookup_many(prod, (doors.county + doors.town + doors.base).tolist())
    doors = doors.reset_index(drop=True).join(cov[["floors_above", "confidence"]])
    print(f"涵蓋率 {time.time() - t0:.0f}s")

    hit = df[df.floors_above.notna()]
    overall = ((hit.floors_above == hit.truth).mean() * 100, ((hit.floors_above - hit.truth).abs() <= 1).mean() * 100)
    by_conf = summarize(df.sort_values("confidence"), "confidence")
    by_src = summarize(df, "來源").sort_values("樣本", ascending=False)
    by_county = summarize(df, "county").sort_values("樣本", ascending=False)
    high = df[df.confidence == "高"]
    high = high[high.floors_above.notna()]
    cov_tab = (doors.groupby(["county", "confidence"], observed=False).size().unstack(fill_value=0)
               .reindex(columns=CONF_ORDER, fill_value=0))
    cov_pct = (cov_tab.div(cov_tab.sum(axis=1), axis=0) * 100).round(1)
    cov_all = (doors.confidence.value_counts(normalize=True).reindex(CONF_ORDER).fillna(0) * 100).round(1)

    text = f"""# Phase 1 全國驗證（{dt.date.today()}）

`scripts/validate_phase1.py` 產生。用網站實際的查詢邏輯做**留一驗證**：每縣市從有實價登錄的門牌抽 {PER_COUNTY:,} 個
（共 {len(df):,} 個）當答案，把這些門牌自己的實價拿掉、同棟推估表重算、人工校正不用，再查一次，看推估的地上總樓層對不對。

**限制**：答案只能來自有實價的門牌（以公寓、大樓為主），沒有買賣紀錄的透天、老屋準確率可能較低；
有實價的門牌在正式網站會直接用自己的實價（信心「高」），所以下表量的是「門牌沒有實價時」的推估準確率。

## 總結

- 有回傳樓層：{len(hit) / len(df) * 100:.1f}%；完全相符 {overall[0]:.1f}%、差一層內 {overall[1]:.1f}%
- 信心「高」：完全相符 {(high.floors_above == high.truth).mean() * 100:.1f}%（目標 ≥ 95%）
- 正式網站涵蓋率（全部門牌抽樣 {len(doors):,} 個）：高 {cov_all['高']}%、中 {cov_all['中']}%、低 {cov_all['低']}%、無 {cov_all['無']}%

## 依信心等級

{md(by_conf, "信心")}

## 依推估來源

{md(by_src, "來源")}

## 依縣市

{md(by_county, "縣市")}

## 正式網站涵蓋率（全部門牌抽樣，各信心等級占比 %）

{md(cov_pct, "縣市")}
"""
    OUT.write_text(text)
    print(text)
    print(f"→ {OUT}（{time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
