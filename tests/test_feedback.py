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
    f.write_text(",".join(curated.COLUMNS) + "\n,,,osm:1,9,3,src,,\n新北市,五股區,凌雲路一段83巷1之5號,,8,,舊,,\n")
    monkeypatch.setattr(curated, "CSV", f)
    added, replaced = review.merge([review.to_row(_fb(basis="住戶"), 12)])
    assert (added, replaced) == (0, 1)
    rows = list(csv.DictReader(f.open(encoding="utf-8")))
    assert [r["floors_above"] for r in rows] == ["9", "10"] and "#12" in rows[1]["source"]
    df = curated.load()  # 寫出來的表要能被校正表讀取
    assert df.base.iloc[1] == "凌雲路一段83巷1之5號"
