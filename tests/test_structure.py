from sitefloor.trades import norm_structure


def test_mixed_structure_takes_first():
    assert norm_structure("鋼骨造、RC造(供公眾使用建築物)") == "鋼骨／鋼構"
    assert norm_structure("ＲＣ造、鋼骨造") == "RC 鋼筋混凝土"
    assert norm_structure("鋼骨鋼筋混凝土造、鋼筋混凝土造") == "SRC 鋼骨鋼筋混凝土"


def test_single_structure():
    assert norm_structure("鋼筋混凝土加強磚造") == "RC 鋼筋混凝土"
    assert norm_structure("見其他登記事項") is None


def test_sane_total():
    from sitefloor.trades import sane_total

    assert sane_total(100, "華廈(10層含以下有電梯)") is None  # 竹北市自強六街13號
    assert sane_total(10, "住宅大樓(11層含以上有電梯)") == 10  # 邊界多半是型態選錯，樓層照用
    assert sane_total(3, "住宅大樓(11層含以上有電梯)") is None
    assert sane_total(12, "公寓(5樓含以下無電梯)") is None
    assert sane_total(120, None) is None
    assert sane_total(101, "辦公商業大樓") == 101


def test_to_trades_prices():
    import pandas as pd

    from sitefloor.trades import M2_PER_PING, to_trades

    cols = {"county": "臺北市", "address": "a", "town": None, "floor": "f", "total": "t", "btype": "b", "structure": "s",
            "built": "y", "date": "d", "kind": None, "source": "x", "price": "u", "total_price": "p", "park_price": "pp",
            "note": "n"}
    df = pd.DataFrame([{"a": "臺北市大安區信義路三段1號", "f": "五層", "t": "十層", "b": None, "s": None, "y": None,
                        "d": "1150101", "u": "300,000", "p": "12,000,000", "pp": "0", "n": "親友間交易"}])
    r = to_trades(df, **cols).iloc[0]
    assert (r.unit_price, r.total_price, r.park_price, r.note) == (300000, 12000000, 0, "親友間交易")
    # 實價週報：總價萬元、單價萬元／坪 → 元、元／m²
    df.loc[0, ["u", "p", "pp"]] = ["99.17", "1200", "-"]
    r = to_trades(df, **cols, wan=True).iloc[0]
    assert r.total_price == 12000000 and abs(r.unit_price - 991700 / M2_PER_PING) < 1 and pd.isna(r.park_price)
