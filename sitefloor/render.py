"""地圖立體繪製用的小工具（不碰資料庫、不用建庫套件，部署環境也能用）。"""

FLOOR_M = 3.2  # 立體高度：每層公尺數


def extrude_height(floors, height):
    """立體高度（公尺）。OSM 標的 height 與樓層不合理時（例：高雄八五大樓標 85 層、高 50 m）改用 樓層×3.2m。"""
    if floors and height and 2.5 * floors <= height <= 6 * floors:
        return height
    if floors:
        return floors * FLOOR_M
    return height
