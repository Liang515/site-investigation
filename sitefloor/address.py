"""台灣地址正規化。

所有來源（門牌點位、實價登錄、使用者輸入）都先過 `parse()`，得到同一套鍵：
- base：街路段＋地區＋巷＋弄＋號（含之號），不含樓層，用來對到「棟」
- full：base＋樓層，用來對到「戶」
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path

_FW = str.maketrans("０１２３４５６７８９－（）　", "0123456789-() ")
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_DIGITS = "零一二三四五六七八九"

TOWNS: dict[str, list[str]] = json.loads((Path(__file__).parent / "towns.json").read_text())
# 長名優先，避免「東區」先吃掉「屏東區」之類的前綴問題
_TOWN_LIST = sorted(((c, t) for c, ts in TOWNS.items() for t in ts), key=lambda x: -len(x[1]))
COUNTIES = sorted(TOWNS, key=len, reverse=True)
# 2010 縣市合併前的舊稱 → 現行縣市
_OLD_COUNTY = {"臺北縣": "新北市", "桃園縣": "桃園市", "臺中縣": "臺中市", "臺南縣": "臺南市", "高雄縣": "高雄市"}


def cn2int(s: str) -> int | None:
    """國字或阿拉伯數字 → int（支援到千位）。"""
    if s.isdigit():
        return int(s)
    total, num = 0, 0
    for ch in s:
        if ch in _CN:
            num = _CN[ch]
        elif ch in "十百千":
            unit = {"十": 10, "百": 100, "千": 1000}[ch]
            total += (num or 1) * unit
            num = 0
        else:
            return None
    return total + num


def int2cn(n: int) -> str:
    """段號用：1 → 一、12 → 十二、20 → 二十。"""
    if n < 10:
        return _CN_DIGITS[n]
    tens, ones = divmod(n, 10)
    return ("" if tens == 1 else _CN_DIGITS[tens]) + "十" + (_CN_DIGITS[ones] if ones else "")


def clean(s: str) -> str:
    s = (s or "").translate(_FW).replace("台", "臺").replace("巿", "市").replace("ㄧ", "一").replace(" ", "").strip()
    # 路段號（一段、二段）轉國字；三位數以上多半是地段號，不動
    s = re.sub(r"(?<!\d)(\d{1,2})段", lambda m: int2cn(int(m.group(1))) + "段", s)
    # 「2-3號」→「2之3號」
    s = re.sub(r"(\d+)-(\d+)(?=號)", r"\1之\2", s)
    return s


@dataclass
class Addr:
    county: str = ""
    town: str = ""
    base: str = ""  # 例：市政路390號、大誠街39巷2之3之2號
    floor: int | None = None  # 地下樓為負數
    unit: str = ""  # 樓之幾，例：之1
    raw: str = ""

    @property
    def full(self) -> str:
        if self.floor is None:
            return self.base
        f = f"B{-self.floor}" if self.floor < 0 else str(self.floor)
        return f"{self.base}{f}樓{self.unit}"


_FLOOR = re.compile(r"^(?:(地下|B)([0-9一二三四五六七八九十]+)(?:樓|F|層)?|()([0-9一二三四五六七八九十百]+)(?:樓|F|層))(之\d+)?", re.I)


def parse(text: str, county: str = "", town: str = "") -> Addr:
    raw = text
    county = county if isinstance(county, str) else ""
    town = clean(town) if isinstance(town, str) else ""
    # 來源給的鄉鎮不在該縣市清單（例：實價登錄新竹市填「新竹市」）→ 當作沒給，改由地址判斷
    if town and county and town not in TOWNS.get(county, []):
        town = ""
    s = clean(text)
    s = re.sub(r"^\d{3,6}", "", s)  # 郵遞區號
    s = s.removeprefix("臺灣省")
    # 「臺灣」只在後面接縣市時才是國名；否則是路名（臺中「臺灣大道」）
    if s.startswith("臺灣") and any(s[2:].startswith(c) for c in [*COUNTIES, *_OLD_COUNTY]):
        s = s[2:]
    for old, new in _OLD_COUNTY.items():
        if s.startswith(old):
            s = new + s[len(old):]
    for c in COUNTIES:
        if s.startswith(c):
            county, s = c, s[len(c):]
            break
    # 實價登錄新竹市、嘉義市常寫成「新竹市新竹市…」
    while county and s.startswith(county):
        s = s[len(county):]
    if not town:
        for c, t in _TOWN_LIST:
            if county and c != county:
                continue
            # 合併前的「板橋市」「大里市」「鳳山市」等 → 現行「XX區」
            names = [t] + ([t[:-1] + x for x in "市鎮鄉"] if t.endswith("區") else [])
            hit = next((n for n in names if s.startswith(n)), None)
            if hit:
                county, town, s = c, t, s[len(hit):]
                break
    else:
        # 來源已給鄉鎮：地址裡若還寫著鄉鎮（含改制前舊名，如「平鎮市」之於「平鎮區」）就去掉
        for n in [town] + ([town[:-1] + x for x in "市鎮鄉"] if town.endswith("區") else []):
            if s.startswith(n):
                s = s[len(n):]
                break
    while town and s.startswith(town):  # 「鼓山區鼓山區…」重複
        s = s[len(town):]
    # 村里、鄰：門牌的一部分但不影響定位，去掉
    s = re.sub(r"^[^\d路街道段巷弄號]{1,4}[里村](?![路街道巷弄段])(?=.*[路街道巷號])", "", s)
    s = re.sub(r"^\d+鄰", "", s)

    a = Addr(county=county, town=town, raw=raw)
    m = re.search(r"號", s)
    if not m:
        a.base = s
        return a
    a.base, rest = s[: m.end()], s[m.end():]
    # 「73號之2」是一樓附號，屬門牌本身
    sub = re.match(r"^之(\d+)", rest)
    if sub and not re.match(r"^之\d+(樓|F)", rest, re.I):
        a.base = a.base[:-1] + f"之{sub.group(1)}號"
        rest = rest[sub.end():]
    fm = _FLOOR.match(rest)
    if fm:
        n = cn2int(fm.group(2) or fm.group(4))
        if n is not None:
            a.floor = -n if fm.group(1) else n
            a.unit = fm.group(5) or ""
    return a


def from_parts(county: str, town: str, street: str, area: str, lane: str, alley: str, number: str) -> Addr:
    """門牌點位資料（已拆欄位）→ Addr。"""
    text = "".join(x for x in (street, area, lane, alley, number) if isinstance(x, str))
    return parse(text, county=county, town=town)
