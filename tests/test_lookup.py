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
