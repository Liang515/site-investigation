from sitefloor.tiers import assign_tier


def test_tier1_reasons():
    assert assign_tier({"model": True})[0] == 1
    assert assign_tier({"wiki": True, "cat": "運動場館"}) == (1, ["維基"])
    assert assign_tier({"wiki": True, "cat": "宗教", "floors": 9}) == (1, ["維基"])
    # 有維基但只是學校、廟宇等小建物：第 2 級
    assert assign_tier({"wiki": True, "cat": "學校"}) == (2, ["學校"])
    assert assign_tier({"wiki": True, "cat": "宗教"}) == (2, ["維基"])
    assert assign_tier({"floors": 30})[0] == 1
    assert assign_tier({"height": 120, "floors": 20})[0] == 1
    assert assign_tier({"name": "某某社區", "floors": 16})[0] == 1
    # 高單價只算大樓
    assert assign_tier({"top_price": True, "floors": 12})[0] == 1
    assert assign_tier({"top_price": True, "floors": 2})[0] == 4


def test_tier2_to_4():
    assert assign_tier({"cat": "運動場館"}) == (2, ["運動場館"])
    assert assign_tier({"name": "某某大樓", "floors": 5})[0] == 3
    assert assign_tier({"floors": 9})[0] == 3
    assert assign_tier({"floors": 4, "cat": "住宅"}) == (4, [])
