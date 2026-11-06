from itertools import pairwise

from sitefloor.models import (
    MODELS,
    cks_hall,
    national_concert_hall,
    national_theater,
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
