"""Phase 0：臺中建物圖 × GIS 門牌點位的空間比對涵蓋率盤點。"""
import time

import geopandas as gpd
import pandas as pd
import pyogrio

BLDG = "data/raw/tc_gis/01-SHP/114期末_B0001_1216n.shp"
ADDR = "data/raw/tc_gis/addr11509.csv"
NEAREST_MAX_M = 10  # 門牌點常落在騎樓／路邊，容許最近建物距離

t = time.time()
bldg = pyogrio.read_dataframe(BLDG, columns=["ID", "B1B", "B1C", "B1D", "B1E"])
bldg.geometry = bldg.geometry.force_2d()
print(f"建物 {len(bldg):,} 棟（{time.time()-t:.0f}s）")
print("樓層分布（地上）：\n", bldg.B1D.describe().round(1).to_string())
print("樓層=0 或空值：", int((bldg.B1D.fillna(0) <= 0).sum()))
print("房屋型態：", bldg.B1B.value_counts(dropna=False).to_dict())
print("房屋結構：", bldg.B1C.value_counts(dropna=False).to_dict())

addr = pd.read_csv(ADDR, dtype=str)
addr = gpd.GeoDataFrame(
    addr,
    geometry=gpd.points_from_xy(addr["TWD97橫坐標"].astype(float), addr["TWD97縱坐標"].astype(float)),
    crs="EPSG:3826",
)
print(f"\n門牌點 {len(addr):,} 筆")

inside = gpd.sjoin(addr, bldg, how="left", predicate="within")
inside = inside[~inside.index.duplicated()]
hit = inside.ID.notna()
print(f"點落在建物內：{hit.sum():,}（{hit.mean():.1%}）")

miss = addr.loc[~hit]
near = gpd.sjoin_nearest(miss, bldg, how="left", max_distance=NEAREST_MAX_M, distance_col="dist")
near = near[~near.index.duplicated()]
near_hit = near.ID.notna()
print(f"未落在內、{NEAREST_MAX_M}m 內有建物：{near_hit.sum():,}（佔全部 {near_hit.sum()/len(addr):.1%}）")
total = hit.sum() + near_hit.sum()
print(f"合計可對應：{total:,}（{total/len(addr):.1%}）")

res = pd.concat([inside.loc[hit], near.loc[near_hit]])
res["match"] = ["within"] * int(hit.sum()) + ["nearest"] * int(near_hit.sum())
print("\n各行政區涵蓋率：")
cov = (res.groupby("鄉鎮市區代碼").size() / addr.groupby("鄉鎮市區代碼").size()).sort_values()
print(cov.round(3).to_string())

cols = ["鄉鎮市區代碼", "村里", "街、路段", "地區", "巷", "弄", "號", "ID", "B1B", "B1C", "B1D", "match"]
res[cols].sample(30, random_state=1).to_csv("data/phase0_sample.csv", index=False)
res[cols].to_parquet("data/phase0_addr_floor.parquet")
print(f"\n完成（{time.time()-t:.0f}s）")
