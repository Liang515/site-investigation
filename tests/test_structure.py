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
