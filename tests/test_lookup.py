from sitefloor.lookup import _is_factory


def test_is_factory():
    assert _is_factory("工廠")
    assert _is_factory(None, "倉庫")
    assert not _is_factory("廠辦")  # 分層出售的工業大樓，推估準確率與一般建物相當
    assert not _is_factory(None, float("nan"), "住宅大樓(11層含以上有電梯)")


def test_extrude_height():
    from app.server import _extrude_height

    assert _extrude_height(85, 50) == 85 * 3.2  # OSM 高度標錯（85 層只標 50 m）
    assert _extrude_height(101, 508) == 508  # 合理的標示高度照用
    assert _extrude_height(None, 40) == 40
    assert _extrude_height(5, None) == 16.0
