"""地標手工立體模型：OSM 只有平面外框的地標，用多段疊起來的方塊畫出外型（取代該外框的單一方柱）。

每個模型是一串 (外框, 底部高度 m, 頂部高度 m)，地圖用 fill-extrusion 的 base／height 疊起來。
模型用真實高度（公尺），不套「樓層×3.2m」。
"""

import math

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


def taipei101() -> list[tuple[list[list[float]], float, float]]:
    """台北101 塔樓（不含商場棟；商場棟用 OSM 分棟 台北101購物中心 畫）。
    高度：屋頂 449 m、尖塔頂 508 m（官方數據）。外型依公開資料估計：
    塔基（約 1–26 樓，0–116 m）往上收窄；8 節竹節各 8 層（約 27–90 樓，到 387 m），每節上寬下窄；
    91–101 樓往上分段內縮；尖塔 449–508 m。寬度依 OSM 外框與總樓地板面積（約 19.3 萬 m²／101 層 ≈ 每層 44 m 見方）估計。
    中心：OSM 外框內扣掉商場棟後、塔樓所在的東南角（與維基百科座標相差約 11 m）。"""
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


def cks_hall() -> list[tuple[list[list[float]], float, float]]:
    """中正紀念堂（坐東朝西）。總高 70 m（中文維基、管理處；英文維基寫 76 m）。
    台基：OSM 外框 147×124 m（含西側 84 階大階梯），OSM 標 height=14.5；這裡畫成 124 m 見方＋西側 4 段階梯。
    堂身：OSM 分棟「中正紀念堂銅像大廳」14.5–38.5 m、面積約 2,400 m²（49 m 見方），中心也用它的。
    重簷八角攢尖頂依照片比例估計：下簷 56 m、上層八角 40 m、攢尖收到 70 m。"""
    lon, lat = 121.521821, 25.034638
    stairs = [(-85.6, -20), (-62, -20), (-62, 20), (-85.6, 20)]  # 西側大階梯：分 4 段往上
    segs = [(_rect(124, 124), 0, 14.5)]
    segs += [(_local_shift(stairs, 5.9 * i), 14.5 * i / 4, 14.5 * (i + 1) / 4) for i in range(4)]
    segs += [(_rect(49, 49), 14.5, 38.5), (_octagon(56), 38.5, 41.5), (_octagon(40), 41.5, 49)]
    segs += [(_octagon(w), z0, z1) for z0, z1, w in _taper(49, 70, 44, 4, 6)]
    return [(_local(lon, lat, CKS_AXIS, pts), z0, z1) for pts, z0, z1 in segs]


def _palace(lon: float, lat: float, hip: bool) -> list[tuple[list[list[float]], float, float]]:
    """兩廳院（1987，楊卓成）：宮殿式，紅柱、黃琉璃瓦，總高 37 m（OSM height）。
    尺寸依 OSM 外框（約 105×103 m，含台基）與照片比例估計：台基到 9 m；堂身沿中軸 84 m、橫向 62 m，到 25 m；
    重簷：下簷 92×70 m、上層堂身 76×54 m，上層屋頂收到 37 m。
    廡殿頂（戲劇院）四面坡：長寬同時內縮，屋脊長＝長−寬；歇山頂（音樂廳）上半是山牆，長邊收得少。"""
    segs = [(_rect(104, 102), 0, 9), (_rect(84, 62), 9, 25), (_rect(92, 70), 25, 27.5), (_rect(76, 54), 27.5, 30)]
    l0, w0, ridge_w = 82, 60, 6
    l1 = l0 - w0 + ridge_w if hip else l0 - 26  # 屋頂最上層的長
    n = 5
    for i in range(n):
        t = (i + 0.5) / n
        segs.append((_rect(l0 + (l1 - l0) * t, w0 + (ridge_w - w0) * t), 30 + 7 * i / n, 30 + 7 * (i + 1) / n))
    return [(_local(lon, lat, CKS_AXIS, pts), z0, z1) for pts, z0, z1 in segs]


def national_theater() -> list[tuple[list[list[float]], float, float]]:
    """國家戲劇院（自由廣場南側）：重簷廡殿頂。中心＝OSM 外框中心。"""
    return _palace(121.518203, 25.035284, hip=True)


def national_concert_hall() -> list[tuple[list[list[float]], float, float]]:
    """國家音樂廳（自由廣場北側）：重簷歇山頂。中心＝OSM 外框中心。"""
    return _palace(121.519237, 25.037031, hip=False)


# 模型已經畫進去的 OSM 分棟，地圖不另外畫（中正紀念堂銅像大廳＝堂身）
REPLACED_PARTS = {"osm:1052759759"}

# OSM 建物 uid → 模型
MODELS = {
    "osm:1159328965": taipei101,
    "osm:1052759757": cks_hall,
    "osm:1052759776": national_theater,
    "osm:1052759775": national_concert_hall,
}
