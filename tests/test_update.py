from sitefloor.update import NEEDS, PIPELINE, _next_season


def test_next_season():
    assert _next_season("115S3") == "115S4"
    assert _next_season("115S4") == "116S1"


def test_pipeline_covers_needs():
    for steps in NEEDS.values():
        assert steps <= set(PIPELINE)
    assert "export" in NEEDS["osm"]  # 任何更新都要重新匯出部署用 Parquet
