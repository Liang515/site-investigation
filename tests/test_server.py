import asyncio
import io
import threading

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


@pytest.mark.parametrize("name,raw", [("壞.xlsx", b"not a zip"), ("空.csv", b""), ("空.xlsx", b"PK\x03\x04garbage")])
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
