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


def test_priority_categories():
    from sitefloor.gaps import category
    assert category("台北福華大飯店", "commercial", None, None, None, None, None) == "飯店"
    assert category("某某", "yes", None, None, None, None, None, "hotel") == "飯店"
    assert category("劍湖山世界樂園", "yes", None, None, None, None, None) == "樂園"
    assert category("威秀影城", "yes", None, None, None, None, None) == "電影院"
    assert category("南港展覽館", "yes", None, None, None, None, None) == "展覽館"
    assert category("國立故宮博物院", "yes", None, None, None, None, None) == "博物館／美術館"
    assert category("台大醫院", "hospital", None, None, None, None, None) == "醫院"


def test_curated_buildings_geojson():
    import geopandas as gpd

    from sitefloor.osm import CURATED_GEOJSON
    assert CURATED_GEOJSON.exists()
    gdf = gpd.read_file(CURATED_GEOJSON)
    assert len(gdf) >= 50
    assert gdf.uid.is_unique
    assert gdf.geometry.is_valid.all()
    assert (gdf.levels >= 1).all()
