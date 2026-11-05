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


# OSM 建物 uid → 模型
MODELS = {"osm:1159328965": taipei101}
