"""地標立體模型：外型特殊的地標，用多段疊起來的方塊畫出外型（取代該外框的單一方柱）。
高度用公開資料（官方網站、維基百科、使用執照）；各段寬度為近似值。

每個模型是一串 (外框, 底部高度 m, 頂部高度 m)，地圖用 fill-extrusion 的 base／height 疊起來。
外框是一圈座標；有中庭時是 [外圈, 中庭, …]（例：總統府日字形）。
模型函式會收到 OSM 外框（shapely，經緯度）；依外框往內收或分圈的模型（體育館屋頂、球場看台）用得到，其他不理它。
模型用真實高度（公尺），不套「樓層×3.2m」。
"""

import math

import shapely

M_PER_DEG_LAT = 110_574


def _square(lon: float, lat: float, w: float) -> list[list[float]]:
    """以 (lon, lat) 為中心、邊長 w 公尺、正南北向的正方形外框。"""
    dx = w / 2 / (111_320 * math.cos(math.radians(lat)))
    dy = w / 2 / M_PER_DEG_LAT
    return [[lon - dx, lat - dy], [lon + dx, lat - dy], [lon + dx, lat + dy], [lon - dx, lat + dy], [lon - dx, lat - dy]]


def _taper(z0: float, z1: float, w0: float, w1: float, n: int) -> list[tuple[float, float, float]]:
    """z0→z1 之間寬度由 w0 漸變到 w1，切成 n 段方塊（每段取該段中間的寬度）。"""
    step = (z1 - z0) / n
    return [(z0 + i * step, z0 + (i + 1) * step, w0 + (w1 - w0) * (i + 0.5) / n) for i in range(n)]


def taipei101(_g=None) -> list[tuple[list[list[float]], float, float]]:
    """台北101 塔樓（不含商場棟；商場棟用 OSM 分棟 台北101購物中心 畫）。
    高度：屋頂 449 m、尖塔頂 508 m（官方數據）。
    塔基（約 1–26 樓，0–116 m）往上收窄；8 節竹節各 8 層（約 27–90 樓，到 387 m），每節上寬下窄；
    91–101 樓往上分段內縮；尖塔 449–508 m。每層約 44 m 見方（總樓地板面積約 19.3 萬 m²／101 層）。"""
    lon, lat = 121.56489, 25.03359
    segs = _taper(0, 116, 50, 42, 4)
    z, module = 116, (387 - 116) / 8
    for _ in range(8):
        segs += _taper(z, z + module, 40, 47, 3)
        z += module
    segs += [(387, 408, 34), (408, 430, 27), (430, 449, 20)]
    segs += _taper(449, 508, 6, 2, 3)
    return [(_square(lon, lat, w), z0, z1) for z0, z1, w in segs]


def _local(lon: float, lat: float, deg: float, pts: list[tuple[float, float]]) -> list[list[float]]:
    """園區座標（公尺；u 沿著中軸往東、v 往北，中軸從正東逆時針轉 deg 度）→ 經緯度外框（頭尾相接）。"""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    kx = 111_320 * math.cos(math.radians(lat))
    ring = [[lon + (u * c - v * s) / kx, lat + (u * s + v * c) / M_PER_DEG_LAT] for u, v in pts]
    return ring + [ring[0]]


def _rect(l: float, w: float, du: float = 0) -> list[tuple[float, float]]:
    """沿中軸長 l、橫向寬 w 的長方形，中心往中軸方向移 du 公尺。"""
    return [(du - l / 2, -w / 2), (du + l / 2, -w / 2), (du + l / 2, w / 2), (du - l / 2, w / 2)]


def _octagon(w: float, du: float = 0) -> list[tuple[float, float]]:
    """對邊距離 w 的正八邊形，四個邊正對中軸與橫向。"""
    r = w / 2 / math.cos(math.pi / 8)
    return [(du + r * math.cos(math.pi / 8 + i * math.pi / 4), r * math.sin(math.pi / 8 + i * math.pi / 4)) for i in range(8)]


# 中正紀念堂園區的中軸（自由廣場門 → 紀念堂）：三棟的 OSM 外框都是從正東轉 -28.5 度
CKS_AXIS = -28.5


def _local_shift(pts: list[tuple[float, float]], du: float) -> list[tuple[float, float]]:
    """把外框西緣往東收 du 公尺（階梯越高越靠近台基）。"""
    u0 = min(u for u, _ in pts)
    return [(u + du if u == u0 else u, v) for u, v in pts]


def cks_hall(_g=None) -> list[tuple[list[list[float]], float, float]]:
    """中正紀念堂（坐東朝西）。總高 70 m（中文維基、管理處；英文維基寫 76 m）。
    台基 124 m 見方、高 14.5 m，西側大階梯；堂身 49 m 見方到 38.5 m；重簷八角攢尖頂到 70 m。"""
    lon, lat = 121.521821, 25.034638
    stairs = [(-85.6, -20), (-62, -20), (-62, 20), (-85.6, 20)]  # 西側大階梯：分 4 段往上
    segs = [(_rect(124, 124), 0, 14.5)]
    segs += [(_local_shift(stairs, 5.9 * i), 14.5 * i / 4, 14.5 * (i + 1) / 4) for i in range(4)]
    segs += [(_rect(49, 49), 14.5, 38.5), (_octagon(56), 38.5, 41.5), (_octagon(40), 41.5, 49)]
    segs += [(_octagon(w), z0, z1) for z0, z1, w in _taper(49, 70, 44, 4, 6)]
    return [(_local(lon, lat, CKS_AXIS, pts), z0, z1) for pts, z0, z1 in segs]


def _palace(lon: float, lat: float, hip: bool) -> list[tuple[list[list[float]], float, float]]:
    """兩廳院（1987，楊卓成）：宮殿式，紅柱、黃琉璃瓦，總高 37 m（OSM height）。
    台基約 105×103 m；堂身沿中軸 84 m、橫向 62 m；重簷屋頂到 37 m。
    廡殿頂（戲劇院）四面坡：長寬同時內縮，屋脊長＝長−寬；歇山頂（音樂廳）上半是山牆，長邊收得少。"""
    segs = [(_rect(104, 102), 0, 9), (_rect(84, 62), 9, 25), (_rect(92, 70), 25, 27.5), (_rect(76, 54), 27.5, 30)]
    l0, w0, ridge_w = 82, 60, 6
    l1 = l0 - w0 + ridge_w if hip else l0 - 26  # 屋頂最上層的長
    n = 5
    for i in range(n):
        t = (i + 0.5) / n
        segs.append((_rect(l0 + (l1 - l0) * t, w0 + (ridge_w - w0) * t), 30 + 7 * i / n, 30 + 7 * (i + 1) / n))
    return [(_local(lon, lat, CKS_AXIS, pts), z0, z1) for pts, z0, z1 in segs]


def national_theater(_g=None) -> list[tuple[list[list[float]], float, float]]:
    """國家戲劇院（自由廣場南側）：重簷廡殿頂。中心＝OSM 外框中心。"""
    return _palace(121.518203, 25.035284, hip=True)


def national_concert_hall(_g=None) -> list[tuple[list[list[float]], float, float]]:
    """國家音樂廳（自由廣場北側）：重簷歇山頂。中心＝OSM 外框中心。"""
    return _palace(121.519237, 25.037031, hip=False)


def presidential_office(_g=None) -> list[tuple[list, float, float]]:
    """總統府（1919 年完工，坐西朝東）：日字形 5 層，中央塔 11 層、高 60 m（總統府網站、維基）。
    主體 132×92 m（南北長），兩個中庭各約 42×34 m；中央塔在東側正面中間。"""
    lon, lat, deg = 121.511975, 25.039992, -6
    body = [_local(lon, lat, deg, pts) for pts in (
        [(-45, -66), (47, -66), (47, 66), (-45, 66)],  # 外圈
        [(-20, -46), (-20, -12), (22, -12), (22, -46)],  # 南中庭（中庭要和外圈反方向繞，MapLibre 才當成洞）
        [(-20, 13), (-20, 46), (22, 46), (22, 13)],  # 北中庭
    )]
    segs: list[tuple[list, float, float]] = [(body, 0, 22)]
    tower = [(22, 36, 18), (36, 48, 14), (48, 55, 10), *_taper(55, 60, 6, 2, 2)]
    segs += [(_local(lon, lat, deg, _rect(w, w, 38)), z0, z1) for z0, z1, w in tower]
    return segs


def _meters(g):
    """經緯度外框 → 以外框中心為原點的公尺座標（回傳外框與反轉換）。"""
    c = g.centroid
    kx, ky = 111_320 * math.cos(math.radians(c.y)), M_PER_DEG_LAT
    fwd = shapely.transform(g, lambda a: (a - [c.x, c.y]) * [kx, ky])
    return fwd, lambda m: shapely.transform(m, lambda a: a / [kx, ky] + [c.x, c.y])


def _coords(g) -> list:
    """shapely 面 → 模型外框（[外圈, 洞…]；多塊時取最大塊）。"""
    if g.geom_type == "MultiPolygon":
        g = max(g.geoms, key=lambda p: p.area)
    g = shapely.geometry.polygon.orient(g, 1.0)  # 外圈逆時針、洞順時針（MapLibre 依繞行方向認洞）
    return [[list(p) for p in g.exterior.coords]] + [[list(p) for p in r.coords] for r in g.interiors]


def gym_roof(g, wall: float, top: float, inset: float, n: int = 5) -> list[tuple[list, float, float]]:
    """外框當牆身到 wall 公尺，屋頂分 n 段往內收 inset 公尺，收到 top（體育館的拱形屋頂）。"""
    m, back = _meters(g)
    segs = [(_coords(g), 0, wall)]
    for i in range(n):
        shrunk = m.buffer(-inset * (i + 1) / n, join_style="mitre")
        if shrunk.is_empty:
            break
        segs.append((_coords(back(shrunk)), wall + (top - wall) * i / n, wall + (top - wall) * (i + 1) / n))
    return segs


def xinzhuang_gym(g) -> list[tuple[list, float, float]]:
    """新莊體育館（2002）：使照 91莊使字第00124號 地上 3 層、地下 2 層、建物高度 30.55 m。
    牆身到 16 m，屋頂往內收到 30.55 m。"""
    return gym_roof(g, 16, 30.55, 34)


def stadium_stands(g, top: float, n: int = 3) -> list[tuple[list, float, float]]:
    """環形看台（外框中間的洞＝球場）：從球場往外分 n 圈，越外圈越高，最外圈到 top 公尺。"""
    m, back = _meters(g)
    poly = max(getattr(m, "geoms", [m]), key=lambda p: p.area)
    if not poly.interiors:
        return [(_coords(g), 0, top)]
    field = shapely.Polygon(poly.interiors[0])
    width = poly.area / field.exterior.length * 1.5  # 平均看台寬度（放寬一點，讓最外圈蓋到外框）
    segs = []
    for i in range(n):
        band = poly.intersection(field.buffer(width * (i + 1) / n + 1)).difference(field.buffer(width * i / n))
        if not band.is_empty:
            segs.append((_coords(back(band)), 0, top * (i + 1) / n))
    return segs


def xinzhuang_baseball_stands(g) -> list[tuple[list, float, float]]:
    """新莊棒球場看台（1997 啟用；2014 年使照 103莊使字第00469號 建物高度 13.94 m）：環形看台分三圈升高到 14 m。"""
    return stadium_stands(g, 14)


# 模型已經畫進去的 OSM 分棟，地圖不另外畫
REPLACED_PARTS = {"osm:1052759759", "osm:r19378990"}

# OSM 建物 uid → 模型
MODELS = {
    "osm:1159328965": taipei101,
    "osm:1052759757": cks_hall,
    "osm:1052759776": national_theater,
    "osm:1052759775": national_concert_hall,
    "osm:r206817": presidential_office,
    "osm:230984631": xinzhuang_gym,
    "osm:r9106110": xinzhuang_baseball_stands,
    "osm:665628025": lambda _g=None: [],  # 球場整座外框（含草皮）不畫，看台另外畫
}
