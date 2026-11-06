"""地圖立體繪製用的小工具（不碰資料庫、不用建庫套件，部署環境也能用）。"""

import shapely

FLOOR_M = 3.2  # 立體高度：每層公尺數
TOWER_MARGIN = 2  # OSM 建物比臺北建照外框高出這麼多層以上，視為裙樓上的塔樓
TOWER_MAX_SHARE = 0.5  # 塔樓外框要小於建照外框的這個比例；差不多大的是同一棟、只是樓層不同
MINOR_PERMIT_FLOORS = 2  # 建照外框只有 1–2 層、OSM 同位置高很多：多半是舊大樓後來的增建小建照
CITY_COVERED_MAX = 0.3  # 市府外框被 OSM 建物蓋住超過此比例就改畫 OSM


def extrude_height(floors, height):
    """立體高度（公尺）。知道樓層就一律用 樓層×3.2m，讓不同來源的建物可以直接比高低
    （混用 OSM 實測高度會讓 31 層的 C棟 134 m、旁邊 30 層的臺北建照外框 96 m，看起來差很多）。
    OSM 標的 height 只用在不知道樓層的建物。"""
    if floors:
        return floors * FLOOR_M
    return height


def part_extent(levels, min_level, height, min_height, parent_floors=None):
    """OSM 分棟的 (底部, 頂部) 高度（公尺），規則和整棟一樣：知道樓層就用 樓層×3.2m。
    building:levels 是從地面算起的樓層數，building:min_level 是底下空著的樓層數（例：空橋）。"""
    top = extrude_height(levels, height) or extrude_height(parent_floors, None)
    if min_level:
        base = min_level * FLOOR_M
    else:
        base = min_height or 0
    return base, top


def _inter_area(a, b) -> float:
    """兩個外框重疊面積；來源資料有自相交等無效外框時，先修正再算（否則整個範圍的地圖都畫不出來）。"""
    try:
        return shapely.intersection(a, b).area
    except shapely.errors.GEOSException:
        return shapely.intersection(shapely.make_valid(a), shapely.make_valid(b)).area


def resolve_overlaps(osm: list[tuple], city_rows: list[tuple], modeled: set[str], trusted: set[str],
                     tp_source: str) -> tuple[list[tuple], set[int]]:
    """OSM 建物與市府外框重疊時決定畫哪個（地圖與 audit.py 共用）。
    osm：[(uid, floors, height, name, geom)]；city_rows：[(uid, floors, source, geom)]；
    modeled：有立體模型的 OSM uid；trusted：人工校正或有模型、一定要畫的 OSM uid；tp_source：臺北建照套繪圖的來源字首。
    回傳 (要畫的市府外框 [(uid, floors, geom)], 不畫的 OSM 索引)。
    - 臺中建物圖（對實價 53%）不如 OSM（70%）→ 畫 OSM。
    - 臺北建照套繪圖（96%）比 OSM（64%）準 → 只有 OSM 最高樓層和建照一致時才改畫 OSM；不一致時保留建照外框，
      並拿掉大半落在框內的 OSM 建物（比建照高很多、外框又明顯較小的是裙樓上的塔樓，照畫）。"""
    city: list[tuple] = []
    drop_osm: set[int] = set()
    # 只有「知道樓層」的 OSM 建物能取代市府外框；樓層未知的（灰色）不能蓋掉有樓層的臺中／臺北外框。
    # 有立體模型的也算知道樓層
    known = [i for i, o in enumerate(osm) if o[1] is not None or o[0] in modeled]
    tree = shapely.STRtree([osm[i][4] for i in known]) if known else None
    for u, f, src, g in city_rows:
        if g is None or g.is_empty:
            continue
        if tree is not None and g.area:
            hit = [known[int(j)] for j in tree.query(g, predicate="intersects")]
            inter = {k: _inter_area(g, osm[k][4]) for k in hit}
            if sum(inter.values()) / g.area >= CITY_COVERED_MAX:
                top = max((osm[k][1] for k in inter if osm[k][1] is not None), default=None)
                if not str(src).startswith(tp_source) or top == f or any(osm[k][0] in trusted for k in inter):
                    continue  # 改畫 OSM
                if f is not None and f <= MINOR_PERMIT_FLOORS and top is not None and top > f + TOWER_MARGIN:
                    continue  # 增建小建照，改畫 OSM
                # 保留臺北建照外框；大半在框內的 OSM 建物不畫，避免兩個立體疊在一起。
                # 但比建照高出 TOWER_MARGIN 層以上、外框又明顯較小的照畫：那是裙樓上的塔樓；
                # 差一兩層、或外框差不多大的當成同一棟
                drop_osm |= {k for k, a in inter.items() if osm[k][4].area and a / osm[k][4].area >= 0.5
                             and not (osm[k][1] is not None and f is not None and osm[k][1] > f + TOWER_MARGIN
                                      and osm[k][4].area < TOWER_MAX_SHARE * g.area)}
        city.append((u, f, g))
    # 樓層未知的 OSM 建物若大半被要畫的市府外框蓋住，不畫（市府外框有樓層）。
    # 用所有重疊外框的面積合計：一個 OSM 大外框常蓋住好幾個建物圖小外框
    unknown = [i for i, o in enumerate(osm) if o[1] is None and o[0] not in modeled]
    if unknown and city:
        ctree = shapely.STRtree([c[2] for c in city])
        for i in unknown:
            g = osm[i][4]
            if g.area and sum(_inter_area(g, city[int(j)][2])
                              for j in ctree.query(g, predicate="intersects")) / g.area >= 0.5:
                drop_osm.add(i)
    return city, drop_osm
