import pandas as pd

from sitefloor.gaps import category
from sitefloor.osm import _num, _tag, underground


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


def test_gap_category():
    assert category("大葉高島屋", "retail", None, None, None, None, None) == "百貨／購物中心"
    assert category("好市多北投店", "retail", "wholesale", None, None, None, None) == "量販／賣場"
    assert category("天母棒球場", "stadium", None, None, "stadium", None, None) == "運動場館"
    assert category("臺南火車站", "train_station", None, None, None, None, "station") == "交通（車站／航廈）"
    assert category(None, "apartments", None, None, None, None, None) == "住宅"


def test_underground():
    tags = pd.Series([
        '"layer"=>"-2","location"=>"underground"',  # 捷運站
        '"layer"=>"-1"',
        '"height"=>"14.5","layer"=>"-1","location"=>"surface"',  # 中正紀念堂：台基有地下樓層，建物在地面上
        '"building:levels"=>"5"',
    ])
    assert underground(tags).tolist() == [True, True, False, False]
