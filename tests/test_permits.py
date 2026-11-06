import xml.etree.ElementTree as ET

from sitefloor.permits import taipei_usage, usage_text, use_groups


def test_use_groups():
    assert usage_text(use_groups("1F店舖,停車空間,1F夾層店舖,2F~5F集合住宅")) == "店鋪診所、住宅"
    assert usage_text(use_groups("集合住宅.店鋪.一般事務所")) == "住宅、店鋪診所、辦公"
    assert use_groups("多戶住宅(H-2集合住宅)") == ["H2"]
    assert use_groups("第四十一組 一般旅館業 (B-4)") == ["B4"]
    # 一段有兩個代碼都要；「甲.乙組(G3)」的點不是分隔
    assert use_groups("一般零售業甲組(G3)及餐飲業)B3)170平方公尺") == ["G3", "B3"]
    assert use_groups("一般零售業甲.乙組(G3)64.6平方公尺") == ["G3"]
    # B1、B2 接樓層字樣時是地下樓層，不是類組
    assert use_groups("B1~B3:停車空間、B2F機房") == []
    assert use_groups("屋頂防水隔熱工程") == []


def test_taipei_usage_by_floor():
    floors = [
        "地下3樓,面積:4000㎡,高度:5.4M,用途:一般零售業乙組(B2)(第二十組),餐飲業(B3)(第二十二組),停車空間",
        "地下2樓,面積:4000㎡,高度:4M,用途:一般零售業乙組(B2)(第二十組),餐飲業(B3)(第二十二組)",
        "地下1樓,面積:3000㎡,高度:3.6M,用途:機電設備空間、機房",
        "地上1樓,面積:3000㎡,高度:6M,用途:門廳,金融保險業(G1)(第三十組)",
        "地上2樓,面積:1000㎡,高度:3.8M,用途:機房",  # 機電層：不切開前後相同的用途
        "地上3樓,面積:1400㎡,高度:3.8M,用途:金融保險業(G1)(第三十組)",
        "突出物1樓,面積:500㎡,高度:3.8M,用途:樓梯間",
    ]
    el = ET.fromstring("<Data><建築概要>" + "".join(f"<樓層>{f}</樓層>" for f in floors) + "</建築概要></Data>")
    usage, by_floor = taipei_usage(el)
    assert usage == "金融保險、零售商場、餐飲"  # 依樓地板面積：金融 4,400、零售與餐飲各 4,000 m²
    assert by_floor == "B3–B2 零售商場、餐飲；1–3F 金融保險"
