"""地址 → 建物樓層查詢。單筆與批次共用同一段 SQL（批次一次 join，不逐筆查）。

兩條線索各自比對、最後合併：
- 圖資：addresses（門牌點）→ buildings（建物圖樓層）
- 實價：trades（實價登錄／週報的總樓層、建材、建築完成年）
"""
import re

import duckdb
import pandas as pd

from sitefloor import landmarks
from sitefloor.address import parse
from sitefloor.derive import DOOR_RE, NEAR_GRID

LEVEL_NAME = {1: "完整地址（含樓層）", 2: "門牌號", 3: "主號推估"}

_MATCH = """(q.county = '' OR {t}.county = q.county) AND (q.town = '' OR {t}.town = q.town)"""
# 實價紀錄可能缺鄉鎮（同縣市多區都有此門牌、無法補），此時只比縣市＋門牌
_TMATCH = """(q.county = '' OR t.county = q.county) AND (q.town = '' OR t.town IS NULL OR t.town = q.town)"""

_SQL = f"""
WITH m AS (
    SELECT q.idx, 1 AS lvl, a.* FROM q JOIN addresses a
      ON q.floor IS NOT NULL AND a."full" = q."full" AND {_MATCH.format(t="a")}
    UNION ALL
    SELECT q.idx, 2, a.* FROM q JOIN addresses a ON a.base = q.base AND {_MATCH.format(t="a")}
    UNION ALL
    SELECT q.idx, 3, a.* FROM q JOIN addresses a
      ON q.base_main <> q.base AND a.base = q.base_main AND {_MATCH.format(t="a")}
),
best AS (SELECT * FROM m QUALIFY lvl = min(lvl) OVER (PARTITION BY idx)),
amb AS (SELECT idx, count(DISTINCT county || '/' || town) AS n_town FROM best GROUP BY idx),
per AS (
    SELECT idx, lvl, bldg_uid, count(*) AS n,
           min(CASE match WHEN 'within' THEN 0 WHEN 'nearest' THEN 1 ELSE 2 END) AS mrank,
           min(dist_m) AS dist_m, avg(lon) AS lon, avg(lat) AS lat,
           any_value(county) AS county, any_value(town) AS town
    FROM best GROUP BY idx, lvl, bldg_uid
),
pick AS (
    SELECT * FROM per
    QUALIFY row_number() OVER (PARTITION BY idx ORDER BY (bldg_uid IS NULL), mrank, n DESC) = 1
)
SELECT pick.*, amb.n_town,
       b.floors_above AS b_floors, b.floors_below, b.structure AS b_structure, b.btype AS b_btype,
       b.year AS b_year, b.year_kind AS b_year_kind, b.source AS b_source
FROM pick JOIN amb USING (idx)
LEFT JOIN buildings b ON b.uid = pick.bldg_uid
"""

_TRADE_SQL = f"""
WITH m AS (
    SELECT q.idx, 2 AS tlvl, t.* FROM q JOIN trades t ON t.base = q.base AND {_TMATCH}
    UNION ALL
    SELECT q.idx, 3, t.* FROM q JOIN trades t
      ON q.base_main <> q.base AND t.base = q.base_main AND {_TMATCH}
),
best AS (SELECT * FROM m QUALIFY tlvl = min(tlvl) OVER (PARTITION BY idx)),
-- 總樓層眾數：平手取有記載所在樓層的、再取最近一筆成交、再取較高（與 derive.door_trade 相同，結果固定）
fl AS (
    SELECT idx, total_floors AS fl_floors FROM (
        SELECT idx, total_floors, count(*) AS k, count(floor) AS kf, max(trade_date) AS d FROM best
        WHERE total_floors IS NOT NULL GROUP BY ALL)
    QUALIFY row_number() OVER (PARTITION BY idx ORDER BY k DESC, kf DESC, d DESC NULLS LAST, total_floors DESC) = 1
)
SELECT idx, min(tlvl) AS tlvl, count(*) AS t_n,
       count(DISTINCT county || '/' || coalesce(town, '')) FILTER (WHERE town IS NOT NULL) AS t_ntown,
       any_value(fl.fl_floors) AS t_floors,
       count(DISTINCT total_floors) AS t_nfloors,
       mode(structure) AS t_structure, mode(btype) AS t_btype,
       max(built_year) AS t_year, max(trade_date) AS t_last,
       any_value(county) AS t_county, any_value(town) AS t_town,
       string_agg(DISTINCT source, '、') AS t_source
FROM best LEFT JOIN fl USING (idx) GROUP BY idx
"""

# 同棟：查詢門牌所在建物多邊形／同點位的其他門牌實價（derive.py 預先算好）
_SAME_SQL = f"""
SELECT q.idx, s.floors AS s_floors, s.n AS s_n, s.spread AS s_spread, s.method AS s_method, s.btype AS s_btype
FROM q JOIN same_bldg s ON s.base = q.base AND {_MATCH.format(t="s")}
QUALIFY row_number() OVER (PARTITION BY q.idx ORDER BY s.spread, s.n DESC) = 1
"""

# 同巷：同前綴（路巷弄）、同側（單雙號）、±6 號內，排除自己；至少 2 個鄰居且彼此差 ≤1 層
# 留一驗證（2026-11-04，20 萬門牌）：可推估 54%，完全相符 92.7%，差一層內 98.2%
NB_WINDOW, NB_MIN, NB_SPREAD = 6, 2, 1
# 工廠／倉庫的推估準確率較低（留一驗證 2026-11-04）：同棟一致 82%（其他 96%）、
# 同巷 工廠 80%／倉庫 58%（全部 93%）→ 信心各降一級。廠辦（分層出售的工業大樓）與一般建物相當，不降。
# 同巷以「鄰居過半是工廠／倉庫」判斷
FACTORY_PAT = "工廠|倉庫"
_NB_SQL = f"""
WITH qq AS (
    SELECT idx, county, town, base,
           regexp_extract(base, '{DOOR_RE}', 1) AS prefix,
           TRY_CAST(regexp_extract(base, '{DOOR_RE}', 2) AS INTEGER) AS num
    FROM q WHERE regexp_matches(base, '{DOOR_RE}')
)
SELECT qq.idx, round(median(d.floors)) AS nb_floors, count(*) AS nb_n,
       max(d.floors) - min(d.floors) AS nb_spread,
       avg(regexp_matches(coalesce(d.btype, ''), '{FACTORY_PAT}')::int) >= 0.5 AS nb_factory
FROM qq JOIN trade_doors d
  ON d.county = qq.county AND (qq.town = '' OR d.town = qq.town) AND d.prefix = qq.prefix
 AND d.num % 2 = qq.num % 2 AND abs(d.num - qq.num) <= {NB_WINDOW} AND d.base <> qq.base
GROUP BY qq.idx
HAVING count(*) >= {NB_MIN} AND max(d.floors) - min(d.floors) <= {NB_SPREAD}
"""

# 附近實價：門牌點 NEAR_M 公尺內最近 NEAR_K 個有實價的門牌（至少 NEAR_MIN 個），取總樓層眾數。
# 留一驗證（2026-11-10，現行邏輯查不到的 9,226 門牌，3/4 是透天）：全部一致 完全相符 91%、差一層內 97% → 中；
# 眾數占 70–99% 完全相符 64%、差一層內 83% → 低；不到 70% 只有 4 成，不推估
NEAR_M, NEAR_K, NEAR_MIN, NEAR_SHARE = 100, 7, 3, 0.7
_NEAR_SQL = f"""
WITH g AS (
    SELECT idx, county, lon, lat, floor(lat / {NEAR_GRID})::INTEGER AS gy, floor(lon / {NEAR_GRID})::INTEGER AS gx
    FROM qp
),
k AS (
    SELECT g.idx, p.floors,
           111320 * sqrt(power(p.lat - g.lat, 2) + power((p.lon - g.lon) * cos(radians(g.lat)), 2)) AS d
    FROM g JOIN trade_pts p
      ON p.county = g.county AND p.gy BETWEEN g.gy - 1 AND g.gy + 1 AND p.gx BETWEEN g.gx - 1 AND g.gx + 1
    WHERE d <= {NEAR_M}
    QUALIFY row_number() OVER (PARTITION BY g.idx ORDER BY d, p.floors) <= {NEAR_K}
),
m AS (SELECT idx, floors, count(*) AS c, min(d) AS dmin, sum(count(*)) OVER (PARTITION BY idx) AS n FROM k GROUP BY idx, floors)
SELECT idx, floors AS ar_floors, c AS ar_k, n AS ar_n FROM m WHERE n >= {NEAR_MIN}
QUALIFY row_number() OVER (PARTITION BY idx ORDER BY c DESC, dmin, floors DESC) = 1
"""


def _is_factory(*btypes) -> bool:
    return any(not _na(b) and re.search(FACTORY_PAT, str(b)) for b in btypes)


TP_PERMIT_SOURCE = "臺北市建照"

# OSM：門牌落在哪棟 OSM 建物（osm.py 預先算好）；之號沒有點位時退回主號
_OSM_SQL = f"""
SELECT q.idx, b.uid AS osm_uid, b.levels AS o_floors, b.name AS o_name
FROM q JOIN door_osm o ON o.base IN (q.base, q.base_main) AND {_MATCH.format(t="o")}
JOIN osm_buildings b ON b.uid = o.osm_uid
QUALIFY row_number() OVER (PARTITION BY q.idx ORDER BY (o.base = q.base) DESC, (b.levels IS NULL)) = 1
"""


# OSM 同棟：同一 OSM 外框內其他門牌的實價（osm.py 預先算好）
_SAME_OSM_SQL = f"""
SELECT q.idx, s.floors AS so_floors, s.n AS so_n, s.spread AS so_spread, s.btype AS so_btype
FROM q JOIN same_osm s ON s.base = q.base AND {_MATCH.format(t="s")}
QUALIFY row_number() OVER (PARTITION BY q.idx ORDER BY s.spread, s.n DESC) = 1
"""

# 人工校正（curated/floor_corrections.csv）：門牌直接指定，或門牌落在指定的 OSM 建物裡
_CORR_SQL = f"""
SELECT idx, c_floors, c_below, c_year, c_source FROM (
    SELECT q.idx, c.floors_above AS c_floors, c.floors_below AS c_below, c.year AS c_year, c.source AS c_source,
           0 AS pri
    FROM q JOIN corrections c ON c.base = q.base AND {_MATCH.format(t="c")}
    UNION ALL
    SELECT q.idx, c.floors_above, c.floors_below, c.year, c.source, 1
    FROM q JOIN door_osm o ON o.base = q.base AND {_MATCH.format(t="o")}
    JOIN corrections c ON c.osm_uid = o.osm_uid
) QUALIFY row_number() OVER (PARTITION BY idx ORDER BY pri) = 1
"""

# 使用執照：門牌直接比對；之號沒有使照時退回主號
_PERMIT_SQL = f"""
SELECT q.idx, p.floors_above AS p_floors, p.floors_below AS p_below, p.structure AS p_structure, p.year AS p_year,
       p.permit_no AS p_no, p.source AS p_source, (p.base = q.base) AS p_exact
FROM q JOIN permits p ON p.base IN (q.base, q.base_main) AND {_MATCH.format(t="p")} AND NOT p.small
QUALIFY row_number() OVER (PARTITION BY q.idx ORDER BY (p.base = q.base) DESC, p.year DESC) = 1
"""
# 使照樓層對實價完全相符（2026-11-04）：臺北 95.8%、臺南公寓大廈 94.6% → 高；新北、新竹市 91.6% → 中
PERMIT_HIGH = ("臺北市使用執照", "臺南市公寓大廈清冊")


def _optional(con: duckdb.DuckDBPyConnection, sql: str, cols: list[str]) -> pd.DataFrame:
    """選配的資料表（例如還沒跑 osm.py）不存在時回空表，不讓查詢失敗。"""
    try:
        return con.execute(sql).df()
    except duckdb.CatalogException:
        return pd.DataFrame(columns=cols)


COLUMNS = {
    "input": "輸入地址", "normalized": "標準化地址", "county": "縣市", "town": "鄉鎮市區",
    "floors_above": "地上樓層", "floors_below": "地下樓層", "unit_floor": "所在樓層",
    "structure": "構造", "btype": "建物型態", "year": "年份", "year_kind": "年份類型",
    "source": "資料來源", "level": "比對方式", "confidence": "信心", "note": "說明",
    "t_n": "實價筆數", "lon": "經度", "lat": "緯度",
    # 建物風險資訊（sitefloor/risk.py）
    "usage": "主要用途", "usage_floors": "各樓層用途", "usage_source": "用途來源", "zoning": "使用分區",
    "flood500": "淹水潛勢（24小時500mm）", "flood650": "淹水潛勢（24小時650mm）", "liquefaction": "土壤液化潛勢",
    "fault": "活動斷層地質敏感區", "landslide": "山崩與地滑地質敏感區", "debris": "土石流潛勢溪流影響範圍",
    "fire_station": "最近消防分隊", "fire_station_m": "消防分隊直線距離（公尺）",
    "hydrant_m": "最近消防栓直線距離（公尺）", "hydrants_100m": "100公尺內消防栓數",
}
RISK_HAZARDS = ["flood500", "flood650", "liquefaction", "fault", "landslide", "debris"]
NOT_IN_ZONE, NO_MAP = "未在範圍內", "此區無圖資"
_CLEAR_ON_AMBIGUOUS = ["town", "bldg_uid", "floors_above", "floors_below", "structure", "btype",
                       "year", "year_kind", "source", "lon", "lat", "t_n", "osm_uid"]


def _queries(texts: list[str]) -> pd.DataFrame:
    rows = []
    for i, t in enumerate(texts):
        lm = landmarks.resolve(t) if t is not None else None
        a = parse(lm[0] if lm else str(t) if t is not None else "")
        if not a.base.endswith("號"):  # 沒有門牌號就不比對（否則空字串會對到資料裡的殘缺門牌）
            rows.append({"idx": i, "input": t, "county": a.county, "town": a.town or "", "base": a.base,
                         "base_main": None, "full": None, "floor": None, "normalized": f"{a.county}{a.town}{a.base}"})
            continue
        rows.append({
            "idx": i, "input": t, "county": a.county, "town": a.town or "",
            "base": a.base, "base_main": re.sub(r"之\d+(?=號$)", "", a.base) if a.base.endswith("號") else a.base,
            "full": a.full, "floor": a.floor,
            "normalized": f"{a.county}{a.town}{a.full}", "landmark": lm[1] if lm else None,
        })
    cols = ["idx", "input", "county", "town", "base", "base_main", "full", "floor", "normalized", "landmark"]
    return pd.DataFrame(rows, columns=cols).astype({"floor": "Int16"})


def _na(x) -> bool:
    return x is None or pd.isna(x)


def _resolve(r, covered: set[str]) -> dict:
    """合併圖資與實價兩條線索，決定最終欄位、信心與說明。"""
    has_addr, has_trade = not _na(r.lvl), not _na(r.tlvl)
    # 門牌點位資料沒有、自己也沒有實價的門牌，同巷鄰居一致時仍可推估：宜蘭、南投、嘉義市的點位資料幾乎沒收，
    # 其他縣市也有舊門牌不在點位裡（留一驗證 2026-11-09：這類門牌同巷推估完全相符 92%，和一般門牌相同）。
    # 沒寫鄉鎮市區時鄰居可能跨區，不推估
    nb_only = not has_addr and not has_trade and not _na(r.nb_floors) and isinstance(r.town_q, str) and r.town_q
    if not has_addr and not has_trade and not nb_only:
        if r.county_q and r.county_q not in covered:
            return dict(confidence="無", note=f"{r.county_q}尚未建置")
        if not str(r.base).endswith("號"):
            return dict(confidence="無", note="地址缺少門牌號")
        return dict(confidence="無", note="查無此門牌")
    if (has_addr and r.n_town > 1) or (has_trade and r.t_ntown > 1):
        n = int(max(r.n_town if has_addr else 0, r.t_ntown if has_trade else 0))
        return dict(confidence="待確認", note=f"{n} 個行政區都有此門牌，請補鄉鎮市區", ambiguous=True)

    b_attr = r.mrank == 0 or _na(r.osm_uid)  # 門牌已落在 OSM 建物內時，不跨巷借用鄰近（nearest）建照外框的年份與構造
    out = dict(
        county=r.county if has_addr else r.t_county, town=r.town if has_addr else r.t_town,
        floors_below=next((x for x in (r.c_below, r.p_below, r.floors_below if b_attr else None) if not _na(x)), None),
        structure=next((x for x in (r.t_structure, r.p_structure, r.b_structure if b_attr else None) if not _na(x)), None),
        btype=r.t_btype if not _na(r.t_btype) else (r.b_btype if b_attr else None),
        year=next((x for x in (r.c_year, r.t_year, r.p_year, r.b_year if b_attr else None) if not _na(x)), None),
        year_kind="竣工年（人工校正）" if not _na(r.c_year) else "建築完成年" if not _na(r.t_year) else (
            "竣工年（使照）" if not _na(r.p_year) else None if (not b_attr or _na(r.b_year)) else r.b_year_kind),
        level=LEVEL_NAME.get(min((x for x in (r.lvl, r.tlvl) if not _na(x)), default=2)),
    )
    b, t = r.b_floors, r.t_floors
    notes = ["門牌點位資料沒有此門牌，請確認門牌存在"] if nb_only else []
    if not _na(t) and r.t_nfloors > 1:
        notes.append("實價登錄紀錄的總樓層不一致，取眾數")
    same_ok = not _na(r.s_floors) and r.s_spread == 0
    # 依實測準確率排序（2026-11-04 驗證，見 scripts/validate*.py）：
    #   人工校正 > 自己的實價 > 同棟／同點位一致（94–98%）> OSM 同棟一致（97%）> 使用執照（92–96%）> 臺北建照套繪圖、門牌點在外框內（96%）> 同巷（93%）
    #   > OSM 樓層（72%）> 同棟不一致（62%）> 其他建物圖（臺中 53%）
    #   > 附近 100 公尺實價（以上都沒有的門牌，多為透天：一致 91%、七成以上 64%；2026-11-10）
    tp_ok = (not _na(b) and str(r.b_source).startswith(TP_PERMIT_SOURCE) and r.mrank == 0 and r.lvl in (1, 2))
    if not _na(r.c_floors):
        floors, src, conf = r.c_floors, "人工校正", "高"
        notes.append(f"人工校正（依據：{r.c_source or '未註明'}）")
        if not _na(t) and int(t) != int(r.c_floors):
            notes.append(f"實價登錄記 {int(t)} 層，已校正")
    elif not _na(t):
        floors, src = t, r.t_source
        if not _na(b) and int(b) != int(t):
            notes.append(f"建物圖 {int(b)} 層、實價登錄 {int(t)} 層，採實價登錄")
        conf = "高" if r.tlvl == 2 else "中"
        if r.tlvl == 3:
            notes.append("用主號的實價登錄推估")
    elif same_ok:
        floors, src, conf = r.s_floors, f"推估：{r.s_method}", "高"
        notes.append(f"本門牌無實價，取{r.s_method} {int(r.s_n)} 個門牌的實價（一致）")
        if _is_factory(r.b_btype, r.s_btype):
            conf = "中"
            notes.append("工廠／倉庫的同棟推估準確率約 8 成，信心降一級")
    elif not _na(r.so_floors) and r.so_spread == 0:
        floors, src, conf = r.so_floors, "推估：同棟（OSM 外框）", "高"
        notes.append(f"本門牌無實價，取同一棟（OSM 建物外框）{int(r.so_n)} 個門牌的實價（一致）")
        if _is_factory(r.b_btype, r.so_btype):
            conf = "中"
            notes.append("工廠／倉庫的同棟推估準確率約 8 成，信心降一級")
    elif not _na(r.p_floors):
        floors, src = r.p_floors, r.p_source
        conf = "高" if r.p_source in PERMIT_HIGH and r.p_exact else "中"
        notes.append(f"本門牌無實價，取使用執照 {r.p_no}" + ("" if r.p_exact else "（主號）"))
        if not _na(b) and int(b) != int(r.p_floors):
            notes.append(f"建物圖 {int(b)} 層、使照 {int(r.p_floors)} 層，採使照")
    elif tp_ok:
        # 臺北建照套繪圖對實價完全相符 95.8%（2026-11-04，6,676 門牌）；但一張建照一個外框，
        # 同建照裡較低的附屬建物（例：富邦 A25 美術館）會拿到最高樓層，所以給「中」不給「高」
        floors, src, conf = b, r.b_source, "中"
        notes.append("本門牌無實價，取臺北市建照套繪圖（對實價驗證完全相符約 96%；同一建照的低矮附屬建物可能被高估）")
    elif not _na(r.nb_floors):
        floors, src, conf = r.nb_floors, "推估：同巷鄰近門牌", "中"
        notes.append(f"本門牌無實價，取同側 ±{NB_WINDOW} 號內 {int(r.nb_n)} 個門牌的實價")
        if (not _na(r.nb_factory) and bool(r.nb_factory)) or _is_factory(r.b_btype):
            conf = "低"
            notes.append("工廠／倉庫的同巷推估準確率約 6–8 成，信心降一級")
    elif not _na(r.o_floors):
        floors, src, conf = r.o_floors, "OpenStreetMap", "低"
        name = f"「{r.o_name}」" if isinstance(r.o_name, str) and r.o_name else ""
        notes.append(f"本門牌無實價，取所在 OSM 建物{name}標註的樓層；對實價驗證完全相符約 7 成、差一層內約 9 成")
    elif not _na(r.s_floors):
        floors, src, conf = r.s_floors, f"推估：{r.s_method}", "低"
        notes.append(f"取{r.s_method} {int(r.s_n)} 個門牌實價的眾數（彼此差 {int(r.s_spread)} 層）")
    elif not _na(b):
        floors, src, conf = b, r.b_source, "低"
        if r.lvl == 3:
            notes.append("用主號推估（之號門牌不在點位資料中）")
        elif r.mrank == 1:
            notes.append(f"門牌點不在建物內，取 {r.dist_m:.0f}m 內最近建物")
        notes.append("僅建物圖資：臺中建物圖對實價完全相符約 5 成、差一層內約 9 成，高樓常被低估"
                     if not str(r.b_source).startswith(TP_PERMIT_SOURCE) else "門牌點不在臺北建照外框內，準確度較低")
    elif not _na(r.ar_floors) and r.ar_k / r.ar_n >= NEAR_SHARE:
        floors, src = r.ar_floors, f"推估：附近 {NEAR_M} 公尺實價"
        k, n = int(r.ar_k), int(r.ar_n)
        conf = "中" if k == n else "低"
        notes.append(f"本門牌無實價，取附近 {NEAR_M} 公尺內最近 {n} 個有實價的門牌，"
                     + (f"都是 {int(floors)} 層" if k == n else f"其中 {k} 個是 {int(floors)} 層")
                     + "（對實價驗證完全相符約 "
                     + ("9 成）" if k == n else "6 成、差一層內約 8 成）"))
    else:
        floors, src = None, None
        conf = "無"
        notes.append("建物圖資缺樓層數" if not _na(r.bldg_uid) else "門牌存在，但無實價登錄也無法推估")
    out.update(floors_above=floors, source=src)
    out.update(confidence=conf, note="；".join(notes))
    return out


def lookup_many(con: duckdb.DuckDBPyConnection, texts: list[str]) -> pd.DataFrame:
    q = _queries(texts)
    con.register("q", q.loc[q.base.str.endswith("號"), ["idx", "county", "town", "base", "base_main", "full", "floor"]])
    hit = con.execute(_SQL).df()
    trade = con.execute(_TRADE_SQL).df()
    same = con.execute(_SAME_SQL).df()
    nb = con.execute(_NB_SQL).df()
    osm = _optional(con, _OSM_SQL, ["idx", "osm_uid", "o_floors", "o_name"])
    corr = _optional(con, _CORR_SQL, ["idx", "c_floors", "c_below", "c_year", "c_source"])
    same_osm = _optional(con, _SAME_OSM_SQL, ["idx", "so_floors", "so_n", "so_spread", "so_btype"])
    permit = _optional(con, _PERMIT_SQL, ["idx", "p_floors", "p_below", "p_structure", "p_year", "p_no", "p_source",
                                          "p_exact"])
    con.unregister("q")
    near = pd.DataFrame(columns=["idx", "ar_floors", "ar_k", "ar_n"])
    pts = hit.loc[hit.lon.notna() & hit.county.notna(), ["idx", "county", "lon", "lat"]]
    if len(pts):
        con.register("qp", pts)
        near = _optional(con, _NEAR_SQL, list(near.columns))
        con.unregister("qp")
    covered = {c for (c,) in con.execute(
        "SELECT county FROM addresses UNION SELECT county FROM trades").fetchall()}
    df = (q.merge(hit, on="idx", how="left", suffixes=("_q", ""))
          .merge(trade, on="idx", how="left").merge(same, on="idx", how="left").merge(nb, on="idx", how="left")
          .merge(osm, on="idx", how="left").merge(permit, on="idx", how="left")
          .merge(same_osm, on="idx", how="left").merge(corr, on="idx", how="left")
          .merge(near, on="idx", how="left"))
    df["unit_floor"] = df.floor
    resolved = pd.DataFrame([_resolve(r, covered) for r in df.itertuples()], index=df.index)
    for c in ("county", "town"):
        resolved[c] = resolved.get(c, pd.Series(index=df.index, dtype=object)).fillna(df[f"{c}_q"])
    df = df.drop(columns=[c for c in resolved.columns if c in df.columns]).join(resolved)
    # 輸入沒寫縣市（或只寫了多縣市同名的區）時，標準化地址補上查到的縣市、區
    fill = df.county.notna() & df.county_q.fillna("").eq("") & df.normalized.notna()
    df.loc[fill, "normalized"] = [
        c + t + (n[len(t):] if t and n.startswith(t) else n)
        for c, t, n in zip(df.county[fill], df.town[fill].fillna(""), df.normalized[fill])]
    # 地標名稱查詢：說明欄註明換成了哪個門牌
    lm = df.landmark.notna()
    df.loc[lm, "note"] = [
        f"地標「{n}」→ {a}" + (f"；{x}" if isinstance(x, str) and x else "")
        + ("（公共建物很少買賣，實價登錄常沒有資料）" if c == "無" else "")
        for n, a, x, c in zip(df.landmark[lm], df.normalized[lm], df.note[lm], df.confidence[lm])]
    # 多個行政區都有此門牌時，不給任何一棟的結果，避免被誤用
    if "ambiguous" in df:
        df.loc[df.ambiguous.fillna(False).astype(bool), [c for c in _CLEAR_ON_AMBIGUOUS if c in df]] = None
    df = _add_risk(con, df)
    for c in COLUMNS:
        if c not in df:
            df[c] = None
    return df.sort_values("idx").reset_index(drop=True)


_PERMIT_USE_SQL = """
SELECT k.i, p.usage AS p_usage, p.usage_floors AS p_usage_floors, p.zoning AS p_zoning FROM rk k
JOIN permits p ON p.county = k.county AND p.town = k.town AND p.base IN (k.base, k.base_main)
WHERE p.usage IS NOT NULL AND NOT p.small
QUALIFY row_number() OVER (PARTITION BY k.i ORDER BY (p.base = k.base) DESC, p.year DESC) = 1"""


def _add_usage(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    """使用性質：使用執照核准用途（臺北逐層、新北、新竹市）優先，其次實價登錄的主要用途眾數。
    使照是整棟的核准用途（可分到每一層），實價只看得到成交那幾戶。"""
    p = _optional(con, _PERMIT_USE_SQL, ["i", "p_usage", "p_usage_floors", "p_zoning"]).set_index("i")
    for c in ("usage", "zoning"):
        if c not in df:
            df[c] = None
    has = df.index.isin(p.index)
    df["usage_source"] = None
    df.loc[df.usage.notna(), "usage_source"] = "實價登錄"
    df["usage_floors"] = None
    if has.any():
        rows = p.loc[df.index[has]]
        df.loc[has, "usage"] = rows.p_usage.to_numpy()
        df.loc[has, "usage_floors"] = rows.p_usage_floors.to_numpy()
        df.loc[has, "usage_source"] = "使用執照"
        z = has & df.index.isin(p.index[p.p_zoning.notna()])
        df.loc[z, "zoning"] = p.loc[df.index[z], "p_zoning"].to_numpy()
    return df


def _add_risk(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    """建物風險資訊：門牌（找不到用主號）的災害潛勢與使用性質。
    災害欄位：在範圍內＝等級／名稱；不在範圍內＝「未在範圍內」；該縣市沒有圖資＝「此區無圖資」；沒有座標＝空白。"""
    keys = pd.DataFrame({"i": df.index, "county": df.county, "town": df.town,
                         "base": df.base_q if "base_q" in df else df.base, "base_main": df.base_main})
    con.register("rk", keys)
    try:
        cov = con.execute("SELECT kind, county FROM risk_coverage").fetchall()
    except duckdb.CatalogException:
        df = _add_usage(con, df)
        con.unregister("rk")
        return df
    hit = con.execute("""
        SELECT k.i, r.* EXCLUDE (county, town, base) FROM rk k
        JOIN door_risk r ON r.county = k.county AND r.town = k.town
             AND r.base IN (k.base, k.base_main)
        QUALIFY row_number() OVER (PARTITION BY k.i ORDER BY (r.base = k.base) DESC) = 1""").df()
    df = df.drop(columns=[c for c in hit.columns if c in df and c != "i"]).join(hit.set_index("i"))
    df = _add_usage(con, df)
    con.unregister("rk")
    covered = {k: {c for kk, c in cov if kk == k} for k in RISK_HAZARDS}
    has_xy = df["lon"].notna() if "lon" in df else pd.Series(False, index=df.index)
    for k in RISK_HAZARDS:
        if k not in df:
            df[k] = None
        empty = df[k].isna() & has_xy
        df.loc[empty, k] = [NOT_IN_ZONE if c in covered[k] else NO_MAP for c in df.county[empty]]
    return df


def lookup(con: duckdb.DuckDBPyConnection, text: str) -> dict:
    r = lookup_many(con, [text]).iloc[0]
    out = {k: (None if _na(r[k]) else r[k]) for k in COLUMNS}
    out["bldg_uid"] = None if _na(r.bldg_uid) else r.bldg_uid
    out["osm_uid"] = None if _na(r.osm_uid) else r.osm_uid
    for k in ("floors_above", "floors_below", "unit_floor", "year", "t_n", "fire_station_m", "hydrant_m", "hydrants_100m"):
        if out[k] is not None:
            out[k] = int(out[k])
    # 其餘 numpy 數值轉成 Python 型別（FastAPI 不會序列化 numpy.int32）
    return {k: (v.item() if hasattr(v, "item") else v) for k, v in out.items()}


# 同一棟建物有多個門牌時，災害潛勢取最嚴重的（在範圍內優先；淹水取最深、液化取最高）
_SEVERITY = {"flood500": ["0.3-0.5", "0.5-1.0", "1.0-2.0", "2.0-3.0", ">3.0"],
             "flood650": ["0.3-0.5", "0.5-1.0", "1.0-2.0", "2.0-3.0", ">3.0"],
             "liquefaction": ["低潛勢", "中潛勢", "高潛勢"]}
RISK_FIELDS = [*RISK_HAZARDS, "usage", "usage_floors", "usage_source", "zoning", "fire_station", "fire_station_m", "hydrant_m", "hydrants_100m"]


def building_risk(con: duckdb.DuckDBPyConnection, doors: pd.DataFrame) -> dict:
    """一棟建物的建物風險資訊（doors：棟內門牌 county, town, base, lon）。沒有門牌就回空 dict。"""
    if doors.empty:
        return {}
    df = doors.assign(base_main=doors.base).reset_index(drop=True)
    df = _add_risk(con, df)
    out: dict = {}
    for k in RISK_HAZARDS:
        vals = [v for v in df.get(k, pd.Series(dtype=object)).dropna() if v not in (NOT_IN_ZONE, NO_MAP)]
        if vals:
            order = _SEVERITY.get(k)
            out[k] = max(vals, key=lambda v: order.index(v) if order and v in order else 0)
        elif k in df and df[k].notna().any():
            out[k] = NOT_IN_ZONE if (df[k] == NOT_IN_ZONE).any() else NO_MAP
        else:
            out[k] = None
    # 使用性質：棟內有使照用途的門牌優先（整棟核准用途），其次實價登錄眾數
    src = df[df.usage_source == "使用執照"] if "usage_source" in df and (df.usage_source == "使用執照").any() else df
    for k in ("usage", "zoning"):
        m = src[k].dropna().mode() if k in src else pd.Series(dtype=object)
        out[k] = m.iloc[0] if len(m) else None
    pick = src[src.usage == out["usage"]] if out["usage"] is not None else src.iloc[0:0]
    out["usage_source"] = pick.usage_source.iloc[0] if len(pick) else None
    floors = pick.usage_floors.dropna() if len(pick) else pd.Series(dtype=object)
    out["usage_floors"] = floors.iloc[0] if len(floors) else None
    if "fire_station_m" in df and df.fire_station_m.notna().any():
        i = df.fire_station_m.astype(float).idxmin()
        out["fire_station"], out["fire_station_m"] = df.fire_station[i], int(df.fire_station_m[i])
    for k, f in (("hydrant_m", "min"), ("hydrants_100m", "max")):
        out[k] = int(getattr(df[k].dropna().astype(int), f)()) if k in df and df[k].notna().any() else None
    return {k: (v.item() if hasattr(v, "item") else v) for k, v in out.items()}
