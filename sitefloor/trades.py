"""實價資料 → trades 表。

目前來源：臺北市實價週報（data.taipei，只提供最近一週快照，每次執行另存一份累積）。
全國實價登錄批次檔到位後，用同一個 `to_trades()` 轉換。

用法：uv run python -m sitefloor.trades taipei_weekly
"""
import datetime as dt
import re
import sys
import unicodedata
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

from sitefloor.address import cn2int, parse
from sitefloor.db import connect
from sitefloor.derive import SUGGEST_DOOR, SUGGEST_LANE

TP_WEEKLY_URL = ("https://data.taipei/api/dataset/a9a97996-3a55-46c8-9076-e5ebdefad6dc"
                 "/resource/2979c431-7a32-4067-9af2-e716cd825c4b/download")
TP_WEEKLY_DIR = Path("data/raw/tp_weekly")


def roc_date(s) -> dt.date | None:
    """民國日期 1150912 / 0850315 / 850315 → date。"""
    s = re.sub(r"\D", "", str(s or ""))
    if len(s) not in (6, 7):
        return None
    y, m, d = int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:])
    try:
        return dt.date(y, m, d or 1)
    except ValueError:
        return None


def floor_of(text) -> int | None:
    """移轉層次「五層」「二十層」「地下一層」「一層，二層」→ 第一個樓層。"""
    m = re.search(r"(地下)?([一二三四五六七八九十百\d]+)層", str(text or ""))
    if not m:
        return None
    n = cn2int(m.group(2))
    return None if n is None else (-n if m.group(1) else n)


def norm_structure(text) -> str | None:
    """主要建材 → 標準類別。「見其他登記事項」「見使用執照」等無效值回 None。"""
    t = unicodedata.normalize("NFKC", str(text or "")).upper().replace(".", "").replace("．", "")
    if not t or t.startswith("見"):
        return None
    # 多種構造並列（「鋼骨造、RC造」）：取先寫的主結構
    parts = [p for p in re.split(r"[、,，及與/]", t) if p.strip()]
    if len(parts) > 1:
        first = norm_structure(parts[0])
        if first not in (None, "其他"):
            return first
    if "鋼骨鋼筋混凝土" in t or "SRC" in t or "鋼骨RC" in t:
        return "SRC 鋼骨鋼筋混凝土"
    if "鋼筋混凝土" in t or "RC" in t:
        return "RC 鋼筋混凝土"
    if "鋼骨" in t or "鋼構" in t or t.startswith("鋼造"):
        return "鋼骨／鋼構"
    if "加強磚" in t:
        return "加強磚造"
    if "磚" in t:
        return "磚造"
    if "木" in t:
        return "木造"
    if "鐵" in t or "鋼架" in t:
        return "鐵架／輕鋼架"
    return "其他"


def total_of(text) -> int | None:
    """總樓層數：週報為「025」，實價登錄為「十五層」。"""
    s = str(text or "").strip()
    if s.isdigit():
        return int(s) or None
    n = floor_of(s)
    return n if n and n > 0 else None


# 建物型態與總樓層明顯矛盾時，總樓層當作填錯（例：「華廈(10層含以下)」卻記 100 層）。
# 只排除差距大的；10 層的「住宅大樓(11層含以上)」這種邊界多半是型態選錯，樓層照用。
_BTYPE_RANGE = {"公寓": (1, 10), "華廈": (2, 20), "透天": (1, 10), "住宅大樓": (6, 110)}
MAX_FLOORS = 110  # 臺灣最高 101 層


def sane_total(total: int | None, btype: str | None) -> int | None:
    if total is None or total > MAX_FLOORS:
        return None
    for key, (lo, hi) in _BTYPE_RANGE.items():
        if btype and btype.startswith(key) and not lo <= total <= hi:
            return None
    return total


def to_trades(df: pd.DataFrame, *, county: str, address: str, town: str | None,
              floor: str, total: str, btype: str, structure: str, built: str, date: str,
              kind: str | None, source: str, usage: str | None = None, zoning: str | None = None) -> pd.DataFrame:
    """把任一實價來源（欄位名不同）轉成 trades 表的欄位。"""
    rows = []
    for r in df.to_dict("records"):
        r = {k: (v if isinstance(v, str) else None) for k, v in r.items()}  # NaN → None
        raw = r.get(address) or ""
        if "號" not in raw:  # 土地案件只有地號，沒有門牌
            continue
        a = parse(raw, county=county, town=(r.get(town) or "") if town else "")
        if not a.town:
            a = parse(raw, county=county)
        tf = sane_total(total_of(r.get(total)), r.get(btype))
        b = roc_date(r.get(built))
        rows.append({
            "county": county, "town": a.town or None, "base": a.base, "full": a.full,
            "floor": a.floor if a.floor is not None else floor_of(r.get(floor)),
            "total_floors": tf,
            "structure": norm_structure(r.get(structure)), "btype": r.get(btype) or None,
            "built_year": b.year if b else None, "trade_date": roc_date(r.get(date)),
            "kind": r.get(kind) if kind else None, "source": source, "raw_address": raw,
            "usage": (r.get(usage) or None) if usage else None, "zoning": (r.get(zoning) or None) if zoning else None,
        })
    out = pd.DataFrame(rows)
    return out.astype({"floor": "Int16", "total_floors": "Int16", "built_year": "Int16"})


def fetch_taipei_weekly() -> Path:
    TP_WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(TP_WEEKLY_URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=120).read()
    p = TP_WEEKLY_DIR / f"{dt.date.today():%Y%m%d}.csv"
    p.write_bytes(raw)
    return p


def load_taipei_weekly(con):
    p = fetch_taipei_weekly()
    snaps = sorted(TP_WEEKLY_DIR.glob("*.csv"))
    df = pd.concat([pd.read_csv(s, dtype=str, encoding="utf-8-sig") for s in snaps]).drop_duplicates()
    df = df.where(df.notna(), None)
    t = to_trades(df, county="臺北市", address="LOCATION", town="DISTRICT", floor="SBUILD",
                  total="TBUILD", btype="BUITYPE", structure="MBUILD", built="FDATE",
                  date="SDATE", kind="CASE_T", source="臺北市實價週報", usage="PBUILD", zoning="LANDA_Z")
    con.execute("DELETE FROM trades WHERE source = '臺北市實價週報'")
    con.execute("INSERT INTO trades SELECT * FROM t")
    print(f"  快照 {len(snaps)} 份（本次 {p.name}）、{len(df):,} 列 → 有門牌 {len(t):,} 筆、"
          f"有總樓層 {t.total_floors.notna().sum():,} 筆、有建築完成年 {t.built_year.notna().sum():,} 筆")


# ── 內政部實價登錄批次檔 ─────────────────────────────────────────
PLVR_DIR = Path("data/raw/plvr")
# 檔名首字母 → 縣市（地政司代碼）
PLVR_COUNTY = {
    "a": "臺北市", "b": "臺中市", "c": "基隆市", "d": "臺南市", "e": "高雄市", "f": "新北市",
    "g": "宜蘭縣", "h": "桃園市", "i": "嘉義市", "j": "新竹縣", "k": "苗栗縣", "m": "南投縣",
    "n": "彰化縣", "o": "新竹市", "p": "雲林縣", "q": "嘉義縣", "t": "屏東縣", "u": "花蓮縣",
    "v": "臺東縣", "w": "金門縣", "x": "澎湖縣", "z": "連江縣",
}
PLVR_KIND = {"a": "買賣", "c": "租賃"}  # b 為預售屋：多半只有地號、樓層也未定，不收


def _plvr_frames():
    """逐一讀 data/raw/plvr 底下所有 zip 裡的主表（*_lvr_land_a.csv / *_lvr_land_c.csv）。"""
    zips = sorted(PLVR_DIR.glob("*.zip")) + sorted((PLVR_DIR / "seasons").glob("*.zip"))
    for z in zips:
        try:
            zf = zipfile.ZipFile(z)
        except zipfile.BadZipFile:
            print(f"  略過（不完整或下載中）：{z.name}")
            continue
        with zf:
            for name in zf.namelist():
                m = re.fullmatch(r"([a-z])_lvr_land_([ac])\.csv", Path(name).name)
                if not m or m.group(1) not in PLVR_COUNTY:
                    continue
                with zf.open(name) as f:
                    df = pd.read_csv(f, dtype=str, encoding="utf-8-sig", skiprows=[1])  # 第 2 列是英文欄名
                yield z.stem, PLVR_COUNTY[m.group(1)], PLVR_KIND[m.group(2)], df


def fill_missing_town(con):
    """缺鄉鎮的實價紀錄：門牌在該縣市只出現在一個區，就補上該區。"""
    n = con.execute("""
        UPDATE trades t SET town = u.town
        FROM (SELECT county, base, any_value(town) AS town FROM addresses
              GROUP BY 1, 2 HAVING count(DISTINCT town) = 1) u
        WHERE t.town IS NULL AND t.county = u.county AND t.base = u.base
    """).fetchall()[0][0]
    # 再用道路判斷（build road_towns；嘉義市實價登錄不寫東區／西區、也沒有門牌點位）
    has_roads = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'road_towns'").fetchone()[0]
    if has_roads:
        n += con.execute(f"""
            UPDATE trades t SET town = r.town FROM road_towns r
            WHERE t.town IS NULL AND t.county = r.county
              AND regexp_extract(regexp_extract(t.base, '{SUGGEST_DOOR}', 1), '{SUGGEST_LANE}', 1) = r.road
        """).fetchall()[0][0]
    left = con.execute("SELECT count(*) FROM trades WHERE town IS NULL AND base LIKE '%號'").fetchall()[0][0]
    print(f"  補上鄉鎮 {n:,} 筆；仍缺鄉鎮（有門牌號）{left:,} 筆")


def load_plvr(con):
    parts, n_raw = [], 0
    for zname, county, kind, df in _plvr_frames():
        n_raw += len(df)
        df = df.where(df.notna(), None)
        t = to_trades(df, county=county, address="土地位置建物門牌", town="鄉鎮市區", floor="移轉層次",
                      total="總樓層數", btype="建物型態", structure="主要建材", built="建築完成年月",
                      date="交易年月日", kind=None, source="實價登錄", usage="主要用途", zoning="都市土地使用分區")
        t["kind"] = kind
        t["_id"] = df.loc[df["土地位置建物門牌"].fillna("").str.contains("號"), "編號"].values \
            if "編號" in df else None
        parts.append(t)
    t = pd.concat(parts, ignore_index=True)
    # 同一案件會出現在本期檔與季檔：以編號去重
    if "_id" in t:
        t = t[t._id.isna() | ~t._id.duplicated()]
    t = t.drop(columns="_id")
    con.execute("DELETE FROM trades WHERE source = '實價登錄'")
    con.execute("INSERT INTO trades SELECT * FROM t")
    fill_missing_town(con)
    print(f"  原始 {n_raw:,} 列 → 有門牌 {len(t):,} 筆；有總樓層 {t.total_floors.notna().sum():,}、"
          f"有建築完成年 {t.built_year.notna().sum():,}；區段化門牌（含～）{t.base.str.contains('[~～]').sum():,}")


LOADERS = {"taipei_weekly": load_taipei_weekly, "plvr": load_plvr}

if __name__ == "__main__":
    con = connect(write=True)
    for name in sys.argv[1:] or LOADERS:
        print(f"[{name}]")
        LOADERS[name](con)
    con.close()
