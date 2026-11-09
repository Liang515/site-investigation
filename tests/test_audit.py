import datetime as dt
import json

import shapely

from sitefloor import audit
from sitefloor.render import resolve_overlaps

TODAY = dt.date(2026, 11, 6)


def codes(r, overlap=None):
    return [c for c, _ in audit.check(r, TODAY, overlap)]


def test_floors_conflict_and_ratio():
    r = {"uid": "osm:1", "tier": 1, "floors": 25, "f_osm": 25, "f_city": 17, "osm_height": 12}
    assert codes(r) == ["floors_conflict", "height_ratio"]
    # 顯示的樓層有實價支持，只有 OSM 不同：OSM 標錯
    assert codes({"uid": "osm:7", "tier": 1, "floors": 18, "f_trade": 18, "f_osm": 1}) == ["osm_mislabel"]
    # 顯示使照 1 層、OSM 7 層：要查
    assert codes({"uid": "osm:8", "tier": 1, "floors": 1, "f_permit": 1, "f_osm": 7}) == ["floors_conflict"]
    assert codes({"uid": "osm:2", "tier": 3, "floors": 7, "f_trade": 7, "f_osm": 8}) == []  # 差一層不算
    # 實價與 OSM 一致，只有使照不同（一張使照包好幾棟、或增建小使照）：網站沒錯
    assert codes({"uid": "osm:9", "tier": 1, "floors": 23, "f_trade": 23, "f_permit": 28, "f_osm": 23}) == ["permit_other"]
    assert codes({"uid": "osm:10", "tier": 1, "floors": 26, "f_trade": 26, "f_permit": 2, "f_osm": 25}) == ["permit_other"]
    # 沒有 OSM 可佐證：照樣要查
    assert codes({"uid": "osm:11", "tier": 1, "floors": 25, "f_trade": 25, "f_permit": 10}) == ["floors_conflict"]


def test_unknown_year_and_small_permit():
    assert codes({"uid": "osm:3", "tier": 1, "floors": None}) == ["floors_unknown"]
    assert codes({"uid": "osm:4", "tier": 3, "floors": None}) == []  # 第 3 級不管樓層未知
    # 改建建照被當成興建年
    assert codes({"uid": "tp:1", "tier": 1, "floors": 4, "city_year": 2005, "trade_year": 1980}) == ["year_suspect"]
    assert codes({"uid": "osm:5", "tier": 2, "floors": 12, "permit_small": True}) == ["permit_small"]


def test_curated_within_limit_suppresses_other_checks():
    r = {"uid": "osm:6", "tier": 1, "floors": None, "f_osm": 25, "f_city": 17, "c_checked": "2026-10-01"}
    assert codes(r) == []
    assert codes(r, overlap="tp:9") == ["overlap_drawn"]
    # 校正過期：要重新查證，其他來源的矛盾一起列出
    assert codes({**r, "c_checked": "2025-01-01"}) == ["stale_check", "floors_conflict", "floors_unknown"]


def test_county_batches_cover_all_once():
    sizes = {c: i * 1000 for i, c in enumerate(audit.COUNTIES)}
    b = audit.county_batches(sizes)
    assert sorted(c for x in b for c in x) == sorted(audit.COUNTIES) and len(b) == audit.COUNTY_BATCHES


def test_auto_scope_rotates(tmp_path, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(audit, "ROTATION_FILE", tmp_path / "rot.json")
    tiers = pd.DataFrame({"uid": [f"u{i}" for i in range(22)], "tier": [4] * 22, "county": audit.COUNTIES})
    seen = set()
    for _ in range(audit.COUNTY_BATCHES):
        t, _, rot = audit.pick_targets(tiers, "auto", dt.date(2026, 11, 16), {})
        seen |= set(t.county)
        (tmp_path / "rot.json").write_text(json.dumps(rot))  # run() 跑完才寫
    assert seen == set(audit.COUNTIES)  # 一輪掃完全部縣市
    assert rot["next"] == audit.COUNTY_BATCHES


def _sq(x, y, w):
    d = w / 2 * 1e-5
    return shapely.box(x - d, y - d, x + d, y + d)


def test_resolve_overlaps_same_building_vs_tower():
    # 外框差不多大、OSM 樓層和建照差很多：同一棟，只畫建照
    city, drop = resolve_overlaps([("osm:a", 25, None, None, _sq(0, 0, 28))], [("tp:1", 17, "臺北市建照套繪圖", _sq(0, 0, 30))],
                                  set(), set(), "臺北市建照")
    assert [c[0] for c in city] == ["tp:1"] and drop == {0}
    # 裙樓上的塔樓：外框明顯較小，兩個都畫
    city, drop = resolve_overlaps([("osm:b", 20, None, None, _sq(0, 0, 10))], [("tp:2", 5, "臺北市建照套繪圖", _sq(0, 0, 40))],
                                  set(), set(), "臺北市建照")
    assert [c[0] for c in city] == ["tp:2"] and drop == set()
    # 建照只有 1 層、OSM 同位置 11 層：增建小建照，改畫 OSM
    city, drop = resolve_overlaps([("osm:c", 11, None, None, _sq(0, 0, 30))], [("tp:3", 1, "臺北市建照套繪圖", _sq(0, 0, 30))],
                                  set(), set(), "臺北市建照")
    assert city == [] and drop == set()


def test_resolve_overlaps_invalid_geometry():
    # 自相交（8 字形）的外框不能讓整個範圍的地圖失敗
    bowtie = shapely.Polygon([(0, 0), (3e-4, 3e-4), (3e-4, 0), (0, 3e-4), (0, 0)])
    assert not bowtie.is_valid
    city, _ = resolve_overlaps([("osm:x", 5, None, None, _sq(1.5e-4, 1.5e-4, 30))],
                               [("tc:1", 3, "臺中市建物圖", bowtie)], set(), set(), "臺北市建照")
    assert isinstance(city, list)


def test_unknown_osm_covered_by_many_small_outlines():
    # 樓層未知的 OSM 大外框被四個建物圖小外框蓋滿：不畫 OSM
    smalls = [(f"tc:{k}", 5, "臺中市建物圖", _sq(x, y, 14)) for k, (x, y) in
              enumerate([(-7e-5, -7e-5), (7e-5, -7e-5), (-7e-5, 7e-5), (7e-5, 7e-5)])]
    city, drop = resolve_overlaps([("osm:big", None, None, None, _sq(0, 0, 30))], smalls, set(), set(), "臺北市建照")
    assert drop == {0} and len(city) == 4


def test_unknown_osm_inside_known_osm():
    # 樓層未知的 OSM 外框落在有樓層的 OSM 建物裡（重複外框、大樓裡的小構造）：不畫；
    # 反過來，樓層未知的大外框裡有一棟有樓層的小建物：照畫
    osm = [("osm:mall", 8, None, None, _sq(0, 0, 40)), ("osm:dome", None, None, None, _sq(0, 0, 10)),
           ("osm:site", None, None, None, _sq(1e-3, 0, 40)), ("osm:shop", 2, None, None, _sq(1e-3, 0, 10))]
    _, drop = resolve_overlaps(osm, [], set(), set(), "臺北市建照")
    assert drop == {1}
