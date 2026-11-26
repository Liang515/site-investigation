"""本機查詢介面。啟動：uv run uvicorn app.server:app --port 8765"""
import asyncio
import io
import json
import math
import os
import urllib.error
import urllib.request
import re
import sys
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import duckdb
import pandas as pd
import shapely
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from sitefloor import feedback, lookup as lookup_mod, popularity, stats, usage
from sitefloor.db import connect
from sitefloor.lookup import COLUMNS, TP_PERMIT_SOURCE, building_risk, lookup, lookup_many, trade_records
from sitefloor.models import MODELS, REPLACED_PARTS
from sitefloor.render import FLOOR_M, extrude_height, part_extent, resolve_overlaps
from sitefloor.suggest import Suggester, split_base

STATIC = Path(__file__).parent / "static"
MAX_BUILDINGS = 6000
PILLAR_M = 10  # 實價門牌方柱邊長（公尺）
OSM_TABLE = ("osm:", "cur:")  # 存在 osm_buildings 的建物：OSM 外框、人工補畫的外框（curated/buildings.geojson）
PILLAR_NEAR_M = 25  # 門牌點常落在建物外框外的騎樓、路邊：這個距離內已有樓層相同（±1）的建物就不畫方柱

_STARTED = time.monotonic()  # 這個執行個體載入程式的時間
app = FastAPI(title="建物查詢平台")

# ── 查詢次數限制（公開部署時避免被大量抓取）：每個 IP 在時間窗內的次數上限 ──
RATE_LIMITS = {  # 路徑前綴：(次數, 秒)；依序比對，越特定的放越前面
    "/api/batch": (5, 3600),
    "/api/feedback": (5, 3600),
    "/api/event": (20, 3600),
    "/api/cron/": (5, 3600),
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


# ── 量測：每個請求各段 SQL 的耗時放進 Server-Timing 標頭（瀏覽器開發者工具的 Timing 分頁看得到） ──
# 查詢用的 SQL 以 lookup.py 裡的常數名稱標示（_TRADE_SQL → trade、_SQL → addr），其他以呼叫它的函式名稱標示
_SQL_NAMES = {id(v): (k.removesuffix("_SQL").strip("_").lower() or "addr")
              for k, v in vars(lookup_mod).items() if k.endswith("_SQL") and isinstance(v, str)}
_timings: ContextVar[dict | None] = ContextVar("timings", default=None)


def _timed(name: str, ms: float) -> None:
    t = _timings.get()
    if t is not None:
        t[name] = t.get(name, 0.0) + ms


class _TimedCursor:
    """DuckDB cursor 加上計時；其他方法照原樣轉給 cursor。"""

    def __init__(self, cur: duckdb.DuckDBPyConnection):
        self._cur = cur

    def execute(self, sql, *args, **kwargs):
        t = time.perf_counter()
        try:
            return self._cur.execute(sql, *args, **kwargs)
        finally:
            name = _SQL_NAMES.get(id(sql)) or sys._getframe(1).f_code.co_name.strip("_")
            _timed(name, (time.perf_counter() - t) * 1000)

    def __getattr__(self, name):
        return getattr(self._cur, name)


@app.middleware("http")
async def server_timing(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    timings: dict[str, float] = {}
    _timings.set(timings)
    t, c = time.perf_counter(), time.process_time()
    resp = await call_next(request)
    timings["total"] = (time.perf_counter() - t) * 1000
    timings["cpu"] = (time.process_time() - c) * 1000  # 整個程序的 CPU 時間（同時有其他請求時會一起算進來）
    resp.headers["Server-Timing"] = ", ".join(f"{k};dur={v:.1f}" for k, v in timings.items())
    return resp


# ── CDN 快取：同一版部署裡結果固定的 GET 請求由 Vercel CDN 直接回應，不再執行程式（省 CPU 時間）──
# Vercel 每次部署會清掉 CDN 快取，資料更新後不會拿到舊結果。查地址、點建物要記使用次數，不快取。
CDN_CACHED = ("/api/buildings", "/api/hazards", "/api/pick", "/api/suggest", "/api/dashboard", "/api/stats")
CDN_MAX_AGE = 86400


@app.middleware("http")
async def cdn_cache(request: Request, call_next):
    resp = await call_next(request)
    if request.method == "GET" and resp.status_code == 200 and request.url.path in CDN_CACHED:
        resp.headers["Vercel-CDN-Cache-Control"] = f"max-age={CDN_MAX_AGE}"  # 只給 Vercel CDN，瀏覽器看不到
    return resp


def _cur():
    # DuckDB 連線不可跨執行緒共用，每個請求開 cursor
    return _TimedCursor(con.cursor())


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
    # 送 GitHub、ntfy 是阻塞的網路請求（最長各 15 秒），放到執行緒裡跑，不卡住其他請求
    if not await asyncio.to_thread(_send_feedback, fb, cfg):
        raise HTTPException(502, "暫時無法送出，請稍後再試")
    return {"ok": True}


def _send_feedback(fb: dict, cfg: dict) -> int:
    """回報送到 GitHub Issue、ntfy；回傳成功送出的管道數。"""
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
            _post_json(cfg["ntfy_server"], {"topic": cfg["ntfy_topic"],
                                            "title": f"建物查詢平台：{feedback.KINDS[fb['kind']]}",
                                            "message": msg, "tags": ["memo"]})
            sent += 1
        except OSError as e:
            print("[feedback] ntfy 推播失敗：", e)
    return sent


@app.post("/api/event", status_code=204)
async def api_event(request: Request):
    """只發生在瀏覽器的動作（例：定位）：只收 usage.EVENTS 裡的名稱，只加次數。"""
    name = (await request.body()).decode(errors="ignore").strip()
    if name not in usage.EVENTS:
        raise HTTPException(400, "未知的事件")
    usage.record({f"ev:{name}": 1})


@app.get("/api/cron/weekly")
def api_cron_weekly(request: Request):
    """每週一由 Vercel Cron 呼叫（vercel.json）：上週使用統計推播到 ntfy。一週只送一次。
    有設 CRON_SECRET 時 Vercel 會帶 Authorization: Bearer <secret>；沒設時只接受 Vercel Cron 的 User-Agent。"""
    secret = os.environ.get("CRON_SECRET")
    ok = (request.headers.get("authorization") == f"Bearer {secret}" if secret
          else request.headers.get("user-agent", "").startswith("vercel-cron"))
    if not ok:
        raise HTTPException(401)
    cfg = _feedback_config()
    if not cfg["ntfy_topic"]:
        raise HTTPException(503, "沒有設定推播頻道")
    end = usage.today()
    if usage.already_sent(end):
        return {"sent": False, "reason": "本週已送過或沒有設定 Upstash"}
    text = usage.summary(end)
    msg = {"topic": cfg["ntfy_topic"], "title": "建物查詢平台：上週使用統計", "message": text, "tags": ["bar_chart"]}
    email = os.environ.get("NOTIFY_EMAIL")  # 另由 ntfy 代寄 Email（信箱只放在部署環境變數；ntfy 要帳號權杖才能寄）
    if email:
        msg["email"] = email
    if token := os.environ.get("NTFY_TOKEN"):
        headers = {"Authorization": f"Bearer {token}"}
    else:
        headers = None
    try:
        _post_json(cfg["ntfy_server"], msg, headers)
    except urllib.error.HTTPError as e:
        if "email" not in msg:
            raise
        print("[cron] ntfy 拒絕代寄 Email，改送一般推播：", e.read().decode(errors="ignore")[:200])
        del msg["email"]
        email = None
        _post_json(cfg["ntfy_server"], msg, headers)
    usage.mark_sent(end)  # 送出成功才記下，失敗時下次排程或手動重試還會再送
    return {"sent": True, "email": bool(email)}


@app.get("/about")
def about():
    return FileResponse(STATIC / "about.html")


@app.get("/charts.js", include_in_schema=False)
def charts_js():
    return FileResponse(STATIC / "charts.js", media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=3600"})


# 前端函式庫（MapLibre）自己放：不依賴第三方 CDN 的可用性與完整性。路徑帶版本號，內容固定，
# 瀏覽器快取一年、Vercel CDN 也快取（每次部署每個區域只有第一次會執行到這裡）
VENDOR = STATIC / "vendor"
VENDOR_TYPES = {".mjs": "text/javascript", ".css": "text/css"}


@app.get("/vendor/{pkg}/{name}", include_in_schema=False)
def vendor(pkg: str, name: str):
    f = VENDOR / pkg / name
    if f.suffix not in VENDOR_TYPES or f.resolve().parent.parent != VENDOR.resolve() or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, media_type=VENDOR_TYPES[f.suffix],
                        headers={"Cache-Control": "public, max-age=31536000, immutable",
                                 "Vercel-CDN-Cache-Control": "max-age=31536000"})


@app.get("/stats")
def stats_page():
    return FileResponse(STATIC / "stats.html")


@lru_cache(maxsize=512)
def _dashboard(county: str, town: str) -> dict | None:
    cur = _cur()
    area = (county or stats.NATION, town if county else "")
    found = cur.execute("SELECT * FROM stats_risk WHERE county = ? AND town = ?", list(area)).df()
    if found.empty:
        return None
    parent = None if area[0] == stats.NATION else (area[0], "") if area[1] else (stats.NATION, "")
    keys = [area] + ([parent] if parent else [])
    where = " OR ".join("(county = ? AND town = ?)" for _ in keys)
    args = [x for k in keys for x in k]
    price = cur.execute(f"SELECT county, town, grp, per, t, n, p25, p50, p75 FROM stats_price WHERE {where} "
                        "ORDER BY t", args).fetchall()

    def series(key, full):
        out: dict = {}
        for c, tw, g, per, t, n, p25, p50, p75 in price:
            if (c, tw) == key:
                out.setdefault(g, {}).setdefault(per, []).append(
                    [t.isoformat(), n, p25, p50, p75] if full else [t.isoformat(), n, p50])
        return out

    bldg: dict = {}
    for dim, bucket, n in cur.execute("SELECT dim, bucket, n FROM stats_bldg WHERE county = ? AND town = ?",
                                      list(area)).fetchall():
        bldg.setdefault(dim, {})[bucket] = n
    risk_rows = cur.execute(f"SELECT * FROM stats_risk WHERE {where}", args).df()
    cov = {(k, c) for k, c in cur.execute("SELECT kind, county FROM risk_coverage").fetchall()}
    county_doors = dict(cur.execute(
        "SELECT county, doors FROM stats_risk WHERE town = '' AND county <> ?", [stats.NATION]).fetchall())

    def risk(key):
        """各項門牌數，加上算比例的分母 base：有這種圖資的縣市的門牌數（沒有圖資＝不知道，不是 0）。"""
        r = risk_rows[(risk_rows.county == key[0]) & (risk_rows.town == key[1])]
        if r.empty:
            return None
        d = {k: (None if pd.isna(v) else int(v)) for k, v in r.iloc[0].drop(["county", "town"]).items()}
        base = {}
        for col, kind in stats.RISK_KINDS.items():
            if key[0] == stats.NATION:
                base[col] = sum(n for c, n in county_doors.items() if (kind, c) in cov) or None
            else:
                base[col] = d["doors"] if (kind, key[0]) in cov else None
            if base[col] is None:
                d[col] = None
        d["base"] = base
        return d

    rank = {c: i for i, c in enumerate(COUNTY_ORDER)}
    towns = [t for (t,) in cur.execute("SELECT town FROM stats_risk WHERE county = ? AND town <> '' ORDER BY doors DESC",
                                       [area[0]]).fetchall()] if area[0] != stats.NATION else []
    return {
        "county": area[0], "town": area[1], "parent": list(parent) if parent else None,
        "counties": sorted(county_doors, key=lambda c: rank.get(c, 99)), "towns": towns,
        "latest_trade": _latest_trade(),  # 已快取；不必每個區域重掃一次成交表
        "groups": stats.GROUPS, "buckets": stats.BUCKETS, "fire_far_m": stats.FIRE_FAR_M,
        "price": series(area, True), "parent_price": series(parent, False) if parent else None,
        "bldg": {dim: [[b, bldg.get(dim, {}).get(b, 0)] for b in order] for dim, order in stats.BUCKETS.items()},
        "risk": risk(area), "parent_risk": risk(parent) if parent else None,
    }


@app.get("/api/dashboard")
def api_dashboard(county: str = "", town: str = ""):
    """統計頁：一個區域（全國／縣市／鄉鎮）的房價走勢、建物分布、災害潛勢比例，附上一層區域做比較。"""
    if not _has_table("stats_price"):
        raise HTTPException(503, "統計資料尚未建立")
    out = _dashboard(county, town)
    if out is None:
        raise HTTPException(404, "找不到這個區域")
    return JSONResponse(out, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/lookup")
def api_lookup(q: str, via: str | None = None):
    r = lookup(_cur(), q)
    uid = r.get("osm_uid") or r.get("bldg_uid")
    popularity.incr(uid)  # 只記建物被查次數
    usage.record(usage.lookup_fields(r, via))  # 每天各項次數（不記地址）
    # 建物資料一起回傳，網頁不用等查詢結果回來再發第二個請求
    try:
        r["building"] = _building(uid, risk=False) if uid else None  # 卡片用不到整棟的風險資訊與成交紀錄
    except HTTPException:
        r["building"] = None
    return r


@lru_cache(maxsize=1)
def _suggester() -> Suggester | None:
    return Suggester(_cur()) if _has_table("roads") else None


@app.get("/api/suggest")
def api_suggest(q: str, limit: int = 8):
    """模糊搜尋：錯字、沒寫縣市區、只打一半都列出最接近的真實門牌（見 sitefloor/suggest.py）。"""
    sg = _suggester()
    return sg.suggest(_cur(), q, min(limit, 20)) if sg else []


@app.get("/api/version")
def api_version():
    """部署的資料版本（最新一筆成交日期）：部署前檢查用來比對，避免拿舊資料蓋掉正式站。
    另附這個執行個體啟動多久、累計用了多少 CPU 時間（含啟動），用來估算雲端的 CPU 用量。"""
    return {"latest_trade": _latest_trade(), "uptime_s": round(time.monotonic() - _STARTED, 1),
            "cpu_s": round(time.process_time(), 2)}


@lru_cache(maxsize=1)
def _latest_trade() -> str | None:
    d = _cur().execute("SELECT max(trade_date) FILTER (WHERE trade_date <= current_date) FROM trades").fetchone()[0]
    return d.isoformat() if d else None


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


@lru_cache(maxsize=32)  # 部署後資料表固定不變
def _has_table(name: str) -> bool:
    return bool(_cur().execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


MAX_BLDG_SPAN = 0.25  # 建物圖層的畫面範圍上限（度）；前端 zoom 15.5 以上才請求，正常畫面遠小於此


def _parse_bbox(bbox: str, span: float) -> tuple[float, float, float, float] | None:
    """解析 minlon,minlat,maxlon,maxlat。格式錯誤回 400；範圍顛倒、非有限數或超過 span 度回 None（呼叫端回空集合）。"""
    try:
        x0, y0, x1, y1 = map(float, bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox 格式錯誤") from None
    if not all(map(math.isfinite, (x0, y0, x1, y1))) or not (x0 < x1 and y0 < y1) \
            or x1 - x0 > span or y1 - y0 > span:
        return None
    return x0, y0, x1, y1


def _feature(i: int, geom: dict, **props) -> dict:
    return {"type": "Feature", "id": i, "properties": props, "geometry": geom}


@app.get("/api/buildings")
def api_buildings(bbox: str = Query(..., description="minlon,minlat,maxlon,maxlat")):
    """地圖立體建物，OSM 建物（每棟分開）、市府建物圖、實價門牌方柱三種。
    OSM 與建物圖重疊時畫哪個：見 sitefloor/render.py resolve_overlaps。
    高度：一律 樓層×3.2m（樓層：實價優先，其次 OSM building:levels／建物圖）；不知道樓層才用 OSM height。
    例外：OSM 分棟（building:part）各用自己的高度；有手工模型的地標（sitefloor/models.py）
    與人工校正有填 height_m 的（一層很高的場館）用真實高度。"""
    xy = _parse_bbox(bbox, MAX_BLDG_SPAN)
    if xy is None:
        return {"type": "FeatureCollection", "features": [], "too_large": True}
    x0, y0, x1, y1 = xy
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
    if _has_table("pillars"):  # 建庫時已算好（osm.PILLARS_SQL）
        pts = _cur().execute(f"""SELECT addr, lon, lat, floors FROM pillars
            WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? LIMIT {MAX_BUILDINGS}""", [x0, x1, y0, y1]).fetchall()
    else:
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
    xy = _parse_bbox(bbox, MAX_ZONE_SPAN)
    if xy is None:
        return {"type": "FeatureCollection", "features": [], "too_large": True}
    x0, y0, x1, y1 = xy
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
    towns = doors.town.dropna() if "town" in doors else pd.Series(dtype=object)
    if not out.get("town") and len(towns):  # 價格圖比較同鄉鎮行情用
        out["town"] = towns.mode().iloc[0]
    if note:
        out["risk_note"] = note
    else:  # 棟內門牌的成交紀錄（借用最近門牌時不列，那不是這棟的）
        out["trades"] = trade_records(_cur(), doors.fillna({"town": ""}))
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


def _osm_part(uid: str, risk: bool = True) -> dict:
    """OSM 分棟（building:part）：樓層用分棟自己的（人工校正優先），門牌與風險資訊用所在建物的。"""
    cur = _cur()
    r = cur.execute("SELECT parent_uid, levels FROM osm_parts WHERE uid = ?", [uid]).fetchone()
    if not r:
        raise HTTPException(404)
    parent, levels = r
    out = _osm_building(parent, risk)
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


def _osm_building(uid: str, risk: bool = True) -> dict:
    cur = _cur()
    r = cur.execute("""SELECT uid, name, building, levels, trade_floors, height,
                       (minlon + maxlon) / 2, (minlat + maxlat) / 2, curated_floors, permit_floors
                       FROM osm_buildings WHERE uid = ?""", [uid]).fetchone()
    if not r:
        if cur.execute("SELECT count(*) FROM osm_parts WHERE uid = ?", [uid]).fetchone()[0]:
            return _osm_part(uid, risk)
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
    return _with_risk(out) if risk else out


@app.get("/api/building/{uid}")
def api_building(uid: str):
    popularity.incr(uid)
    usage.record({"building": 1})
    return _building(uid)


def _building(uid: str, risk: bool = True) -> dict:
    """建物資料；risk＝一併算建物風險資訊與棟內成交紀錄（點地圖建物時要，查地址時卡片用門牌自己的）。"""
    if uid.startswith(OSM_TABLE):
        return _osm_building(uid, risk)
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
        return _with_risk(out) if risk else out
    if out.get("floors_above") is None:
        full = [c + t + b for c, t, b in cur.execute(
            "SELECT DISTINCT county, town, base FROM addresses WHERE list_contains(?, bldg_uid) ORDER BY 2, 3 LIMIT 30",
            [uids]).fetchall()]
        if d := _floors_from_doors(full):
            out.update({k: d[k] for k in ("floors_above", "floors_below", "source")})
            out["note"] = d["note"]
    return _with_risk(out) if risk else out


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
    # 讀檔、查詢、寫 Excel 都是阻塞的運算，放到執行緒裡跑，不卡住其他請求
    return await asyncio.to_thread(_batch, raw, file.filename or "input", column)


def _read_table(raw: bytes, name: str) -> pd.DataFrame:
    """讀上傳的 Excel／CSV；讀不了或沒有欄位回 400。"""
    try:
        if name.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(raw), dtype=str)
        else:
            try:
                df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="cp950")
    except Exception as e:  # noqa: BLE001 — 壞檔的例外種類很多（zip、xlrd、CSV 解析、編碼）
        print("[batch] 讀檔失敗：", type(e).__name__, e)
        raise HTTPException(400, "無法讀取檔案，請確認是 Excel（.xlsx）或 CSV") from None
    if df.empty:  # 沒有欄位，或只有標題列
        raise HTTPException(400, "檔案裡沒有資料")
    return df


def _batch(raw: bytes, name: str, column: str | None) -> StreamingResponse:
    df = _read_table(raw, name)
    if len(df) > BATCH_MAX_ROWS:
        raise HTTPException(413, f"一次最多 {BATCH_MAX_ROWS:,} 筆，這個檔案有 {len(df):,} 筆，請分批上傳")
    col = column or _guess_col(df)
    if col not in df.columns:
        raise HTTPException(400, f"找不到欄位：{col}")
    res = lookup_many(_cur(), df[col].fillna("").tolist())
    usage.record({"batch": 1, "batch_rows": len(df)})
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
