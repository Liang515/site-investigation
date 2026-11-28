import asyncio
import io
import subprocess
import sys
import threading

import duckdb
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.requests import Request

from app import server
from app.server import MAX_BLDG_SPAN, _parse_bbox, api_buildings, api_hazards


def test_parse_bbox_ok():
    assert _parse_bbox("121.5,25.0,121.51,25.01", 0.25) == (121.5, 25.0, 121.51, 25.01)


@pytest.mark.parametrize("bbox", ["", "1,2,3", "a,b,c,d", "1,2,3,4,5"])
def test_parse_bbox_bad_format(bbox):
    with pytest.raises(HTTPException) as e:
        _parse_bbox(bbox, 0.25)
    assert e.value.status_code == 400


@pytest.mark.parametrize("bbox", ["-180,-90,180,90", "121.6,25,121.5,25.1", "nan,25,121.5,25.1", "121,25,inf,25.1"])
def test_parse_bbox_rejects_range(bbox):
    assert _parse_bbox(bbox, MAX_BLDG_SPAN) is None


def test_endpoints_skip_query_for_huge_bbox():
    # 範圍過大時不查資料庫，直接回空集合
    empty = {"type": "FeatureCollection", "features": [], "too_large": True}
    assert api_buildings("-180,-90,180,90") == empty
    assert api_hazards("flood500", "-180,-90,180,90") == empty
    with pytest.raises(HTTPException):
        api_buildings("x")


# ── 整批查詢、回報：壞檔回 400；阻塞的工作不在 event loop 執行緒上跑 ──
def _upload(name: str, raw: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(raw), filename=name)


@pytest.mark.parametrize("name,raw", [("壞.xlsx", b"not a zip"), ("空.csv", b""), ("空.xlsx", b"PK\x03\x04garbage"),
                                      ("只有標題.csv", "地址\n".encode())])
def test_batch_bad_file_is_400(name, raw):
    with pytest.raises(HTTPException) as e:
        asyncio.run(server.api_batch(_upload(name, raw)))
    assert e.value.status_code == 400


def test_feedback_posts_off_event_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_post_json", lambda *a, **k: seen.append(threading.current_thread()))
    monkeypatch.setattr(server, "_feedback_config", lambda: {"github_repo": None, "github_token": None,
                                                            "ntfy_server": "https://ntfy.invalid", "ntfy_topic": "t"})
    body = '{"address": "新北市五股區凌雲路一段83巷1之5號", "floors_above": "10"}'.encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        req = Request({"type": "http", "method": "POST", "path": "/api/feedback", "headers": []}, receive)
        return await server.api_feedback(req), threading.current_thread()

    res, loop_thread = asyncio.run(run())
    assert res == {"ok": True} and seen and seen[0] is not loop_thread


def test_vendor_files():
    r = server.vendor("maplibre-gl-6.16.0", "maplibre-gl.mjs")
    assert r.media_type == "text/javascript" and "immutable" in r.headers["cache-control"]
    assert server.vendor("maplibre-gl-6.16.0", "maplibre-gl.css").media_type == "text/css"
    assert server.common_css().media_type == "text/css"
    for pkg, name in [("maplibre-gl-6.16.0", "LICENSE.txt"), ("..", "server.py"), ("maplibre-gl-6.16.0", "nope.mjs"),
                      ("..", "charts.mjs")]:
        with pytest.raises(HTTPException):
            server.vendor(pkg, name)


def test_security_headers():
    from starlette.responses import Response

    async def call_next(req):
        return Response("ok")

    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    resp = asyncio.run(server.security_headers(req, call_next))
    assert resp.headers["x-content-type-options"] == "nosniff"
    csp = resp.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp
    assert "https://wmts.nlsc.gov.tw" in csp
    assert "worker-src 'self' blob:" in csp
    script_part = next(p for p in csp.split("; ") if p.startswith("script-src "))
    assert "'unsafe-inline'" not in script_part and "'sha256-" in script_part


def test_lazy_imports_on_startup():
    """冷啟動不預先載入 pandas 與 shapely，省雲端 CPU。"""
    code = "import sys, app.server; print('pandas' in sys.modules, 'shapely' in sys.modules)"
    out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert out == "False False"


def test_pick_towns_precomputed_and_fallback(monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE TABLE addresses (county VARCHAR, town VARCHAR, base VARCHAR)")
    con.execute("CREATE TABLE door_trade (county VARCHAR, town VARCHAR, base VARCHAR)")
    con.execute("INSERT INTO addresses VALUES ('臺北市', '大安區', '仁愛路1號'), ('臺北市', '信義區', '市府路1號')")
    con.execute("INSERT INTO door_trade VALUES ('臺北市', '大安區', '仁愛路2號')")
    monkeypatch.setattr(server, "_cur", con.cursor)
    server._has_table.cache_clear()
    server._counties.cache_clear()
    server._towns.cache_clear()
    try:
        # 沒有 pick_towns 表時退回即時彙整
        assert server._counties() == [{"name": "臺北市", "n": 2}]
        assert server._towns("臺北市") == [{"name": "大安區", "n": 2}, {"name": "信義區", "n": 1}]
        # 有 pick_towns 表時直接查預先算好的表
        con.execute("CREATE TABLE pick_towns AS SELECT '臺北市' AS county, '大安區' AS town, 99 AS n")
        server._has_table.cache_clear()
        server._counties.cache_clear()
        server._towns.cache_clear()
        assert server._counties() == [{"name": "臺北市", "n": 1}]
        assert server._towns("臺北市") == [{"name": "大安區", "n": 99}]
    finally:
        server._has_table.cache_clear()
        server._counties.cache_clear()
        server._towns.cache_clear()


def test_cdn_cache_covers_static_and_api():
    from starlette.responses import Response

    async def call_next(req):
        return Response("ok")

    for path in ("/", "/stats", "/about", "/common.css", "/charts.js", "/api/dashboard", "/api/pick"):
        req = Request({"type": "http", "method": "GET", "path": path, "headers": []})
        resp = asyncio.run(server.cdn_cache(req, call_next))
        assert "max-age=" in resp.headers.get("vercel-cdn-cache-control", ""), path
    # 查詢要記使用次數，不快取
    req = Request({"type": "http", "method": "GET", "path": "/api/lookup", "headers": []})
    resp = asyncio.run(server.cdn_cache(req, call_next))
    assert "vercel-cdn-cache-control" not in resp.headers


def test_cron_requires_secret_when_set(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")

    def req(headers):
        return Request({"type": "http", "method": "GET", "path": "/api/cron/weekly",
                        "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})

    with pytest.raises(HTTPException) as e:
        server.api_cron_weekly(req({"user-agent": "vercel-cron/1.0"}))  # 有設密鑰時只看 User-Agent 不夠
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        server.api_cron_weekly(req({"authorization": "Bearer wrong"}))
    assert e.value.status_code == 401
