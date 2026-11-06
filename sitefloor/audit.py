"""正確性檢查：依 building_tiers 的級別挑建物，比對各資料來源，列出疑似有錯、需要查證的建物。

檢查項目（代碼：嚴重度 3 高 / 2 中 / 1 低）：
- overlap_drawn（3）：地圖上和另一個立體大面積重疊（同一棟被畫兩次）
- floors_conflict（2；差 5 層以上為 3）：實價、使照、OSM、市府建物圖的樓層任兩個差 2 層以上
- floors_unknown（2）：第 1、2 級卻查不到樓層
- year_suspect（2）：使照或建照年比棟內實價的建築完成年晚 YEAR_GAP 年以上（後來改建、增建的執照被當成興建年）
- height_ratio（1）：OSM 高度 ÷ 樓層 不在 RATIO_MIN–RATIO_MAX 公尺（樓層或高度有一個錯）
- permit_small（1）：棟內門牌的使照因建築面積太小沒有採用（確認排除是否正確）
- stale_check（1）：人工校正超過查證期限（第 1 級 STALE_DAYS[1] 天、第 2 級 STALE_DAYS[2] 天）
人工校正在期限內的建物，只檢查 overlap_drawn。

範圍（--scope）：
- auto：每週排程用。第 1 級全部＋查詢熱度前 POPULAR_TOP；每月第一週加第 2 級；第 3、4 級依縣市輪流
  （COUNTY_BATCHES 批，輪到哪批記在 data/state/audit_rotation.json）
- tier1、tier2、all、county:臺北市（可用逗號列多個縣市）
輸出：data/reports/audit-<日期>.md（摘要）、data/reports/audit_queue.csv（待查清單，依級別 → 查詢熱度 → 嚴重度排序）
用法：uv run python -m sitefloor.audit [--scope auto]   （tiers 之後）
"""

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import shapely

from sitefloor.db import connect
from sitefloor.lookup import TP_PERMIT_SOURCE
from sitefloor.models import MODELS
from sitefloor.render import _inter_area, resolve_overlaps

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "data" / "reports"
ROTATION_FILE = ROOT / "data" / "state" / "audit_rotation.json"
POPULARITY_FILE = ROOT / "data" / "state" / "popularity.json"
SEVERITY = {"overlap_drawn": 3, "floors_conflict": 2, "floors_unknown": 2, "year_suspect": 2,
            "height_ratio": 1, "permit_small": 1, "stale_check": 1}
CONFLICT_GAP, CONFLICT_HIGH = 2, 5
YEAR_GAP = 5
RATIO_MIN, RATIO_MAX = 2.5, 7.0
OVERLAP_SHARE = 0.5  # 兩個立體重疊面積超過較小者的這個比例 → 同一棟畫兩次
STALE_DAYS = {1: 365, 2: 730}
POPULAR_TOP = 500
COUNTY_BATCHES = 13
COUNTIES = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市", "基隆市", "新竹市", "新竹縣", "苗栗縣", "彰化縣",
            "南投縣", "雲林縣", "嘉義市", "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"]


def county_batches(sizes: dict[str, int], n: int = COUNTY_BATCHES) -> list[list[str]]:
    """縣市分 n 批、每批建物數盡量平均（大的先放進目前最小的一批）。"""
    batches: list[list[str]] = [[] for _ in range(n)]
    load = [0] * n
    for c in sorted(COUNTIES, key=lambda c: -sizes.get(c, 0)):
        i = load.index(min(load))
        batches[i].append(c)
        load[i] += sizes.get(c, 0)
    return [b for b in batches if b]


def popularity() -> dict[str, int]:
    try:
        return json.loads(POPULARITY_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def pick_targets(tiers: pd.DataFrame, scope: str, today: dt.date,
                 pop: dict[str, int]) -> tuple[pd.DataFrame, str, dict | None]:
    """依範圍挑要檢查的建物，回傳 (建物, 範圍說明, 縣市輪流的下一個狀態)。輪流狀態在檢查跑完才寫入。"""
    if scope == "all":
        return tiers, "全部", None
    if scope == "tier1":
        return tiers[tiers.tier == 1], "第 1 級", None
    if scope == "tier2":
        return tiers[tiers.tier == 2], "第 2 級", None
    if scope.startswith("county:"):
        cs = scope.split(":", 1)[1].split(",")
        return tiers[tiers.county.isin(cs)], "、".join(cs), None
    if scope != "auto":
        raise SystemExit(f"不認得的範圍：{scope}")
    hot = sorted(pop, key=lambda u: -pop[u])[:POPULAR_TOP]
    mask = (tiers.tier == 1) | tiers.uid.isin(hot)
    desc = ["第 1 級", f"查詢熱度前 {POPULAR_TOP}"] if hot else ["第 1 級"]
    if today.day <= 7:
        mask |= tiers.tier == 2
        desc.append("第 2 級（每月第一週）")
    batches = county_batches(tiers.groupby("county").size().to_dict())
    try:
        i = json.loads(ROTATION_FILE.read_text()).get("next", 0) % len(batches)
    except (FileNotFoundError, ValueError):
        i = 0
    mask |= tiers.county.isin(batches[i])
    desc.append(f"縣市輪流第 {i + 1}/{len(batches)} 批：{'、'.join(batches[i])}")
    return tiers[mask], "；".join(desc), {"next": i + 1, "last": today.isoformat(), "batch": batches[i]}


def _sources(con) -> pd.DataFrame:
    """每棟建物各來源的樓層、高度、校正、年份（OSM 建物與市府外框）。"""
    osm = con.execute("""
        SELECT b.uid, b.trade_floors AS f_trade, b.permit_floors AS f_permit, b.levels AS f_osm,
               b.height AS osm_height, b.curated_floors AS f_curated, b.curated_height,
               c.checked AS c_checked
        FROM osm_buildings b
        LEFT JOIN (SELECT osm_uid, max(checked) AS checked FROM corrections WHERE osm_uid IS NOT NULL GROUP BY 1) c
          ON c.osm_uid = b.uid""").df()
    # 臺中建物圖的年份是圖資更新年，不是興建或執照年，不比
    city = con.execute("""SELECT uid, floors_above AS f_city, CASE WHEN year_kind = '建照年' THEN year END AS city_year,
                          source AS city_source FROM buildings""").df()
    # 年份：棟內門牌實價的最早建築完成年 vs 採用的使照年
    years = con.execute(f"""
        WITH d AS (SELECT osm_uid AS uid, county, town, base FROM door_osm
                   UNION ALL SELECT bldg_uid, county, town, base FROM addresses WHERE bldg_uid IS NOT NULL)
        SELECT d.uid, min(t.built_year) AS trade_year,
               max(p.year) FILTER (WHERE NOT p.small) AS permit_year,
               bool_or(coalesce(p.small, false)) AS permit_small
        FROM d LEFT JOIN trades t ON t.county = d.county AND t.town = d.town AND t.base = d.base
                              AND t.built_year BETWEEN 1900 AND {dt.date.today().year}
               LEFT JOIN permits p ON p.county = d.county AND p.town = d.town AND p.base = d.base
        GROUP BY 1""").df()
    addr = con.execute("""
        SELECT uid, any_value(county || town || base) AS address FROM (
            SELECT osm_uid AS uid, county, town, base FROM door_osm
            UNION ALL SELECT bldg_uid, county, town, base FROM addresses WHERE bldg_uid IS NOT NULL) GROUP BY 1""").df()
    out = pd.concat([osm, city], ignore_index=True).merge(years, on="uid", how="left").merge(addr, on="uid", how="left")
    return out


def overlaps(con, targets: set[str]) -> dict[str, str]:
    """要檢查的建物附近，照地圖的規則決定畫哪些外框，再找互相大面積重疊的立體。回傳 {uid: 和誰重疊}。"""
    osm_rows = con.execute("""
        SELECT uid, coalesce(curated_floors, trade_floors, permit_floors, levels), height, name, wkb,
               curated_floors IS NOT NULL FROM osm_buildings WHERE wkb IS NOT NULL""").fetchall()
    city_rows = con.execute("SELECT uid, floors_above, source, wkb FROM buildings WHERE wkb IS NOT NULL").fetchall()
    og = shapely.from_wkb([r[4] for r in osm_rows])
    cg = shapely.from_wkb([r[3] for r in city_rows])
    # 只處理目標建物附近（外框外擴約 50 m）的外框，全國一起算太慢
    tgt = [g for r, g in zip(osm_rows, og) if r[0] in targets] + [g for r, g in zip(city_rows, cg) if r[0] in targets]
    if not tgt:
        return {}
    area = shapely.union_all(shapely.envelope(shapely.buffer(shapely.envelope(tgt), 0.0005)))
    near_o = [i for i in shapely.STRtree(og).query(area, predicate="intersects")]
    near_c = [i for i in shapely.STRtree(cg).query(area, predicate="intersects")]
    osm = [(osm_rows[i][0], osm_rows[i][1], osm_rows[i][2], osm_rows[i][3], og[i]) for i in near_o]
    trusted = {osm_rows[i][0] for i in near_o if osm_rows[i][5] or osm_rows[i][0] in MODELS}
    city, drop = resolve_overlaps(osm, [(city_rows[i][0], city_rows[i][1], city_rows[i][2], cg[i]) for i in near_c],
                                  set(MODELS), trusted, TP_PERMIT_SOURCE)
    drawn = [(u, g) for k, (u, _, _, _, g) in enumerate(osm) if k not in drop and u not in MODELS] + \
            [(u, g) for u, _, g in city]
    tree = shapely.STRtree([g for _, g in drawn])
    out: dict[str, str] = {}
    for i, (u, g) in enumerate(drawn):
        if u not in targets or not g.area:
            continue
        for j in tree.query(g, predicate="intersects"):
            if j == i:
                continue
            v, h = drawn[j]
            if h.area and _inter_area(g, h) / min(g.area, h.area) >= OVERLAP_SHARE:
                out[u] = v
                break
    return out


def check(r: dict, today: dt.date, overlap_with: str | None) -> list[tuple[str, str]]:
    """一棟建物的問題清單 [(代碼, 說明)]。r：building_tiers 一列＋_sources 欄位。"""
    issues: list[tuple[str, str]] = []
    if overlap_with:
        issues.append(("overlap_drawn", f"和 {overlap_with} 重疊"))
    checked = r.get("c_checked")
    tier = r.get("tier")
    if checked:
        age = (today - dt.date.fromisoformat(str(checked)[:10])).days
        limit = STALE_DAYS.get(tier)
        if limit and age > limit:
            issues.append(("stale_check", f"人工校正 {checked} 查證，已 {age} 天"))
        else:
            return issues  # 校正在期限內，其他來源的矛盾不再列
    vals = {k: r.get(f) for k, f in (("實價", "f_trade"), ("使照", "f_permit"), ("OSM", "f_osm"), ("建物圖", "f_city"))}
    vals = {k: int(v) for k, v in vals.items() if v is not None and not pd.isna(v)}
    if len(vals) >= 2 and max(vals.values()) - min(vals.values()) >= CONFLICT_GAP:
        gap = max(vals.values()) - min(vals.values())
        issues.append(("floors_conflict", "、".join(f"{k} {v}" for k, v in vals.items())
                       + ("（差很多）" if gap >= CONFLICT_HIGH else "")))
    if tier in (1, 2) and r.get("floors") is None and r.get("uid") not in MODELS:
        issues.append(("floors_unknown", "查不到樓層"))
    py, ty = r.get("permit_year") or r.get("city_year"), r.get("trade_year")
    if py and ty and py - ty >= YEAR_GAP:
        issues.append(("year_suspect", f"執照年 {int(py)}、實價建築完成年 {int(ty)}"))
    h, f = r.get("osm_height"), r.get("floors")
    if h and f and not r.get("curated_height") and not RATIO_MIN <= h / f <= RATIO_MAX:
        issues.append(("height_ratio", f"OSM 高 {h:g} m ÷ {int(f)} 層 = {h / f:.1f} m"))
    if r.get("permit_small"):
        issues.append(("permit_small", "棟內有使照因建築面積太小未採用"))
    return issues


def run(scope: str = "auto", today: dt.date | None = None) -> pd.DataFrame:
    t0 = time.time()
    today = today or dt.date.today()
    con = connect()
    tiers = con.execute("SELECT * FROM building_tiers").df()
    pop = popularity()
    targets, desc, rotation = pick_targets(tiers, scope, today, pop)
    src = _sources(con)
    d = targets.merge(src, on="uid", how="left")
    ov = overlaps(con, set(d.uid))
    rows = []
    for r in d.to_dict("records"):
        r = {k: (None if not isinstance(v, (list, dict)) and pd.isna(v) else v) for k, v in r.items()}
        issues = check(r, today, ov.get(r["uid"]))
        if issues:
            rows.append({"tier": r["tier"], "popularity": pop.get(r["uid"], 0),
                         "severity": max(SEVERITY[c] for c, _ in issues), "uid": r["uid"], "name": r.get("name"),
                         "county": r.get("county"), "town": r.get("town"), "address": r.get("address"),
                         "floors": r.get("floors"), "issues": "、".join(c for c, _ in issues),
                         "detail": "；".join(m for _, m in issues), "reasons": r.get("reasons")})
    q = pd.DataFrame(rows, columns=["tier", "popularity", "severity", "uid", "name", "county", "town", "address",
                                    "floors", "issues", "detail", "reasons"])
    q = q.sort_values(["tier", "popularity", "severity"], ascending=[True, False, False])
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    q.to_csv(REPORT_DIR / "audit_queue.csv", index=False, encoding="utf-8-sig")
    report(q, len(d), desc, today, time.time() - t0)
    if rotation:
        ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        ROTATION_FILE.write_text(json.dumps(rotation, ensure_ascii=False))
    return q


def report(q: pd.DataFrame, n_checked: int, desc: str, today: dt.date, secs: float):
    lines = [f"# 正確性檢查 {today}", "", f"範圍：{desc}；檢查 {n_checked:,} 棟，待查 {len(q):,} 棟（{secs:.0f}s）", "",
             "| 級 | 待查 | " + " | ".join(SEVERITY) + " |", "|---|---|" + "---|" * len(SEVERITY)]
    for tier, g in q.groupby("tier"):
        cnt = [int(g.issues.str.contains(c).sum()) for c in SEVERITY]
        lines.append(f"| {tier} | {len(g):,} | " + " | ".join(f"{c:,}" for c in cnt) + " |")
    lines += ["", "## 前 30 筆", "", "| 級 | 建物 | 地址 | 問題 |", "|---|---|---|---|"]
    for r in q.head(30).itertuples():
        lines.append(f"| {r.tier} | {r.name or r.uid} | {r.address or ''} | {r.detail} |")
    (REPORT_DIR / f"audit-{today}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:8 + q.tier.nunique()]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="auto")
    run(ap.parse_args().scope)


if __name__ == "__main__":
    main()
