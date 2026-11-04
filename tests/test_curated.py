import pandas as pd
import pytest

from sitefloor import curated


def _load(tmp_path, monkeypatch, text):
    f = tmp_path / "c.csv"
    f.write_text("county,town,base,osm_uid,floors_above,floors_below,source,note,checked\n" + text)
    monkeypatch.setattr(curated, "CSV", f)
    return curated.load()


def test_osm_only_row_has_no_base(tmp_path, monkeypatch):
    df = _load(tmp_path, monkeypatch, ",,,osm:1,9,3,src,,\n")
    assert df.base.isna().all() and df.osm_uid.iloc[0] == "osm:1"
    assert int(df.floors_above.iloc[0]) == 9 and int(df.floors_below.iloc[0]) == 3


def test_address_row_is_normalized(tmp_path, monkeypatch):
    df = _load(tmp_path, monkeypatch, "新竹縣,竹北市,自強六街１３號,,10,,使用者回報,,\n")
    assert df.base.iloc[0] == "自強六街13號" and pd.isna(df.floors_below.iloc[0])


def test_bad_rows_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _load(tmp_path, monkeypatch, ",,,,9,,src,,\n")  # 沒有門牌也沒有 osm_uid
    with pytest.raises(ValueError):
        _load(tmp_path, monkeypatch, ",,,osm:1,0,,src,,\n")  # 樓層超出範圍
