"""本機查詢介面。啟動：uv run uvicorn app.server:app --port 8765"""
import io
import json
import math
import os
import urllib.request
import re
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import duckdb
import pandas as pd
import shapely
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from sitefloor import feedback, popularity
from sitefloor.db import connect
from sitefloor.lookup import COLUMNS, TP_PERMIT_SOURCE, building_risk, lookup, lookup_many
from sitefloor.models import MODELS, REPLACED_PARTS
from sitefloor.render import FLOOR_M, extrude_height, part_extent, resolve_overlaps
from sitefloor.suggest import Suggester, split_base

STATIC = Path(__file__).parent / "static"
MAX_BUILDINGS = 6000
PILLAR_M = 10  # 實價門牌方柱邊長（公尺）
OSM_TABLE = ("osm:", "cur:")  # 存在 osm_buildings 的建物：OSM 外框、人工補畫的外框（curated/buildings.geojson）
PILLAR_NEAR_M = 25  # 門牌點常落在建物外框外的騎樓、路邊：這個距離內已有樓層相同（±1）的建物就不畫方柱

app = FastAPI(title="建物查詢平台")

# ── 查詢次數限制（公開部署時避免被大量抓取）：每個 IP 在時間窗內的次數上限 ──
RATE_LIMITS = {  # 路徑前綴：(次數, 秒)；依序比對，越特定的放越前面
    "/api/batch": (5, 3600),
    "/api/feedback": (5, 3600),
    "/api/": (120, 60),
}
BATCH_MAX_BYTES = 4 * 1024 * 1024  # Vercel 函式的請求上限是 4.5 MB
BATCH_MAX_ROWS = 2000
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # 部署在反向代理後面時，真實 IP 在 X-Vercel-Forwarded-For／Fly-Client-IP／X-Real-IP／X-Forwarded-For
    h = request.headers
    return (h.get("x-vercel-forwarded-for") or h.get("fly-client-ip") or h.get("x-real-ip")
            or h.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    rule = next(((p, lim) for p, lim in RATE_LIMITS.items() if path.startswith(p)), None)
    if rule:
        prefix, (n, window) = rule
        q = _hits[(_client_ip(request), prefix)]
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= n:
            wait = int(window - (now - q[0])) + 1
            return JSONResponse({"detail": f"查詢太頻繁，請 {wait} 秒後再試"}, status_code=429,
                                headers={"Retry-After": str(wait)})
        q.append(now)
    return await call_next(request)
con = connect()


def _cur():
    # DuckDB 連線不可跨執行緒共用，每個請求開 cursor
    return con.cursor()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# 圖示（LOGO 縮圖，app/static/*.png）
# 分頁圖示：LOGO 縮圖（favicon.ico 含 16／32／48／64px、favicon.png 64px，都由 logo.png 轉出）
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/{name}.png", include_in_schema=False)
def static_png(name: str):
    if name not in ("logo", "favicon", "apple-touch-icon"):
        raise HTTPException(404)
    return FileResponse(STATIC / f"{name}.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


# ── 樓層回報：使用者覺得樓層有誤時送出 → 私人 repo 開 GitHub Issue（會寄信）＋ ntfy 推播；審核見 sitefloor.review ──
def _feedback_config() -> dict:
    """部署環境用 Vercel 環境變數（不放在公開程式碼裡）；本機用 data/state/notify.json。"""
    f = Path(__file__).resolve().parent.parent / "data" / "state" / "notify.json"
    cfg = json.loads(f.read_text()) if f.exists() else {}
    env = {"ntfy_server": "NTFY_SERVER", "ntfy_topic": "NTFY_TOPIC",
           "github_repo": "FEEDBACK_GITHUB_REPO", "github_token": "FEEDBACK_GITHUB_TOKEN"}
    out = {k: os.environ.get(e) or cfg.get(k) for k, e in env.items()}
    out["ntfy_server"] = out["ntfy_server"] or "https://ntfy.sh"
    return out


def _post_json(url: str, payload: dict, headers: dict | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    urllib.request.urlopen(req, timeout=15).read()


@app.post("/api/feedback")
async def api_feedback(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "格式錯誤") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "格式錯誤")
    try:
        fb = feedback.validate(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    cfg = _feedback_config()
    if not (cfg["github_repo"] and cfg["github_token"]) and not cfg["ntfy_topic"]:
        raise HTTPException(503, "回報功能尚未設定")
    msg = feedback.summary(fb)
    print("[feedback]", msg.replace("\n", " | "))  # 也留在部署紀錄
    sent = 0
    if cfg["github_repo"] and cfg["github_token"]:
        try:
            _post_json(f"https://api.github.com/repos/{cfg['github_repo']}/issues", feedback.issue(fb),
                       {"Authorization": f"Bearer {cfg['github_token']}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "site-investigation"})
            sent += 1
        except OSError as e:
            print("[feedback] GitHub Issue 建立失敗：", e)
    if cfg["ntfy_topic"]:
        try:
            _post_json(cfg["ntfy_server"], {"topic": cfg["ntfy_topic"], "title": "建物查詢平台：樓層回報",
                                            "message": msg, "tags": ["memo"]})
            sent += 1
        except OSError as e:
            print("[feedback] ntfy 推播失敗：", e)
    if not sent:
        raise HTTPException(502, "暫時無法送出，請稍後再試")
    return {"ok": True}


@app.get("/about")
def about():
    return FileResponse(STATIC / "about.html")


@app.get("/api/lookup")
def api_lookup(q: str):
    r = lookup(_cur(), q)
    popularity.incr(r.get("osm_uid") or r.get("bldg_uid"))  # 只記建物被查次數
    return r


@lru_cache(maxsize=1)
def _suggester() -> Suggester | None:
    return Suggester(_cur()) if _has_table("roads") else None


@app.get("/api/suggest")
def api_suggest(q: str, limit: int = 8):
    """模糊搜尋：錯字、沒寫縣市區、只打一半都列出最接近的真實門牌（見 sitefloor/suggest.py）。"""
    sg = _suggester()
    return sg.suggest(_cur(), q, min(limit, 20)) if sg else []


@app.get("/api/stats")
def api_stats():
    # coverage 表由 `python -m sitefloor.coverage` 預先算好
    rows = _cur().execute(
        "SELECT county, doors, by_trade, high_mid, any_floor FROM coverage ORDER BY doors DESC").fetchall()
    return [dict(county=c, addresses=n, by_trade=round(t, 3), high_mid=round(h, 3), any_floor=round(a, 3))
            for c, n, t, h, a in rows]


@lru_cache(maxsize=16)
def _has_column(table: str, col: str) -> bool:
    return bool(_cur().execute("SELECT count(*) FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
                               [table, col]).fetchone()[0])


def _has_table(name: str) -> bool:
    return bool(_cur().execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


def _feature(i: int, geom: dict, **props) -> dict:
    return {"type": "Feature", "id": i, "properties": props, "geometry": geom}


@app.get("/api/buildings")
def api_buildings(bbox: str = Query(..., description="minlon,minlat,maxlon,maxlat")):
    """地圖立體建物，OSM 建物（每棟分開）、市府建物圖、實價門牌方柱三種。
    OSM 與建物圖重疊時畫哪個：見 sitefloor/render.py resolve_overlaps。
    高度：一律 樓層×3.2m（樓層：實價優先，其次 OSM building:levels／建物圖）；不知道樓層才用 OSM height。
    例外：OSM 分棟（building:part）各用自己的高度；有手工模型的地標（sitefloor/models.py）
    與人工校正有填 height_m 的（一層很高的場館）用真實高度。"""
    x0, y0, x1, y1 = map(float, bbox.split(","))
    box = [x0, x1, y0, y1]
    osm: list[tuple] = []  # (uid, floors, height, name, geom)
    real_h: dict[str, float] = {}
    trusted: set[str] = set()
    has_osm = _has_table("osm_buildings")
    if has_osm:
        rows = _cur().execute(f"""
            SELECT uid, coalesce(curated_floors, trade_floors, permit_floors, levels), height, name, wkb, curated_height,
                   curated_floors IS NOT NULL
            FROM osm_buildings
            WHERE maxlon >= ? AND minlon <= ? AND maxlat >= ? AND minlat <= ? AND wkb IS NOT NULL
            ORDER BY coalesce(curated_floors, trade_floors, permit_floors, levels) IS NULL  -- 超過上限時先保留知道樓層的
            LIMIT {MAX_BUILDINGS}
        """, box).fetchall()
        osm = [(u, f, h, name, shapely.from_wkb(w)) for u, f, h, name, w, _, _ in rows]
        real_h = {r[0]: r[5] for r in rows if r[5] is not None}  # 人工校正的實際高度
        # 人工校正或有手工模型的 OSM 建物一定畫，不被臺北建照外框蓋掉
        trusted = {r[0] for r in rows if r[6] or r[0] in MODELS}
    # 同建照的附屬小外框：套繪圖記的是整張建照的最高樓層，不是它自己的，當成樓層未知（畫成矮的一層）
    annex = "coalesce(annex, false)" if _has_column("buildings", "annex") else "false"
    rows = _cur().execute(f"""
        SELECT uid, CASE WHEN {annex} THEN NULL ELSE floors_above END, source, wkb FROM buildings
        WHERE maxlon >= ? AND minlon <= ? AND maxlat >= ? AND minlat <= ? AND wkb IS NOT NULL
        LIMIT {MAX_BUILDINGS + 1}
    """, box).fetchall()
    truncated = len(rows) > MAX_BUILDINGS
    city, drop_osm = resolve_overlaps(osm, [(u, f, src, shapely.from_wkb(w)) for u, f, src, w in rows[:MAX_BUILDINGS]],
                                      set(MODELS), trusted, TP_PERMIT_SOURCE)
    feats: list[dict] = []
    # OSM 分棟（building:part）：分棟各用自己的高度畫，外框只畫扣掉分棟的部分；有手工模型的地標
    # 塔樓改畫模型、外框剩下的部分不畫
    parts: dict[str, list] = defaultdict(list)
    if osm and _has_table("osm_parts"):
        uids = [o[0] for i, o in enumerate(osm) if i not in drop_osm]
        for uid, pu, lv, mlv, ph, mh, w in _cur().execute(
                "SELECT uid, parent_uid, levels, min_level, height, min_height, wkb FROM osm_parts "
                "WHERE list_contains(?, parent_uid)", [uids]).fetchall():
            if uid not in REPLACED_PARTS:
                parts[pu].append((lv, mlv, ph, mh, shapely.from_wkb(w), uid))
    # 分棟的人工校正：分棟用自己的編號，點了顯示分棟自己的樓層
    part_fix = dict(_cur().execute(
        "SELECT osm_uid, floors_above FROM corrections WHERE list_contains(?, osm_uid)",
        [[p[5] for ps in parts.values() for p in ps]]).fetchall()) if parts else {}
    for i, (u, f, h, name, g) in enumerate(osm):
        if i in drop_osm:
            continue
        for lv, mlv, ph, mh, pg, pu in parts.get(u, []):
            lv = part_fix.get(pu, lv)
            base, top = part_extent(lv, mlv, None if pu in part_fix else ph, mh, f)
            feats.append(_feature(len(feats), json.loads(shapely.to_geojson(pg)), uid=pu, floors=lv or f,
                                  h=top, base=base, name=name, src="osm_part"))
        if u in MODELS:
            for ring, z0, z1 in MODELS[u](g):
                coords = ring if isinstance(ring[0][0], list) else [ring]  # 有中庭：[外圈, 中庭…]
                feats.append(_feature(len(feats), {"type": "Polygon", "coordinates": coords}, uid=u, floors=f,
                                      h=z1, base=z0, name=name, src="model"))
            continue
        if parts.get(u):
            g = shapely.difference(g, shapely.union_all([p[4] for p in parts[u]]))
            if g.is_empty or g.area == 0:
                continue
        feats.append(_feature(len(feats), json.loads(shapely.to_geojson(g)), uid=u, floors=f,
                              h=real_h.get(u) or extrude_height(f, h), name=name, src="osm"))
    for u, f, g in city:
        feats.append(_feature(len(feats), json.loads(shapely.to_geojson(g)), uid=u, floors=f,
                              h=f * FLOOR_M if f else None, src="city"))
    # 沒有任何外框的門牌：有實價總樓層就畫成小方柱
    # 門牌所在的 OSM 建物已經有樓層（會畫出來）才不畫方柱；樓層未知的 OSM 外框裡仍畫方柱
    in_osm = """AND NOT EXISTS (SELECT 1 FROM door_osm o JOIN osm_buildings ob ON ob.uid = o.osm_uid
        WHERE o.county = a.county AND o.town = a.town AND o.base = a.base
          AND coalesce(ob.curated_floors, ob.trade_floors, ob.permit_floors, ob.levels) IS NOT NULL)"""
    pts = _cur().execute(f"""
        SELECT a.county || a.town || a.base, avg(a.lon), avg(a.lat), any_value(t.floors)
        FROM addresses a JOIN door_trade t USING (county, town, base)
        WHERE a.bldg_uid IS NULL AND a.lon BETWEEN ? AND ? AND a.lat BETWEEN ? AND ? {in_osm if has_osm else ""}
        GROUP BY a.county, a.town, a.base LIMIT {MAX_BUILDINGS}
    """, [x0, x1, y0, y1]).fetchall()
    half = PILLAR_M / 2 / 111320
    drawn = [(o[4], o[1]) for i, o in enumerate(osm) if i not in drop_osm and o[1] is not None] + \
            [(g, f) for _, f, g in city if f is not None]
    dtree = shapely.STRtree([g for g, _ in drawn]) if drawn else None
    near = PILLAR_NEAR_M / 111320
    for addr, lon, lat, f in pts:
        if dtree is not None and any(abs(drawn[int(j)][1] - f) <= 1
                                     for j in dtree.query(shapely.Point(lon, lat), predicate="dwithin", distance=near)):
            continue
        dx = half / math.cos(math.radians(lat))
        ring = [[lon - dx, lat - half], [lon + dx, lat - half], [lon + dx, lat + half], [lon - dx, lat + half],
                [lon - dx, lat - half]]
        feats.append(_feature(len(feats), {"type": "Polygon", "coordinates": [ring]}, uid=None, floors=f,
                              h=f * FLOOR_M, addr=addr, pillar=True, src="pillar"))
    return {"type": "FeatureCollection", "features": feats, "truncated": truncated}


# ── 災害潛勢圖層（sitefloor/risk.py 的 hazard_zones）：地圖上可開關，依畫面範圍回傳 ──
HAZARD_KINDS = ("flood500", "liquefaction", "fault", "landslide", "debris", "fire_station")
MAX_ZONES = 4000
MAX_ZONE_SPAN = 0.25  # 畫面範圍上限（度）；太大時前端請使用者放大


@app.get("/api/hazards")
def api_hazards(kind: str = Query(...), bbox: str = Query(..., description="minlon,minlat,maxlon,maxlat")):
    if kind not in HAZARD_KINDS:
        raise HTTPException(400, "未知的圖層")
    try:
        x0, y0, x1, y1 = map(float, bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox 格式錯誤") from None
    if not (x0 < x1 and y0 < y1) or x1 - x0 > MAX_ZONE_SPAN or y1 - y0 > MAX_ZONE_SPAN:
        return {"type": "FeatureCollection", "features": [], "too_large": True}
    if not _has_table("hazard_zones"):
        return {"type": "FeatureCollection", "features": []}
    rows = _cur().execute(f"""
        SELECT value, rank, wkb FROM hazard_zones
        WHERE kind = ? AND maxlon >= ? AND minlon <= ? AND maxlat >= ? AND minlat <= ?
        ORDER BY rank DESC LIMIT {MAX_ZONES + 1}""", [kind, x0, x1, y0, y1]).fetchall()
    feats = [_feature(i, json.loads(shapely.to_geojson(shapely.from_wkb(w))), value=v, rank=r)
             for i, (v, r, w) in enumerate(rows[:MAX_ZONES])]
    return {"type": "FeatureCollection", "features": feats, "truncated": len(rows) > MAX_ZONES}


NEAR_DOOR_M = 100  # 建物沒有門牌時，用這個距離內最近的門牌判斷建物風險資訊


def _door_uids(uid: str) -> list[str]:
    """門牌要用哪些外框的：本身沒有門牌的臺北建照外框，用同一張建照其他外框的門牌
    （同一張建照常拆成好幾個外框，門牌點只會落在其中一個）。"""
    cur = _cur()
    if cur.execute("SELECT count(*) FROM addresses WHERE bldg_uid = ?", [uid]).fetchone()[0]:
        return [uid]
    try:
        same = cur.execute("""SELECT uid FROM buildings WHERE permit_no IS NOT NULL AND permit_no =
                              (SELECT permit_no FROM buildings WHERE uid = ?)""", [uid]).fetchall()
    except duckdb.BinderException:  # 舊資料沒有 permit_no 欄位
        return [uid]
    return [u for (u,) in same] or [uid]


def _building_doors(uid: str, lon, lat) -> tuple[pd.DataFrame, str | None]:
    """棟內門牌（含座標），供建物風險資訊用；沒有就找 NEAR_DOOR_M 內最近的門牌。回傳 (門牌, 說明)。"""
    cur = _cur()
    if uid.startswith(OSM_TABLE):
        sql = """SELECT o.county, o.town, o.base, avg(a.lon) AS lon FROM door_osm o
                 LEFT JOIN addresses a USING (county, town, base) WHERE o.osm_uid = ? GROUP BY ALL LIMIT 200"""
    else:
        sql = """SELECT county, town, base, avg(lon) AS lon FROM addresses WHERE list_contains(?, bldg_uid)
                 GROUP BY ALL LIMIT 200"""
    doors = cur.execute(sql, [uid if uid.startswith(OSM_TABLE) else _door_uids(uid)]).df()
    if not doors.empty or lon is None or lat is None:
        return doors, None
    d = NEAR_DOOR_M / 111_000
    near = cur.execute("""
        SELECT county, town, base, lon, (lon - ?) * (lon - ?) * 0.82 + (lat - ?) * (lat - ?) AS d2 FROM addresses
        WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND town IS NOT NULL ORDER BY d2 LIMIT 1""",
        [lon, lon, lat, lat, lon - d, lon + d, lat - d, lat + d]).df()
    if near.empty:
        return near, None
    n = near.iloc[0]
    return near[["county", "town", "base", "lon"]], f"這棟建物沒有門牌，建物風險資訊依最近的門牌（{n.town}{n.base}）判斷"


def _with_risk(out: dict) -> dict:
    doors, note = _building_doors(out["uid"], out.get("lon"), out.get("lat"))
    out.update(building_risk(_cur(), doors))
    if note:
        out["risk_note"] = note
    return out


_CONF_RANK = {"高": 0, "中": 1, "低": 2}


def _floors_from_doors(full_addrs: list[str]) -> dict | None:
    """建物本身沒有樓層時，改查它的門牌（和地址查詢同一套：使用執照、同棟、同巷推估…），
    取信心最高、其次最多門牌一致的結果。讓「點地圖建物」和「查該地址」看到的樓層一致。"""
    if not full_addrs:
        return None
    res = lookup_many(_cur(), full_addrs)
    res = res[res.floors_above.notna() & res.confidence.isin(list(_CONF_RANK))]
    if res.empty:
        return None
    res = res.assign(rank=res.confidence.map(_CONF_RANK))
    best = res[res["rank"] == res["rank"].min()]
    floors = best.floors_above.mode().max()
    r = best[best.floors_above == floors].iloc[0]
    n = int((best.floors_above == floors).sum())
    return {"floors_above": int(floors), "floors_below": None if pd.isna(r.floors_below) else int(r.floors_below),
            "source": f"依門牌查詢：{r.source}", "confidence": r.confidence,
            "note": f"這棟建物本身沒有樓層資料，改用棟內門牌查詢（{n}/{len(full_addrs)} 個門牌）"}


def _osm_part(uid: str) -> dict:
    """OSM 分棟（building:part）：樓層用分棟自己的（人工校正優先），門牌與風險資訊用所在建物的。"""
    cur = _cur()
    r = cur.execute("SELECT parent_uid, levels FROM osm_parts WHERE uid = ?", [uid]).fetchone()
    if not r:
        raise HTTPException(404)
    parent, levels = r
    out = _osm_building(parent)
    fix = cur.execute("SELECT floors_above, floors_below, year, source, note FROM corrections WHERE osm_uid = ?",
                      [uid]).fetchone()
    pname = out.get("name") or ""
    if fix:
        out.update(floors_above=fix[0], floors_below=fix[1], year=fix[2],
                   year_kind="竣工年（人工校正）" if fix[2] is not None else None, source="人工校正",
                   note="；".join(x for x in (fix[4], fix[3]) if x))
    else:
        out.update(floors_above=levels, floors_below=None, year=None, year_kind=None, source="OpenStreetMap 分棟",
                   note=f"{pname}的分棟（OSM building:part）；整棟資料請點主體")
    out.update(uid=uid, parent_uid=parent)
    return out


def _osm_building(uid: str) -> dict:
    cur = _cur()
    r = cur.execute("""SELECT uid, name, building, levels, trade_floors, height,
                       (minlon + maxlon) / 2, (minlat + maxlat) / 2, curated_floors, permit_floors
                       FROM osm_buildings WHERE uid = ?""", [uid]).fetchone()
    if not r:
        if cur.execute("SELECT count(*) FROM osm_parts WHERE uid = ?", [uid]).fetchone()[0]:
            return _osm_part(uid)
        raise HTTPException(404)
    u, name, _btype, levels, tf, h, lon, lat, cf, pf = r
    addrs = cur.execute("SELECT town || base FROM door_osm WHERE osm_uid = ? ORDER BY 1 LIMIT 30", [uid]).fetchall()
    n, county = cur.execute("SELECT count(*), any_value(county) FROM door_osm WHERE osm_uid = ?", [uid]).fetchone()
    below = year = None
    if cf is not None:  # 人工校正的地下樓層、竣工年
        below, year = cur.execute(
            "SELECT max(floors_below), max(year) FROM corrections WHERE osm_uid = ?", [uid]).fetchone()
    src = ("人工校正" if cf is not None else "實價登錄（棟內門牌）" if tf is not None
           else "使用執照（棟內門牌）" if pf is not None else "人工補畫外框" if u.startswith("cur:") else "OpenStreetMap")
    known_floors = next((x for x in (cf, tf, pf, levels) if x is not None), None)
    note = [] if known_floors is not None else ["OSM 有這棟的外框，但沒有樓層資料（地圖畫成灰色）"]
    if name:
        note.append(name)
    if levels is not None and tf is not None and levels != tf:
        note.append(f"OSM 標 {levels} 層、實價登錄 {tf} 層，採實價登錄")
    if h is not None:
        note.append(f"OSM 標高 {h:g} m（地圖高度統一用 樓層×3.2m）")
    out = {"uid": u, "source": src, "floors_above": known_floors, "floors_below": below,
           "structure": None, "btype": None, "year": year,
           "year_kind": "竣工年（人工校正）" if year is not None else None, "lon": lon, "lat": lat, "addresses": [a for (a,) in addrs], "n_units": n,
           "note": "；".join(note), "osm": True, "county": county, "name": name}
    if known_floors is None:
        full = [c + t + b for c, t, b in cur.execute(
            "SELECT county, town, base FROM door_osm WHERE osm_uid = ? ORDER BY town, base LIMIT 30", [uid]).fetchall()]
        if d := _floors_from_doors(full):
            out.update({k: d[k] for k in ("floors_above", "floors_below", "source")})
            out["note"] = "；".join(x for x in [d["note"], name] if x)
    return _with_risk(out)


@app.get("/api/building/{uid}")
def api_building(uid: str):
    popularity.incr(uid)
    if uid.startswith(OSM_TABLE):
        return _osm_building(uid)
    cur = _cur()
    b = cur.execute("""SELECT uid, county, source, floors_above, floors_below, structure, btype,
                       year, year_kind, lon, lat FROM buildings WHERE uid = ?""", [uid]).df()
    if b.empty:
        raise HTTPException(404)
    uids = _door_uids(uid)
    addrs = cur.execute("""
        SELECT DISTINCT town || base AS addr FROM addresses WHERE list_contains(?, bldg_uid) ORDER BY 1 LIMIT 30
    """, [uids]).fetchall()
    n = cur.execute("SELECT count(*) FROM addresses WHERE list_contains(?, bldg_uid)", [uids]).fetchone()[0]
    out = b.iloc[0].to_dict()
    out = {k: (None if pd.isna(v) else v) for k, v in out.items()}
    out.update(addresses=[a for (a,) in addrs], n_units=n)
    if _has_column("buildings", "annex") and cur.execute("SELECT annex FROM buildings WHERE uid = ?", [uid]).fetchone()[0]:
        out["note"] = (f"同一張建照拆出來的附屬小外框；套繪圖記的 {out['floors_above']} 層是整張建照的最高樓層，"
                       "不是這塊的樓層")
        out["floors_above"] = None
        return _with_risk(out)
    if out.get("floors_above") is None:
        full = [c + t + b for c, t, b in cur.execute(
            "SELECT DISTINCT county, town, base FROM addresses WHERE list_contains(?, bldg_uid) ORDER BY 2, 3 LIMIT 30",
            [uids]).fetchall()]
        if d := _floors_from_doors(full):
            out.update({k: d[k] for k in ("floors_above", "floors_below", "source")})
            out["note"] = d["note"]
    return _with_risk(out)


# ── 用選的查地址：縣市 → 鄉鎮市區 → 路街 → 巷弄 → 門牌號 ──
# 門牌來源＝門牌點位 ∪ 實價登錄（沒有點位的縣市也能選）；has＝這個門牌自己有實價登錄


COUNTY_ORDER = ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣", "臺中市", "彰化縣", "南投縣",
                "雲林縣", "嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣",
                "金門縣", "連江縣"]


@lru_cache(maxsize=4)
def _counties() -> list[dict]:
    rows = _cur().execute("""
        SELECT county, count(DISTINCT town) FROM (
            SELECT county, town FROM addresses UNION ALL SELECT county, town FROM door_trade)
        WHERE town IS NOT NULL GROUP BY 1""").fetchall()
    rank = {c: i for i, c in enumerate(COUNTY_ORDER)}
    return [{"name": c, "n": n} for c, n in sorted(rows, key=lambda r: rank.get(r[0], 99))]


@lru_cache(maxsize=32)
def _towns(county: str) -> list[dict]:
    rows = _cur().execute("""
        SELECT town, count(DISTINCT base) FROM (
            SELECT town, base FROM addresses WHERE county = ? UNION ALL
            SELECT town, base FROM door_trade WHERE county = ?)
        WHERE town IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""", [county, county]).fetchall()
    return [{"name": t, "n": n} for t, n in rows]


@lru_cache(maxsize=16)
def _doors(county: str, town: str) -> list[dict]:
    rows = _cur().execute("""
        SELECT d.base, t.base IS NOT NULL FROM (
            SELECT DISTINCT base FROM addresses WHERE county = ? AND town = ?
            UNION SELECT base FROM door_trade WHERE county = ? AND town = ?) d
        LEFT JOIN door_trade t ON t.county = ? AND t.town = ? AND t.base = d.base""",
        [county, town] * 3).fetchall()
    out = []
    for base, has in rows:
        p = split_base(base)
        if not p:  # 地號、中文數字等不規則門牌不列
            continue
        road, lane, num, sub, near = p
        label = ("臨" if near else "") + str(num) + (f"之{sub}" if sub else "")
        out.append({"road": road, "lane": lane, "num": label, "base": base, "has": has, "key": (num, sub, near)})
    out.sort(key=lambda d: d["key"])
    return out


def _lane_key(lane: str):
    return [int(x) for x in re.findall(r"\d+", lane)] or [-1]


@app.get("/api/pick")
def api_pick(county: str = "", town: str = "", road: str = "", lane: str | None = None):
    """依已選的層級回傳下一層選項。lane=""（空字串）代表沿路門牌（不在巷弄內）。"""
    if not county:
        return {"level": "county", "items": _counties()}
    if not town:
        return {"level": "town", "items": _towns(county)}
    doors = _doors(county, town)
    if not road:
        cnt: dict[str, list[int]] = {}
        for d in doors:
            c = cnt.setdefault(d["road"], [0, 0])
            c[0] += 1
            c[1] += d["has"]
        items = [{"name": r, "n": n, "has": h} for r, (n, h) in cnt.items()]
        return {"level": "road", "items": sorted(items, key=lambda x: -x["n"])}
    doors = [d for d in doors if d["road"] == road]
    if lane is None:
        lanes: dict[str, list[int]] = {}
        for d in doors:
            c = lanes.setdefault(d["lane"], [0, 0])
            c[0] += 1
            c[1] += d["has"]
        if len(lanes) > 1:
            items = [{"name": ln, "n": n, "has": h} for ln, (n, h) in lanes.items()]
            return {"level": "lane", "items": sorted(items, key=lambda x: _lane_key(x["name"]))}
        lane = next(iter(lanes), "")
    items = [{"name": d["num"], "base": d["base"], "has": d["has"]} for d in doors if d["lane"] == lane]
    return {"level": "door", "lane": lane, "items": items}


def _guess_col(df: pd.DataFrame) -> str:
    for c in df.columns:
        if "地址" in str(c) or "門牌" in str(c):
            return c
    # 否則取「最像地址」的欄：含「號」比例最高
    return max(df.columns, key=lambda c: df[c].astype(str).str.contains("號").mean())


@app.post("/api/batch")
async def api_batch(file: UploadFile, column: str | None = None):
    raw = await file.read(BATCH_MAX_BYTES + 1)
    if len(raw) > BATCH_MAX_BYTES:
        raise HTTPException(413, f"檔案太大，上限 {BATCH_MAX_BYTES // 1024 // 1024} MB")
    name = file.filename or "input"
    if name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw), dtype=str)
    else:
        try:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="cp950")
    if len(df) > BATCH_MAX_ROWS:
        raise HTTPException(413, f"一次最多 {BATCH_MAX_ROWS:,} 筆，這個檔案有 {len(df):,} 筆，請分批上傳")
    col = column or _guess_col(df)
    if col not in df.columns:
        raise HTTPException(400, f"找不到欄位：{col}")
    res = lookup_many(_cur(), df[col].fillna("").tolist())
    add = res[[k for k in COLUMNS if k != "input"]].rename(columns=COLUMNS)
    out = pd.concat([df.reset_index(drop=True), add], axis=1)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        out.to_excel(w, index=False, sheet_name="結果")
        summary = res.confidence.value_counts().rename_axis("信心").reset_index(name="筆數")
        summary.to_excel(w, index=False, sheet_name="摘要")
    buf.seek(0)
    stem = Path(name).stem
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stem + '_樓層.xlsx')}"},
    )
