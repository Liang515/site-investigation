import pytest

from sitefloor.address import cn2int, parse

CASES = [
    ("台中市西屯區市政路390號14樓", ("臺中市", "西屯區", "市政路390號", 14, "")),
    ("407臺中市西屯區市政路３９０號十四樓之２", ("臺中市", "西屯區", "市政路390號", 14, "之2")),
    ("臺中市中區大誠里16鄰大誠街39巷2之3之2號", ("臺中市", "中區", "大誠街39巷2之3之2號", None, "")),
    ("台北市大安區忠孝東路4段100號B1", ("臺北市", "大安區", "忠孝東路四段100號", -1, "")),
    ("新北市板橋區文化路1段2-3號5F", ("新北市", "板橋區", "文化路一段2之3號", 5, "")),
    ("臺中市大里區大明路73號之2", ("臺中市", "大里區", "大明路73之2號", None, "")),
    ("臺北縣板橋市中山路一段161號", ("新北市", "板橋區", "中山路一段161號", None, "")),
    ("高雄市新興區新村路10號", ("高雄市", "新興區", "新村路10號", None, "")),
    ("臺東縣臺東市中華路一段1號", ("臺東縣", "臺東市", "中華路一段1號", None, "")),
    ("台灣台北市信義區信義路五段7號", ("臺北市", "信義區", "信義路五段7號", None, "")),
    ("臺灣省臺中縣大里市大明路73號", ("臺中市", "大里區", "大明路73號", None, "")),
]


@pytest.mark.parametrize("text,want", CASES)
def test_parse(text, want):
    a = parse(text)
    assert (a.county, a.town, a.base, a.floor, a.unit) == want


def test_cn2int():
    assert [cn2int(x) for x in ["十", "十四", "二十", "二十二", "一百零五", "7"]] == [10, 14, 20, 22, 105, 7]


def test_parts_skip_town_detection():
    a = parse("中山路1號", county="臺北市", town="中山區")
    assert a.base == "中山路1號"


def test_town_normalized_and_validated():
    assert parse("知本路3段331巷4號", county="臺東縣", town="台東市").town == "臺東市"
    a = parse("新竹市新竹市中華路四段451巷162號", county="新竹市", town="新竹市")
    assert (a.town, a.base) == ("", "中華路四段451巷162號")


def test_variant_chars_and_legacy_town_with_given_town():
    assert parse("宜蘭縣宜蘭巿弘志路67號").town == "宜蘭市"
    assert parse("平鎮市中正二路2巷4號", county="桃園市", town="平鎮區").base == "中正二路2巷4號"


def test_taiwan_boulevard_kept():
    # 「臺灣大道」不可被當成國名去掉（曾讓臺中整條臺灣大道的門牌對不到）
    assert parse("臺灣大道三段99號", county="臺中市", town="西屯區").base == "臺灣大道三段99號"
    assert parse("台中市西屯區台灣大道三段99號").base == "臺灣大道三段99號"


def test_ambiguous_town_does_not_guess_county():
    # 信義區在臺北市、基隆市都有：只寫區名時不要猜縣市（之前會變成基隆市，查不到）
    a = parse("信義區忠孝東路四段521號")
    assert a.county == "" and a.town == "信義區" and a.base == "忠孝東路四段521號"
    assert parse("基隆市信義區信二路1號").county == "基隆市"
    assert parse("板橋區中山路一段152號").county == "新北市"  # 只有一個縣市有的區照補
