import csv

import pytest

from sitefloor import curated, feedback, review


def _fb(**kw):
    return feedback.validate({"address": "新北市五股區凌雲路一段83巷1之5號", "floors_above": "10", **kw})


def test_validate():
    fb = _fb(floors_below="2", basis="住戶\x07，電梯到 10F")
    assert fb["floors_above"] == 10 and fb["floors_below"] == 2 and "\x07" not in fb["basis"]
    assert _fb(floors_below="")["floors_below"] is None
    for bad in ({"floors_above": "0"}, {"floors_above": "111"}, {"floors_above": "十"}, {"floors_below": "21"},
                {"address": ""}):
        with pytest.raises(ValueError):
            _fb(**bad)


def test_issue_roundtrip_and_escaping():
    fb = _fb(basis="```\n@someone [x](https://evil) ```json {}", contact="a@b.c")
    body = feedback.issue(fb)["body"]
    assert "```\n@someone" not in body  # 使用者輸入不能關掉程式碼區塊
    got = feedback.parse_issue(body)
    assert got["floors_above"] == 10 and got["address"] == fb["address"]
    assert feedback.parse_issue("隨便一個 issue") is None


def test_reviewer_edits_json():
    body = feedback.issue(_fb())["body"].replace('"floors_above": 10', '"floors_above": 11')
    assert feedback.parse_issue(body)["floors_above"] == 11
    assert feedback.parse_issue(body.replace('"floors_above": 11', '"floors_above": 999')) is None


def test_merge_replaces_same_door(tmp_path, monkeypatch):
    f = tmp_path / "c.csv"
    f.write_text(",".join(curated.COLUMNS) + "\n,,,osm:1,9,3,,,src,,\n新北市,五股區,凌雲路一段83巷1之5號,,8,,,,舊,,\n")
    monkeypatch.setattr(curated, "CSV", f)
    added, replaced = review.merge([review.to_row(_fb(basis="住戶"), 12)])
    assert (added, replaced) == (0, 1)
    rows = list(csv.DictReader(f.open(encoding="utf-8")))
    assert [r["floors_above"] for r in rows] == ["9", "10"] and "#12" in rows[1]["source"]
    df = curated.load()  # 寫出來的表要能被校正表讀取
    assert df.base.iloc[1] == "凌雲路一段83巷1之5號"


@pytest.mark.parametrize(("text", "want"), [
    ("改成 18 層", {"floors_above": 18}),
    ("地上18樓，地下3層", {"floors_above": 18, "floors_below": 3}),
    ("應為 22F B2", {"floors_above": 22, "floors_below": 2}),
    ("沒有地下室", {"floors_below": 0}),
    ("地址：臺北市信義區忠孝東路四段535號，改成20層", {"address": "臺北市信義區忠孝東路四段535號", "floors_above": 20}),
    ("看起來沒問題", {}),
])
def test_corrections(text, want):
    assert feedback.corrections(text) == want


def test_apply_comments_only_trusted_latest_wins():
    fb = _fb()
    cs = [{"authorAssociation": "OWNER", "body": "改成 18 層", "createdAt": "2026-11-05T01:00:00Z"},
          {"authorAssociation": "NONE", "body": "改成 99 層", "createdAt": "2026-11-05T02:00:00Z"},
          {"authorAssociation": "OWNER", "body": "地下 2 層", "createdAt": "2026-11-05T03:00:00Z"},
          {"authorAssociation": "OWNER", "body": "改成 19 層", "createdAt": "2026-11-05T04:00:00Z"}]
    got, used = feedback.apply_comments(fb, cs)
    assert (got["floors_above"], got["floors_below"], len(used)) == (19, 2, 3)
    with pytest.raises(ValueError):
        feedback.apply_comments(fb, [{"authorAssociation": "OWNER", "body": "改成 0 層"}])


def test_building_uid():
    assert feedback.building_uid("osm:442195153") == "osm:442195153"
    assert feedback.building_uid("osm:r6821492") == "osm:r6821492"
    assert feedback.building_uid("tp:2507") == "tp:2507"
    assert feedback.building_uid("cur:xinhe_01") == "cur:xinhe_01"  # 人工補畫的外框
    for bad in ("", "osm:abc", "javascript:alert(1)", "osm:1 OR 1=1", None):
        assert feedback.building_uid(bad) == ""


def test_map_click_report_becomes_osm_row_and_keeps_height(tmp_path, monkeypatch):
    f = tmp_path / "c.csv"
    f.write_text(",".join(curated.COLUMNS) + "\n,,,osm:442195153,6,2,64,2023,維基,臺北大巨蛋,\n")
    monkeypatch.setattr(curated, "CSV", f)
    fb = feedback.validate({"address": "臺北大巨蛋", "floors_above": "7", "building": "osm:442195153"})
    body = feedback.issue(fb)["body"]
    assert "建物：osm:442195153" in body and feedback.parse_issue(body)["building"] == "osm:442195153"
    row = review.to_row(fb, 5)
    assert row["osm_uid"] == "osm:442195153" and row["base"] == ""
    assert review.merge([row]) == (0, 1)
    df = curated.load()
    assert len(df) == 1 and int(df.floors_above.iloc[0]) == 7 and df.height_m.iloc[0] == 64  # 實際高度保留
    assert int(df.year.iloc[0]) == 2023  # 竣工年保留


def test_other_kinds_need_detail_not_floors():
    fb = feedback.validate({"address": "臺北市信義區松高路77號", "kind": "shape", "detail": "應該只有一棟塔樓"})
    assert fb["kind"] == "shape" and fb["floors_above"] is None
    issue = feedback.issue(fb)
    assert issue["title"].startswith("建物形狀或立體圖錯誤") and "人工查證" in issue["body"]
    assert feedback.parse_issue(issue["body"])["detail"] == "應該只有一棟塔樓"  # 審核指令讀得回來
    with pytest.raises(ValueError):
        feedback.validate({"address": "某地址", "kind": "shape"})  # 其他類型要說明
    with pytest.raises(ValueError):
        feedback.validate({"address": "某地址", "kind": "floors"})  # 樓層錯誤要填樓層
    with pytest.raises(ValueError):
        feedback.validate({"address": "某地址", "kind": "hack", "detail": "x" * 5})


def test_old_reports_without_kind_are_floors():
    fb = feedback.validate({"address": "某地址", "floors_above": "12"})
    assert fb["kind"] == "floors" and "樓層錯誤" in feedback.summary(fb)
