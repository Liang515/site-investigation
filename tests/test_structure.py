from sitefloor.trades import norm_structure


def test_mixed_structure_takes_first():
    assert norm_structure("鋼骨造、RC造(供公眾使用建築物)") == "鋼骨／鋼構"
    assert norm_structure("ＲＣ造、鋼骨造") == "RC 鋼筋混凝土"
    assert norm_structure("鋼骨鋼筋混凝土造、鋼筋混凝土造") == "SRC 鋼骨鋼筋混凝土"


def test_single_structure():
    assert norm_structure("鋼筋混凝土加強磚造") == "RC 鋼筋混凝土"
    assert norm_structure("見其他登記事項") is None
