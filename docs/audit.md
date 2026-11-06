# 指標建物正確性檢查

越容易被查到的建物，越要確保資料正確。`sitefloor.tiers` 把建物分級，`sitefloor.audit` 比對各資料來源、列出疑似有錯的建物，查證後寫進人工校正表。

## 分級

| 級 | 條件（符合任一） |
|---|---|
| 1 地標／指標 | 有立體模型或在地標表、有維基連結的地標類建物或 8 層以上、25 層以上或 100 m 以上、有名稱且 15 層以上、實價單價在該縣市前 1% 的大樓 |
| 2 公共與場館 | 公家機關、運動場館、醫院、交通、學校、百貨／購物中心，以及其他有維基連結的 |
| 3 有名稱或中高樓 | 其他有名稱的建物，或 8 層以上 |
| 4 一般 | 其他 |

門檻是 `sitefloor/tiers.py` 開頭的常數。

## 檢查頻率（每週更新時自動跑）

- 每週：第 1 級全部，加上查詢熱度前 500 名
- 每月第一週：加第 2 級
- 每週輪流：第 3、4 級依縣市分 13 批，約一季掃完全國（進度：`data/state/audit_rotation.json`）
- 手動：`uv run python -m sitefloor.audit --scope tier1|tier2|all|county:臺北市`

## 檢查項目

| 代碼 | 意思 |
|---|---|
| overlap_drawn | 地圖上同一個位置畫了兩個立體 |
| floors_conflict | 實價、使照、OSM、市府建物圖的樓層差 2 層以上 |
| floors_unknown | 第 1、2 級查不到樓層 |
| year_suspect | 執照年比實價的建築完成年晚 5 年以上（可能是改建、增建的執照） |
| height_ratio | OSM 高度 ÷ 樓層 不合理 |
| permit_small | 有使照因建築面積太小沒採用 |
| stale_check | 人工校正超過查證期限（第 1 級 1 年、第 2 級 2 年） |

人工校正在期限內的建物不再列出樓層與年份問題。

## 查證流程

1. 在 Claude 說「跑指標建物審核」（可指定筆數或縣市）。
2. Claude 讀 `data/reports/audit_queue.csv` 前幾筆（依級別 → 查詢熱度 → 嚴重度排序），查使照、官方網站、維基百科，整理成建議表：建物、目前值、建議值、依據網址。
3. 確認後寫進 `curated/floor_corrections.csv`：`source` 填依據、`checked` 填查證日期、`note` 只寫建物名稱。
   規則性的問題（例如某類外框的重疊判斷）改程式規則，不逐棟校正。
4. 重建：`uv run python -m sitefloor.curated && uv run python -m sitefloor.osm && uv run python -m sitefloor.export`，再部署。

repo 是公開的：註解、commit 訊息、`note` 欄只描述規則或建物名稱，不寫個別建物的修正經過。
