import pytest
from fastapi import HTTPException

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
