"""本機查詢介面。啟動：uv run uvicorn app.server:app --port 8765"""
import io
import json
import math
import re
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import shapely
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from sitefloor.db import connect
from sitefloor.lookup import COLUMNS, TP_PERMIT_SOURCE, lookup, lookup_many
from sitefloor.suggest import Suggester, split_base

STATIC = Path(__file__).parent / "static"
MAX_BUILDINGS = 6000
PILLAR_M = 10  # 實價門牌方柱邊長（公尺）
FLOOR_M = 3.2  # 立體高度：每層公尺數
CITY_COVERED_MAX = 0.3  # 市府外框被 OSM 建物蓋住超過此比例就改畫 OSM

app = FastAPI(title="地址樓層查詢")

# ── 查詢次數限制（公開部署時避免被大量抓取）：每個 IP 在時間窗內的次數上限 ──
RATE_LIMITS = {  # 路徑前綴：(次數, 秒)
    "/api/batch": (5, 3600),
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
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.png", media_type="image/png")


@app.get("/{name}.png", include_in_schema=False)
def static_png(name: str):
    if name not in ("logo", "favicon", "apple-touch-icon"):
        raise HTTPException(404)
    return FileResponse(STATIC / f"{name}.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/about")
def about():
    return FileResponse(STATIC / "about.html")


@app.get("/api/lookup")
def api_lookup(q: str):
    return lookup(_cur(), q)


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


def _has_table(name: str) -> bool:
    return bool(_cur().execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


def _extrude_height(floors, height):
    """立體高度（公尺）。OSM 標的 height 與樓層不合理時（例：高雄八五大樓標 85 層、高 50 m）改用 樓層×3.2m。"""
    if floors and height and 2.5 * floors <= height <= 6 * floors:
        return height
    if floors:
        return floors * FLOOR_M
    return height


def _feature(i: int, geom: dict, **props) -> dict:
    return {"type": "Feature", "id": i, "properties": props, "geometry": geom}


@app.get("/api/buildings")
def api_buildings(bbox: str = Query(..., description="minlon,minlat,maxlon,maxlat")):
    """地圖立體建物，OSM 建物（每棟分開）、市府建物圖、實價門牌方柱三種。
    OSM 與建物圖重疊時（建物圖外框有 30% 以上被 OSM 蓋住）：
    - 臺中建物圖（對實價 53%）不如 OSM（70%）→ 畫 OSM。
    - 臺北建照套繪圖（96%）比 OSM（64%）準 → 只有 OSM 最高樓層和建照一致時才改畫 OSM
      （例：富邦 A25 塔樓 54 層一致，OSM 把同建照的美術館分開畫）；不一致時（例：遼寧街 177 號建照 20 層、
      OSM 標 19 層）保留建照外框，並拿掉大半落在框內的 OSM 建物。
    高度：OSM 有標 height 就用它（公尺），否則 樓層×3.2m；樓層（上色）實價優先，其次 OSM building:levels／建物圖。"""
    x0, y0, x1, y1 = map(float, bbox.split(","))
    box = [x0, x1, y0, y1]
    osm: list[tuple] = []  # (uid, floors, height, name, geom)
    has_osm = _has_table("osm_buildings")
    if has_osm:
        rows = _cur().execute(f"""
            SELECT uid, coalesce(trade_floors, levels), height, name, wkb FROM osm_buildings
            WHERE maxlon >= ? AND minlon <= ? AND maxlat >= ? AND minlat <= ? AND wkb IS NOT NULL
            LIMIT {MAX_BUILDINGS}
        """, box).fetchall()
        osm = [(u, f, h, name, shapely.from_wkb(w)) for u, f, h, name, w in rows]
    rows = _cur().execute(f"""
        SELECT uid, floors_above, source, wkb FROM buildings
        WHERE maxlon >= ? AND minlon <= ? AND maxlat >= ? AND minlat <= ? AND wkb IS NOT NULL
        LIMIT {MAX_BUILDINGS + 1}
    """, box).fetchall()
    truncated = len(rows) > MAX_BUILDINGS
    city = []
    drop_osm: set[int] = set()
    tree = shapely.STRtree([o[4] for o in osm]) if osm else None
    for u, f, src, w in rows[:MAX_BUILDINGS]:
        g = shapely.from_wkb(w)
        if g is None or g.is_empty:
            continue
        if tree is not None and g.area:
            hit = tree.query(g, predicate="intersects")
            inter = {int(k): shapely.intersection(g, osm[k][4]).area for k in hit}
            if sum(inter.values()) / g.area >= CITY_COVERED_MAX:
                top = max((osm[k][1] for k in inter if osm[k][1] is not None), default=None)
                if not str(src).startswith(TP_PERMIT_SOURCE) or top == f:
                    continue  # 改畫 OSM
                # 保留臺北建照外框；大半在框內的 OSM 建物不畫，避免兩個立體疊在一起
                drop_osm |= {k for k, a in inter.items() if osm[k][4].area and a / osm[k][4].area >= 0.5}
        city.append((u, f, g))
    feats: list[dict] = []
    for i, (u, f, h, name, g) in enumerate(osm):
        if i not in drop_osm:
            feats.append(_feature(len(feats), json.loads(shapely.to_geojson(g)), uid=u, floors=f,
                                  h=_extrude_height(f, h), name=name, src="osm"))
    for u, f, g in city:
        feats.append(_feature(len(feats), json.loads(shapely.to_geojson(g)), uid=u, floors=f,
                              h=f * FLOOR_M if f else None, src="city"))
    # 沒有任何外框的門牌：有實價總樓層就畫成小方柱
    in_osm = "AND NOT EXISTS (SELECT 1 FROM door_osm o WHERE o.county = a.county AND o.town = a.town AND o.base = a.base)"
    pts = _cur().execute(f"""
        SELECT a.county || a.town || a.base, avg(a.lon), avg(a.lat), any_value(t.floors)
        FROM addresses a JOIN door_trade t USING (county, town, base)
        WHERE a.bldg_uid IS NULL AND a.lon BETWEEN ? AND ? AND a.lat BETWEEN ? AND ? {in_osm if has_osm else ""}
        GROUP BY a.county, a.town, a.base LIMIT {MAX_BUILDINGS}
    """, [x0, x1, y0, y1]).fetchall()
    half = PILLAR_M / 2 / 111320
    for addr, lon, lat, f in pts:
        dx = half / math.cos(math.radians(lat))
        ring = [[lon - dx, lat - half], [lon + dx, lat - half], [lon + dx, lat + half], [lon - dx, lat + half],
                [lon - dx, lat - half]]
        feats.append(_feature(len(feats), {"type": "Polygon", "coordinates": [ring]}, uid=None, floors=f,
                              h=f * FLOOR_M, addr=addr, pillar=True, src="pillar"))
    return {"type": "FeatureCollection", "features": feats, "truncated": truncated}


def _osm_building(uid: str) -> dict:
    cur = _cur()
    r = cur.execute("""SELECT uid, name, building, levels, trade_floors, height,
                       (minlon + maxlon) / 2, (minlat + maxlat) / 2 FROM osm_buildings WHERE uid = ?""", [uid]).fetchone()
    if not r:
        raise HTTPException(404)
    u, name, _btype, levels, tf, h, lon, lat = r
    addrs = cur.execute("SELECT town || base FROM door_osm WHERE osm_uid = ? ORDER BY 1 LIMIT 30", [uid]).fetchall()
    n = cur.execute("SELECT count(*) FROM door_osm WHERE osm_uid = ?", [uid]).fetchone()[0]
    src = "實價登錄（棟內門牌）" if tf is not None else "OpenStreetMap"
    note = []
    if name:
        note.append(name)
    if levels is not None and tf is not None and levels != tf:
        note.append(f"OSM 標 {levels} 層、實價登錄 {tf} 層，採實價登錄")
    if h is not None:
        shown = tf if tf is not None else levels
        ok = _extrude_height(shown, h) == h
        note.append(f"OSM 標高 {h:g} m" + ("" if ok else "（與樓層不符，地圖未採用）"))
    return {"uid": u, "source": src, "floors_above": tf if tf is not None else levels, "floors_below": None,
            "structure": None, "btype": None, "year": None,
            "year_kind": None, "lon": lon, "lat": lat, "addresses": [a for (a,) in addrs], "n_units": n,
            "note": "；".join(note), "osm": True}


@app.get("/api/building/{uid}")
def api_building(uid: str):
    if uid.startswith("osm:"):
        return _osm_building(uid)
    cur = _cur()
    b = cur.execute("""SELECT uid, county, source, floors_above, floors_below, structure, btype,
                       year, year_kind, lon, lat FROM buildings WHERE uid = ?""", [uid]).df()
    if b.empty:
        raise HTTPException(404)
    addrs = cur.execute("""
        SELECT DISTINCT town || base AS addr FROM addresses WHERE bldg_uid = ? ORDER BY 1 LIMIT 30
    """, [uid]).fetchall()
    n = cur.execute("SELECT count(*) FROM addresses WHERE bldg_uid = ?", [uid]).fetchone()[0]
    out = b.iloc[0].to_dict()
    out = {k: (None if pd.isna(v) else v) for k, v in out.items()}
    out.update(addresses=[a for (a,) in addrs], n_units=n)
    return out


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
