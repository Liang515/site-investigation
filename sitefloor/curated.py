"""人工校正樓層表（curated/floor_corrections.csv，進版控）→ DuckDB corrections 表。

一列＝一棟（或一個門牌）的已確認樓層。兩種指定方式擇一或並用：
- county, town, base：門牌號（例：使用者回報、查證過的實價錯誤）
- osm_uid：OpenStreetMap 建物（例：百貨、球場等沒有單一門牌的地標；查詢時落在這棟裡的門牌都會套用）
欄位：floors_above（必填）、floors_below、source（依據，例：官網樓層介紹網址、使用執照字號、使用者回報）、note、checked（查證日期）。

查詢時優先序最高（高於實價登錄），地圖上 OSM 建物也改用這個樓層。
用法：uv run python -m sitefloor.curated   （osm 之前；每週更新會自動跑）
"""

from pathlib import Path

import pandas as pd

from sitefloor.address import parse
from sitefloor.db import connect

CSV = Path(__file__).resolve().parent.parent / "curated" / "floor_corrections.csv"
COLUMNS = ["county", "town", "base", "osm_uid", "floors_above", "floors_below", "height_m", "source", "note", "checked"]


def load() -> pd.DataFrame:
    df = pd.read_csv(CSV, dtype=str).reindex(columns=COLUMNS) if CSV.exists() else pd.DataFrame(columns=COLUMNS)
    # 空白格轉成 None（pandas 3 的字串欄空值是 NaN，在 if 判斷裡會被當成有值）
    df = df.astype(object).map(lambda x: x.strip() or None if isinstance(x, str) else None)
    bad = []

    def has(v) -> bool:  # 非空字串才算有值（iterrows 會把 None 變回 NaN，所以逐列用 dict）
        return isinstance(v, str) and v != ""

    for i, r in enumerate(df.to_dict("records")):
        if not (has(r["osm_uid"]) or has(r["base"])):
            bad.append(f"第 {i + 2} 列：要有門牌（base）或 osm_uid")
        if has(r["base"]):  # 門牌照查詢的規則正規化（全形、台／臺、之號…）
            a = parse(f"{r['county'] or ''}{r['town'] or ''}{r['base']}")
            df.loc[i, ["county", "town", "base"]] = [a.county or r["county"], a.town or r["town"], a.base]
        try:
            if not 1 <= int(r["floors_above"]) <= 110:
                raise ValueError
        except (TypeError, ValueError):
            bad.append(f"第 {i + 2} 列：floors_above 要是 1–110 的整數")
        if has(r["height_m"]):  # 實際高度（公尺），選填：地圖改用這個高度畫（例：巨蛋一層很高）
            try:
                if not 2 <= float(r["height_m"]) <= 600:
                    raise ValueError
            except ValueError:
                bad.append(f"第 {i + 2} 列：height_m 要是 2–600 的數字（公尺）")
    if bad:
        raise ValueError("floor_corrections.csv 有誤：\n" + "\n".join(bad))
    df["floors_above"] = df.floors_above.astype(int).astype("Int16")
    df["floors_below"] = pd.to_numeric(df.floors_below, errors="coerce").astype("Int16")
    df["height_m"] = pd.to_numeric(df.height_m, errors="coerce")
    return df


def main():
    df = load()
    con = connect(write=True)
    con.execute("CREATE OR REPLACE TABLE corrections AS SELECT * FROM df")
    print(f"  人工校正 {len(df):,} 筆（門牌 {df.base.notna().sum():,}、OSM 建物 {df.osm_uid.notna().sum():,}）")


if __name__ == "__main__":
    main()
