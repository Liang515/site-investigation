from itertools import pairwise

from sitefloor.models import MODELS, taipei101
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
