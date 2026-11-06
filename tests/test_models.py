from itertools import pairwise

from sitefloor.models import (
    MODELS,
    cks_hall,
    national_concert_hall,
    national_theater,
    presidential_office,
    taipei101,
)
from sitefloor.render import part_extent


def test_taipei101_stacks_to_508m():
    segs = taipei101()
    assert segs[0][1] == 0 and segs[-1][2] == 508
    for (_, _, top), (_, base, _) in pairwise(segs):
        assert abs(top - base) < 1e-6  # 一段接一段，沒有縫也沒有重疊
    assert all(ring[0] == ring[-1] for ring, _, _ in segs)
    assert "osm:1159328965" in MODELS


def test_part_extent():
    assert part_extent(6, None, 30, None) == (0, 6 * 3.2)  # 知道樓層用 樓層×3.2
    assert part_extent(9, 8, None, None) == (8 * 3.2, 9 * 3.2)  # 空橋：8 樓以下是空的
    assert part_extent(None, None, 12, 4) == (4, 12)
    assert part_extent(None, None, None, None, parent_floors=5) == (0, 16)


def _span(ring):
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return max(xs) - min(xs), max(ys) - min(ys)


def test_cks_and_ntch_models():
    hall, theater, concert = cks_hall(), national_theater(), national_concert_hall()
    assert max(z1 for _, _, z1 in hall) == 70
    assert max(z1 for _, _, z1 in theater) == max(z1 for _, _, z1 in concert) == 37
    for segs in (hall, theater, concert):
        assert all(ring[0] == ring[-1] and z0 < z1 for ring, z0, z1 in segs)
    # 廡殿頂（戲劇院）屋頂最上層比歇山頂（音樂廳）短
    assert _span(theater[-1][0])[0] < _span(concert[-1][0])[0]
    assert {"osm:1052759757", "osm:1052759776", "osm:1052759775"} <= MODELS.keys()


def test_presidential_office_has_courtyards():
    segs = presidential_office()
    body = segs[0][0]
    assert len(body) == 3 and all(r[0] == r[-1] for r in body)  # 外圈＋兩個中庭（日字形）
    assert max(z1 for _, _, z1 in segs) == 60  # 中央塔 60 m

    def signed(ring):  # 中庭和外圈要反方向繞，MapLibre 才當成洞
        return sum(a[0] * b[1] - b[0] * a[1] for a, b in pairwise(ring))

    assert all(signed(body[0]) * signed(h) < 0 for h in body[1:])
    assert "osm:r206817" in MODELS


def test_tower_crown_steps_in_to_spire():
    import shapely

    from sitefloor.models import tower_crown
    g = shapely.box(121.0, 25.0, 121.0004, 25.0008)  # 約 40×88 m
    segs = tower_crown(g, 200, 238.15, 12, 4, 244.15)
    assert segs[0][1:] == (0, 200) and segs[-1][2] == 244.15
    widths = [max(p[0] for p in ring[0]) - min(p[0] for p in ring[0]) for ring, _, _ in segs[:-1]]
    assert widths == sorted(widths, reverse=True)  # 越往上越窄
