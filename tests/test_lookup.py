from sitefloor.lookup import _is_factory


def test_is_factory():
    assert _is_factory("工廠")
    assert _is_factory(None, "倉庫")
    assert not _is_factory("廠辦")  # 分層出售的工業大樓，推估準確率與一般建物相當
    assert not _is_factory(None, float("nan"), "住宅大樓(11層含以上有電梯)")


def test_extrude_height():
    from sitefloor.render import extrude_height

    assert extrude_height(85, 50) == 85 * 3.2  # OSM 高度標錯（85 層只標 50 m）
    assert extrude_height(31, 134) == 31 * 3.2  # 有樓層就用樓層，不同來源才能比高低
    assert extrude_height(None, 40) == 40  # 不知道樓層才用 OSM 高度
    assert extrude_height(5, None) == 16.0


class _Row:
    """_resolve 的輸入列：沒給的欄位都是缺值。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return float("nan")


def test_resolve_neighbors_without_address_point():
    from sitefloor.lookup import _resolve

    # 門牌點位資料沒有此門牌、自己也沒有實價：同巷鄰居一致時照樣推估，並提醒確認門牌存在
    r = _Row(county_q="宜蘭縣", town_q="羅東鎮", base="中正路10號", nb_floors=4.0, nb_n=3, nb_factory=False)
    got = _resolve(r, {"宜蘭縣"})
    assert got["floors_above"] == 4 and got["confidence"] == "中"
    assert "門牌點位資料沒有此門牌" in got["note"]
    # 沒寫鄉鎮市區：鄰居可能來自不同區，不推估
    r = _Row(county_q="宜蘭縣", town_q="", base="中正路10號", nb_floors=4.0, nb_n=3, nb_factory=False)
    assert _resolve(r, {"宜蘭縣"})["confidence"] == "無"
    # 也沒有鄰居：查無此門牌
    assert _resolve(_Row(county_q="宜蘭縣", town_q="羅東鎮", base="中正路10號"), {"宜蘭縣"})["note"] == "查無此門牌"


def test_resolve_nearby_trades():
    from sitefloor.lookup import _resolve

    base = {"county_q": "屏東縣", "town_q": "潮州鎮", "base": "中山路1號", "lvl": 2, "n_town": 1,
            "county": "屏東縣", "town": "潮州鎮"}
    # 附近實價全部一致：中
    got = _resolve(_Row(**base, ar_floors=3.0, ar_k=5, ar_n=5), {"屏東縣"})
    assert (got["floors_above"], got["confidence"]) == (3, "中")
    assert "都是 3 層" in got["note"]
    # 七成以上：低
    got = _resolve(_Row(**base, ar_floors=2.0, ar_k=5, ar_n=7), {"屏東縣"})
    assert (got["floors_above"], got["confidence"]) == (2, "低")
    # 不到七成：不推估
    assert _resolve(_Row(**base, ar_floors=2.0, ar_k=4, ar_n=7), {"屏東縣"})["confidence"] == "無"
    # 其他來源優先（建物圖）
    got = _resolve(_Row(**base, b_floors=4.0, b_source="臺中市建物圖114", ar_floors=2.0, ar_k=5, ar_n=5), {"屏東縣"})
    assert got["floors_above"] == 4


def test_near_sql():
    import duckdb
    import pandas as pd

    from sitefloor.derive import NEAR_GRID
    from sitefloor.lookup import _NEAR_SQL

    con = duckdb.connect()
    lat, lon = 22.55, 120.54
    m = 1 / 111320  # 約 1 公尺（緯度）
    pts = [(lat + 10 * m, lon, 3), (lat + 20 * m, lon, 3), (lat - 30 * m, lon, 3), (lat + 50 * m, lon, 2),
           (lat + 150 * m, lon, 9)]  # 超過 100 m 的不算
    con.execute("CREATE TABLE trade_pts (county VARCHAR, gy INTEGER, gx INTEGER, lon DOUBLE, lat DOUBLE, floors SMALLINT)")
    for y, x, f in pts:
        con.execute("INSERT INTO trade_pts VALUES ('屏東縣', floor(?/?), floor(?/?), ?, ?, ?)",
                    [y, NEAR_GRID, x, NEAR_GRID, x, y, f])
    con.register("qp", pd.DataFrame({"idx": [0, 1], "county": ["屏東縣", "臺東縣"], "lon": [lon, lon], "lat": [lat, lat]}))
    got = con.execute(_NEAR_SQL).fetchall()
    assert got == [(0, 3, 3, 4)]  # 100 m 內 4 個，3 個是 3 層；別的縣市不算
