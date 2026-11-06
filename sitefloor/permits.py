"""使用執照開放資料 → DuckDB permits 表（一列＝一個門牌號在一張使照上）。

來源（data/raw/permits/，見 README）：
- 臺北市歷年使用執照摘要（data.gov.tw 128203，XML，民國 90 年起）：一張執照列出所有門牌；只取新建、改建
- 新北市建築執照存查（123639，CSV，民國 91 年起）：house_address
- 新竹市使用執照清冊（163823，CSV，101 年起）：門牌地址
- 臺南市公寓大廈管理委員會清冊（120168，CSV）：建築物地址、地上層數（附使照字號）
臺南市使照、新竹縣使照只有地號、沒有門牌，對不到地址，不收。

欄位：county, town, base, floors_above, floors_below, structure, year（竣工年；沒有就用使照發照年）, permit_no, source,
usage（核准用途，依建築技術規則使用類組歸類，臺北依樓地板面積由大到小）、usage_floors（臺北：各樓層用途摘要）、zoning（使用分區）、
bldg_area（建築面積 m²；臺北、新北）、small（osm.py 填：建築面積不到門牌所在 OSM 建物外框的 1/4、樓層又對不上，
這張使照講的是後來增建的附屬小建物，不套用）
同一門牌有多張使照時取最新一張。用途是領使照時核准的；後來變更使用（變使）不在這份資料裡。

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


# 建築技術規則使用類組 → 顯示名稱
USE_GROUP = {
    "A1": "集會表演", "A2": "運輸場站", "B1": "娛樂場所", "B2": "零售商場", "B3": "餐飲", "B4": "旅館",
    "C1": "工廠（特殊）", "C2": "工廠倉庫", "D1": "健身休閒", "D2": "文教展覽", "D3": "補習班", "D4": "學校",
    "D5": "學校", "E": "宗教殯葬", "F1": "醫療照護", "F2": "社會福利", "F3": "托兒", "F4": "戒護",
    "G1": "金融保險", "G2": "辦公", "G3": "店鋪診所", "H1": "宿舍安養", "H2": "住宅", "I": "危險物品",
}
# 沒寫類組代碼時用關鍵字歸類（依序比對，先比對到的為準）；停車、梯間、機房等附屬空間不列
USE_WORDS = [
    ("住宅", "H2"), ("宿舍", "H1"), ("安養", "H1"), ("事務所", "G2"), ("辦公", "G2"), ("金融", "G1"),
    ("銀行", "G1"), ("餐", "B3"), ("旅館", "B4"), ("零售", "B2"), ("商場", "B2"), ("百貨", "B2"), ("市場", "B2"),
    ("店舖", "G3"), ("店鋪", "G3"), ("診所", "G3"), ("健身", "D1"), ("醫", "F1"), ("托嬰", "F3"), ("幼兒", "F3"),
    ("補習", "D3"), ("學校", "D4"), ("教室", "D4"), ("工廠", "C2"), ("廠房", "C2"), ("倉", "C2"),
    ("宗教", "E"), ("寺", "E"), ("廟", "E"), ("社會福利", "F2"), ("長照", "F2"), ("娛樂", "B1"),
    ("展覽", "D2"), ("圖書", "D2"), ("集會", "A1"), ("劇場", "A1"), ("車站", "A2"),
]
# 「B1~B3停車空間」「B2F」「B1:」的 B1、B2 是地下樓層，不是類組
_CODE = re.compile(r"(?<![A-Za-z])([ABCDFGH])\s*[-－]?\s*([1-5])(?![0-9FfLl樓層~～至:：])|[(（]\s*([EI])\s*[)）]")
_SPLIT = re.compile(r"[,，、;；/]")
_SPLIT_DOT = re.compile(r"[.。]")  # 新北常用「集合住宅.店鋪」分隔；但「一般零售業甲.乙組(G3)」的點不是分隔


def use_groups(text) -> list[str]:
    """用途文字 → 使用類組代碼（依出現順序、不重複）。例：「1F店舖,停車空間,2F~5F集合住宅」→ ["G3", "H2"]。
    一段裡有類組代碼就全取代碼（例：「(G3)及餐飲業)B3)」→ G3、B3），沒有代碼才用關鍵字。"""
    out: list[str] = []
    for part in _SPLIT.split(str(text or "")):
        codes = [a + b if a else c for a, b, c in _CODE.findall(part)]
        if not codes:
            codes = [next((c for w, c in USE_WORDS if w in x), None) for x in _SPLIT_DOT.split(part)]
        out += [c for c in codes if c in USE_GROUP and c not in out]
    return list(dict.fromkeys(out))


def usage_text(codes: list[str]) -> str | None:
    names = list(dict.fromkeys(USE_GROUP[c] for c in codes))  # D4、D5 都是學校
    return "、".join(names) or None


_TP_FLOOR = re.compile(r"(?:^|-)(地下|地上)(\d+)樓")


def taipei_usage(el) -> tuple[str | None, str | None]:
    """臺北使照「建築概要」逐層用途 → (整棟用途：依樓地板面積由大到小, 各樓層摘要：同用途的連續樓層合併)。
    例：「辦公、金融保險、餐飲」「B2–B1 餐飲；1F 金融保險、餐飲；3–20F 辦公、金融保險」"""
    floors: dict[int, list[str]] = {}  # 地下 N 樓 → -N
    area: dict[str, float] = {}
    for f in el.iterfind(".//建築概要/樓層"):
        t = f.text or ""
        m = _TP_FLOOR.search(t.split(",", 1)[0])
        if not m or "用途:" not in t:  # 突出物、騎樓等不列
            continue
        codes = use_groups(t.split("用途:", 1)[1])
        if not codes:
            continue
        n = int(m[2]) * (-1 if m[1] == "地下" else 1)
        floors[n] = list(dict.fromkeys(floors.get(n, []) + codes))
        a = re.search(r"面積:([\d.]+)", t)
        for c in codes:
            area[c] = area.get(c, 0) + (float(a[1]) if a else 0) / len(codes)
    if not floors:
        return None, None
    order = sorted(area, key=lambda c: -area[c])

    def label(n: int) -> str:
        return f"B{-n}" if n < 0 else f"{n}F"

    groups: list[list] = []  # [起, 迄, 用途]
    for n in sorted(floors):
        names = usage_text(sorted(floors[n], key=order.index))
        if groups and groups[-1][2] == names:  # 中間沒列用途的樓層（避難層、機電層）不切開
            groups[-1][1] = n
        else:
            groups.append([n, n, names])
    parts = []
    for a, b, names in groups:
        if a == b:
            rng = label(a)
        elif a > 0:
            rng = f"{a}–{b}F"
        else:
            rng = f"{label(a)}–{label(b)}"
        parts.append(f"{rng} {names}")
    return usage_text(order), "；".join(parts)


def _area(s) -> float | None:
    m = re.search(r"[\d.]+", str(s or "").replace(",", ""))
    try:
        v = float(m[0]) if m else None
    except ValueError:
        return None
    return v if v and v > 0 else None


def _row(county, addr, above, below, struct, year, no, source, usage=None, usage_floors=None,
         zoning=None, bldg_area=None) -> dict | None:
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
        "usage": usage,
        "usage_floors": usage_floors,
        "zoning": (str(zoning).strip(" ,，") or None) if isinstance(zoning, str) else None,
        "bldg_area": _area(bldg_area),
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
                usage, usage_floors = taipei_usage(el)
                zoning, area = el.findtext("使用分區"), el.findtext(".//建築面積")
                for a in el.iterfind(".//地址"):
                    r = _row("臺北市", a.text, above, below, struct, year, no, "臺北市使用執照", usage, usage_floors,
                             zoning, area)
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
            usage_text(use_groups(r.use_of_buildings)),
            zoning=r.land_use_zoning,
            bldg_area=r.building_area,
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
            usage_text(use_groups(r.建築物用途)),
            zoning=r.土地使用分區,
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
    df["small"] = False
    con.execute("CREATE OR REPLACE TABLE permits AS SELECT * FROM df")
    print(
        con.sql(
            "SELECT source, count(*) AS 門牌號, count(structure) AS 有構造, count(year) AS 有年份, "
            "count(usage) AS 有用途, count(usage_floors) AS 有逐層用途 "
            "FROM permits GROUP BY 1 ORDER BY 2 DESC"
        )
    )


if __name__ == "__main__":
    main()
