"""地址模糊搜尋：輸入打錯字、沒寫縣市區、只打一半時，列出最接近的真實門牌。

路名比對（roads 表，derive.py 預先算好）：完全相同 > 開頭相同（打一半）> 包含 > 編輯距離（錯字）。
門牌號：同一條路／巷弄上找相同號碼，沒有就列最接近的號碼。
門牌來源＝門牌點位 ∪ 實價登錄；has＝這個門牌自己有實價登錄。
"""

import re

import duckdb
from rapidfuzz import process
from rapidfuzz.distance import Levenshtein

from sitefloor import landmarks
from sitefloor.address import COUNTIES, TOWNS, clean, parse
from sitefloor.derive import SUGGEST_DOOR, SUGGEST_LANE

# 門牌號拆解：路巷弄＋（臨）號＋之號；巷弄從路名尾端拆出
DOOR = re.compile(SUGGEST_DOOR)
LANE = re.compile(SUGGEST_LANE)
FUZZY_MIN_DOORS = 3
_QUERY = re.compile(r"^(.*?)(\d+)(?:之(\d+))?號?$")  # 使用者輸入：號可省略


def split_base(base: str) -> tuple[str, str, int, int, bool] | None:
    """門牌號 → (路, 巷弄, 號, 之號, 是否臨)。不規則門牌（地號、中文數字）回 None。"""
    m = DOOR.match(base)
    if not m:
        return None
    road, lane = LANE.match(m[1]).groups()
    if not road:
        return None
    return road, lane, int(m[3]), int(m[4] or 0), bool(m[2])


def door_label(lane: str, num: int, sub: int, near: bool) -> str:
    return f"{lane}{'臨' if near else ''}{num}{f'之{sub}' if sub else ''}號"


def _towns_of(county: str) -> list[str]:
    return TOWNS.get(county, []) if county else [t for ts in TOWNS.values() for t in ts]


def variants(text: str) -> list[tuple[str, str]]:
    """輸入的各種解讀：原文、補全縣市簡稱（台中→臺中市）、補全區名簡稱（西屯→西屯區）、修正區名錯字（板僑區→板橋區）。
    不直接改寫原文，因為「中山北路」這種路名開頭也像區名簡稱；各解讀都查，結果合併。"""
    s = clean(text)
    s = re.sub(r"^\d{3,6}", "", s)
    out = [(s, "")]
    county = next((c for c in COUNTIES if s.startswith(c)), "")
    rest = s[len(county) :]
    if not county:
        hits = [c for c in COUNTIES if s.startswith(c[:2])]
        if len(hits) == 1:  # 新竹、嘉義有市有縣，不猜
            county, rest = hits[0], s[2:]
            out.append((county + rest, ""))
    town = next((t for t in sorted(_towns_of(county), key=len, reverse=True) if rest.startswith(t)), "")
    if not town:
        for t in _towns_of(county):
            short = t[:-1]
            if len(short) >= 2 and rest.startswith(short):
                out.append((county + t + rest[len(short) :], ""))
        m = re.match(r"^(\w{1,3}?[區鄉鎮市])", rest)
        if m:
            near = [t for t in _towns_of(county) if len(t) == len(m[1]) and Levenshtein.distance(t, m[1]) == 1]
            if len(near) == 1:
                out.append((county + near[0] + rest[len(m[1]) :], f"區名相近：{m[1]}→{near[0]}"))
    return out


class Suggester:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        rows = con.execute("SELECT county, town, road, n FROM roads").fetchall()
        self.roads = rows
        self.names = [r[2] for r in rows]

    def _roads(self, q: str, county: str, town: str, k: int = 5) -> list[tuple[tuple, str]]:
        """回傳 [(roads 列, 比對方式)]，依相似度排序。"""

        def ok(r):
            return (not county or r[0] == county) and (not town or r[1] == town)

        scored = []
        for i, name in enumerate(self.names):
            if name == q:
                s, how = 100, ""
            elif name.startswith(q):
                s, how = 90, ""
            elif q in name:
                s, how = 80, ""
            else:
                continue
            if ok(self.roads[i]):
                scored.append((s, self.roads[i][3], i, how))
        # 錯字：字數 3 以上才比，允許 1 個字（6 字以上 2 個字）不同
        if len(q) >= 3:
            cutoff = 1 if len(q) < 6 else 2
            for name, d, i in process.extract(
                q, self.names, scorer=Levenshtein.distance, score_cutoff=cutoff, limit=None
            ):
                # 門牌很少的路名多半是來源資料的錯字（例：「化路一段」），錯字比對時不列
                if d and ok(self.roads[i]) and self.roads[i][3] >= FUZZY_MIN_DOORS:
                    scored.append((70 - 10 * d, self.roads[i][3], i, f"路名相近：{q}→{name}"))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        out, seen = [], set()
        for _, _, i, how in scored:
            if i not in seen:
                seen.add(i)
                out.append((self.roads[i], how))
            if len(out) >= k:
                break
        return out

    def suggest(self, con: duckdb.DuckDBPyConnection, text: str, limit: int = 8) -> list[dict]:
        text = str(text or "").strip()
        out: list[dict] = []
        key = landmarks.key(text)
        if len(key) >= 2:
            for addr, names in landmarks.LANDMARKS:
                hit = next((n for n in names if key in landmarks.key(n)), None)
                if hit:
                    out.append({"text": hit, "addr": addr, "kind": "landmark", "has": None, "hint": f"地標 → {addr}"})
        for variant, fix in variants(text):
            out += self._one(con, variant, fix)
        # 去重（同一門牌保留最直接的解讀）；有完全相符的門牌時，其他「號碼最接近」的就不列
        out.sort(key=lambda x: (x["kind"] != "landmark", not x.get("exact", False), len(x.get("hint") or "")))
        seen, uniq = set(), []
        for x in out:
            if x["text"] not in seen:
                seen.add(x["text"])
                uniq.append(x)
        if any(x.get("exact") for x in uniq):
            uniq = [x for x in uniq if x["kind"] != "door" or x.get("exact")]
        uniq.sort(key=lambda x: (x["kind"] != "landmark", not x.get("exact", False)))
        return uniq[:limit]

    def _one(self, con, text: str, fix: str) -> list[dict]:
        a = parse(text)
        m = _QUERY.match(a.base)
        prefix, num, sub = (m[1], int(m[2]), int(m[3] or 0)) if m else (a.base, None, 0)
        qroad, qlane = LANE.match(prefix).groups()
        if len(qroad) < 2:  # 只剩「路」一個字（例：「中山路」被解讀成中山區＋路）不比
            return []
        out = []
        for (county, town, road, _n), how in self._roads(qroad, a.county, a.town or ""):
            head = f"{county}{town}"
            hint = "；".join(x for x in (fix, how) if x)
            if num is None:
                out.append(
                    {
                        "text": f"{head}{road}{qlane}",
                        "kind": "road",
                        "has": None,
                        "hint": hint or "選這條路，再輸入門牌號",
                    }
                )
                continue
            doors = self._doors(con, county, town, road, qlane)
            exact = [d for d in doors if d[0] == num and d[1] == sub]
            pick = exact or sorted(doors, key=lambda d: (abs(d[0] - num), d[1]))[:3]
            for _num, _sub, _near, base, has in pick:
                h = hint or ("" if exact else f"沒有 {door_label(qlane, num, sub, False)}，最接近的門牌")
                out.append({"text": f"{head}{base}", "kind": "door", "has": has, "hint": h, "exact": bool(exact)})
        if num is not None and not fix:
            out += self._exact_elsewhere(con, a, qroad, qlane, num, sub, {x["text"] for x in out})
        return out

    @staticmethod
    def _exact_elsewhere(con, a, road, lane, num, sub, seen) -> list[dict]:
        """路名完全相同的區很多時（中正路 181 個區），_roads 只取門牌最多的前 5 個；
        門牌少的區（臺北市中正區仁愛路一段）有完全相符的門牌也要列出，一次查所有區。"""
        base = road + door_label(lane, num, sub, False)
        where = "base = ?" + (" AND county = ?" if a.county else "") + (" AND town = ?" if a.town else "")
        args = [base] + [x for x in (a.county, a.town) if x]
        rows = con.execute(
            f"""
            SELECT d.county, d.town, t.base IS NOT NULL FROM (
                SELECT DISTINCT county, town FROM addresses WHERE {where}
                UNION SELECT county, town FROM door_trade WHERE {where}) d
            LEFT JOIN door_trade t ON t.county = d.county AND t.town = d.town AND t.base = ?
            WHERE d.town IS NOT NULL ORDER BY d.county, d.town""",
            args * 2 + [base],
        ).fetchall()
        return [
            {"text": f"{c}{t}{base}", "kind": "door", "has": has, "hint": "", "exact": True}
            for c, t, has in rows
            if f"{c}{t}{base}" not in seen
        ]

    @staticmethod
    def _doors(con, county, town, road, lane) -> list[tuple[int, int, bool, str, bool]]:
        rows = con.execute(
            """
            SELECT d.base, t.base IS NOT NULL FROM (
                SELECT DISTINCT base FROM addresses WHERE county = ? AND town = ? AND starts_with(base, ?)
                UNION SELECT base FROM door_trade WHERE county = ? AND town = ? AND starts_with(base, ?)) d
            LEFT JOIN door_trade t ON t.county = ? AND t.town = ? AND t.base = d.base""",
            [county, town, road + lane] * 2 + [county, town],
        ).fetchall()
        out = []
        for base, has in rows:
            p = split_base(base)
            if p and p[0] == road and p[1] == lane:
                out.append((p[2], p[3], p[4], base, has))
        return out
