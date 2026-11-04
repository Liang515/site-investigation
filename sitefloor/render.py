"""地圖立體繪製用的小工具（不碰資料庫、不用建庫套件，部署環境也能用）。"""

FLOOR_M = 3.2  # 立體高度：每層公尺數


def extrude_height(floors, height):
    """立體高度（公尺）。知道樓層就一律用 樓層×3.2m，讓不同來源的建物可以直接比高低
    （混用 OSM 實測高度會讓 31 層的 C棟 134 m、旁邊 30 層的臺北建照外框 96 m，看起來差很多）。
    OSM 標的 height 只用在不知道樓層的建物。"""
    if floors:
        return floors * FLOOR_M
    return height
