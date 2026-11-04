import pandas as pd

from sitefloor.osm import _num, _tag


def test_tag():
    s = '"building:levels"=>"52","height"=>"266","name:en"=>"Fubon A25"'
    assert _tag(s, "building:levels") == "52"
    assert _tag(s, "height") == "266"
    assert _tag(s, "building:min_level") == ""
    assert _tag(None, "height") == ""


def test_num():
    got = _num(pd.Series(["52", "93.2m", "12;14", "", "abc"])).tolist()
    assert got[:3] == [52, 93.2, 12]
    assert all(pd.isna(x) for x in got[3:])
