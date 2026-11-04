from sitefloor.address import parse
from sitefloor.landmarks import LANDMARKS, resolve


def test_resolve_variants():
    for text in ["台北101", "臺北 101", "臺北１０１", "Taipei101"]:
        assert resolve(text) == ("臺北市信義區信義路五段7號", text)
    assert resolve("臺北市信義區信義路五段7號") is None


def test_landmark_addresses_parse():
    names = [n for _, ns in LANDMARKS for n in ns]
    assert len(names) == len(set(names)), "地標名稱重複"
    for addr, _ in LANDMARKS:
        a = parse(addr)
        assert a.county and a.town and a.base.endswith("號"), addr
