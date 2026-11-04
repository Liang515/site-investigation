from sitefloor.suggest import split_base, variants


def test_split_base():
    assert split_base("市政路604巷5號") == ("市政路", "604巷", 5, 0, False)
    assert split_base("工業區一路98巷7弄10之2號") == ("工業區一路", "98巷7弄", 10, 2, False)
    assert split_base("中山北路四段1巷1弄臨11號") == ("中山北路四段", "1巷1弄", 11, 0, True)
    assert split_base("北屯段259-72地號") is None


def test_variants_expand_short_names():
    got = [v for v, _ in variants("台中西屯市政路387號")]
    assert "臺中市西屯區市政路387號" in got


def test_variants_fix_town_typo():
    got = dict(variants("新北市板僑區文化路一段188號"))
    assert got.get("新北市板橋區文化路一段188號") == "區名相近：板僑區→板橋區"


def test_variants_ambiguous_county_not_guessed():
    # 新竹有市有縣，不猜
    assert all(not v.startswith(("新竹市", "新竹縣")) for v, _ in variants("新竹光復路1號"))
