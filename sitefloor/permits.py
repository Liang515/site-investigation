"""使用執照開放資料 → DuckDB permits 表（一列＝一個門牌號在一張使照上）。

來源（data/raw/permits/，見 README）：
- 臺北市歷年使用執照摘要（data.gov.tw 128203，XML，民國 90 年起）：一張執照列出所有門牌；只取新建、改建
- 新北市建築執照存查（123639，CSV，民國 91 年起）：house_address
- 新竹市使用執照清冊（163823，CSV，101 年起）：門牌地址
- 臺南市公寓大廈管理委員會清冊（120168，CSV）：建築物地址、地上層數（附使照字號）
臺南市使照、新竹縣使照只有地號、沒有門牌，對不到地址，不收。

欄位：county, town, base, floors_above, floors_below, structure, year（竣工年；沒有就用使照發照年）, permit_no, source
同一門牌有多張使照時取最新一張。

用法：uv run python -m sitefloor.permits   （要先停掉 server）
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from sitefloor.address import parse
from sitefloor.db import connect
from sitefloor.trades import norm_structure

RAW = Path("data/raw/permits")


def _roc_year(s) -> int | None:
    """民國日期（1091204、109/12/04、(094)…）→ 西元年。"""
    m = re.search(r"(\d{2,3})", str(s or ""))
    if not m:
        return None
    y = int(m[1])
    return y + 1911 if 30 <= y <= 130 else None


def _int(s) -> int | None:
    try:
        v = int(float(str(s).strip()))
    except ValueError:
        return None
    return v if v >= 0 else None


def _row(county, addr, above, below, struct, year, no, source) -> dict | None:
    a = parse(str(addr or ""), county=county)
    if not a.base.endswith("號") or a.county != county:
        return None
    return {
        "county": county,
        "town": a.town,
        "base": a.base,
        "floors_above": _int(above),
        "floors_below": _int(below),
        "structure": norm_structure(struct),
        "year": year,
        "permit_no": no,
        "source": source,
    }


def taipei() -> list[dict]:
    out = []
    for p in sorted(RAW.glob("tp_use_*.xml")):
        for _, el in ET.iterparse(p):
            if el.tag != "Data":
                continue
            if el.findtext("建造類別") in ("新建", "改建"):
                no = el.findtext("執照號碼")
                year = _roc_year(el.findtext("竣工日期")) or _roc_year(el.findtext("發照日期"))
                above, below = el.findtext(".//地上層數"), el.findtext(".//地下層數")
                struct = el.findtext("構造種類")
                for a in el.iterfind(".//地址"):
                    r = _row("臺北市", a.text, above, below, struct, year, no, "臺北市使用執照")
                    if r:
                        out.append(r)
            el.clear()
    return out


def new_taipei() -> list[dict]:
    d = pd.read_csv(RAW / "ntpc_permit.csv", dtype=str)
    out = []
    for r in d.itertuples():
        year = _roc_year(r.completion_date) or _roc_year(r.date_licensing)
        x = _row(
            "新北市",
            r.house_address,
            r.number_of_stories,
            r.ground_floor,
            None,
            year,
            r.license_number,
            "新北市建築執照存查",
        )
        if x:
            out.append(x)
    return out


def hsinchu_city() -> list[dict]:
    d = pd.read_csv(RAW / "hcc_use.csv", dtype=str)
    d = d[~d["執照字號"].fillna("").str.contains("雜")]  # 雜項執照（電梯、圍牆等）
    out = []
    for r in d.itertuples():
        x = _row(
            "新竹市",
            r.門牌地址,
            r.地上層數,
            r.地下層數,
            r.構造種類,
            _roc_year(r.核准日期),
            r.執照字號,
            "新竹市使用執照",
        )
        if x:
            out.append(x)
    return out


def tainan_apartments() -> list[dict]:
    d = pd.read_csv(RAW / "tn_apt.csv", dtype=str)
    out = []
    for r in d.itertuples():
        x = _row(
            "臺南市",
            r.建築物地址,
            r.地上層數,
            r.地下層數,
            None,
            _roc_year(r.使用執照),
            r.使用執照,
            "臺南市公寓大廈清冊",
        )
        if x:
            out.append(x)
    return out


SOURCES = {"臺北": taipei, "新北": new_taipei, "新竹市": hsinchu_city, "臺南公寓大廈": tainan_apartments}


def main():
    rows = []
    for name, f in SOURCES.items():
        got = f()
        print(f"  {name}：{len(got):,} 門牌列")
        rows += got
    df = pd.DataFrame(rows)
    df = df[df.floors_above.fillna(0) > 0]
    con = connect(write=True)
    # 地址沒寫區（新竹市常見）：同縣市只有一個區有這個門牌號時補上
    df["town"] = df.town.fillna("")
    miss = df[df.town == ""][["county", "base"]].drop_duplicates()
    fill = con.execute("""
        SELECT m.county, m.base, any_value(d.town) AS town2 FROM miss m
        JOIN (SELECT DISTINCT county, town, base FROM addresses UNION SELECT county, town, base FROM door_trade) d
          USING (county, base)
        GROUP BY 1, 2 HAVING count(DISTINCT d.town) = 1""").df()
    df = df.merge(fill, on=["county", "base"], how="left")
    df["town"] = df.town.where(df.town != "", df.town2)
    print(f"  沒寫區的門牌 {len(miss):,}，補上區 {len(fill):,}")
    df = df[df.town.notna()].drop(columns="town2")
    # 同一門牌多張使照：取最新
    df = df.sort_values("year", na_position="first").drop_duplicates(["county", "town", "base"], keep="last")
    df = df.astype({"floors_above": "Int16", "floors_below": "Int16", "year": "Int16"})
    con.execute("CREATE OR REPLACE TABLE permits AS SELECT * FROM df")
    print(
        con.sql(
            "SELECT source, count(*) AS 門牌號, count(structure) AS 有構造, count(year) AS 有年份 "
            "FROM permits GROUP BY 1 ORDER BY 2 DESC"
        )
    )


if __name__ == "__main__":
    main()
