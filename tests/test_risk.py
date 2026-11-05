import duckdb
import pandas as pd

from sitefloor.lookup import NO_MAP, NOT_IN_ZONE, _add_risk


def _con():
    con = duckdb.connect()
    con.execute("""CREATE TABLE door_risk AS SELECT * FROM (VALUES
        ('臺北市', '信義區', '松仁路28號', '0.5-1.0', '1.0-2.0', NULL, NULL, NULL, NULL, '商業用', '商業區'),
        ('臺南市', '東區', '大學路1號', NULL, NULL, '中潛勢', '新化斷層', NULL, NULL, NULL, NULL))
        t(county, town, base, flood500, flood650, liquefaction, fault, landslide, debris, usage, zoning)""")
    con.execute("""CREATE TABLE risk_coverage AS SELECT * FROM (VALUES
        ('flood500', '臺北市'), ('flood500', '臺南市'), ('flood650', '臺北市'), ('flood650', '臺南市'),
        ('fault', '臺北市'), ('fault', '臺南市'), ('debris', '臺北市'), ('debris', '臺南市'),
        ('landslide', '臺北市'), ('liquefaction', '臺南市')) t(kind, county)""")
    return con


def _df(rows):
    return pd.DataFrame(rows, columns=["county", "town", "base", "base_main", "lon"])


def test_add_risk_labels():
    df = _add_risk(_con(), _df([
        ("臺北市", "信義區", "松仁路28號", "松仁路28號", 121.5),   # 在淹水範圍內
        ("臺南市", "東區", "大學路1之3號", "大學路1號", 120.2),    # 用主號找到
        ("臺北市", "信義區", "松仁路30號", "松仁路30號", 121.5),   # 都不在範圍內
        ("臺北市", "信義區", "松仁路32號", "松仁路32號", None),    # 沒有座標
    ]))
    a, b, c, d = (df.iloc[i] for i in range(4))
    assert a.flood500 == "0.5-1.0" and a.usage == "商業用" and a.fault == NOT_IN_ZONE
    assert a.liquefaction == NO_MAP  # 臺北市沒有液化圖資
    assert b.liquefaction == "中潛勢" and b.fault == "新化斷層" and b.landslide == NO_MAP
    assert c.flood500 == NOT_IN_ZONE and pd.isna(c.usage)
    assert pd.isna(d.flood500)  # 沒有座標不判斷


def test_add_risk_without_tables():
    df = _df([("臺北市", "信義區", "松仁路28號", "松仁路28號", 121.5)])
    assert _add_risk(duckdb.connect(), df).equals(df)


def test_building_risk_takes_worst():
    from sitefloor.lookup import building_risk
    con = _con()
    con.execute("""INSERT INTO door_risk VALUES
        ('臺北市', '信義區', '松仁路30號', '1.0-2.0', NULL, NULL, NULL, NULL, NULL, '住家用', '商業區')""")
    doors = pd.DataFrame({"county": ["臺北市"] * 3, "town": ["信義區"] * 3,
                          "base": ["松仁路28號", "松仁路30號", "松仁路32號"], "lon": [121.5] * 3})
    r = building_risk(con, doors)
    assert r["flood500"] == "1.0-2.0"  # 棟內最深的
    assert r["fault"] == NOT_IN_ZONE and r["liquefaction"] == NO_MAP
    assert r["zoning"] == "商業區"
    assert building_risk(con, doors.iloc[0:0]) == {}


def test_add_risk_keeps_no_map_inside_covered_county():
    con = _con()  # 臺南市有液化圖資，但這個門牌沒被任何區塊蓋到（例：山區）
    con.execute(f"""INSERT INTO door_risk VALUES
        ('臺南市', '東區', '大學路3號', NULL, NULL, '{NO_MAP}', NULL, NULL, NULL, NULL, NULL)""")
    df = _add_risk(con, _df([("臺南市", "東區", "大學路3號", "大學路3號", 120.2)]))
    assert df.iloc[0].liquefaction == NO_MAP and df.iloc[0].fault == NOT_IN_ZONE
