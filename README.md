# 地址 → 建物樓層查詢

輸入台灣地址，回傳建物地上樓層、構造、建物型態，並附上資料來源和信心等級。

## 使用

```bash
uv run uvicorn app.server:app --port 8765   # 開 http://localhost:8765
```

- **單筆**：在搜尋框輸入地址；地圖會飛到該建物，並以 3D 顯示樓層。
- **用選的**：預設分頁，依序點選縣市 → 鄉鎮市區 → 路街 → 巷弄 → 門牌號，不用打字；路和門牌太多時可以篩選。綠點表示這個門牌自己有實價登錄。
- **打字（模糊搜尋）**：邊打邊列出最接近的真實門牌，可用滑鼠或上下鍵＋Enter 選。錯字（市正路→市政路）、簡寫（台中西屯→臺中市西屯區）、區名錯字（板僑區→板橋區）、只打一半都可以；沒有這個號碼時列最接近的號碼。查不到時結果下方會出現「你是不是要找」。
- **地標**：約 35 個常用地標名稱（台北101、85大樓、各市政府、車站…）可以直接查，清單在 `sitefloor/landmarks.py`。公共建物很少買賣，常常查不到樓層。
- **整批**：把 Excel/CSV 拖到「整批查詢」。程式會自動找含「地址」或「門牌」的欄位，回傳 `<檔名>_樓層.xlsx`，裡面有「結果」和「摘要」兩個工作表。

地圖立體的優先順序：
1. **OpenStreetMap 建物**（每棟分開；全臺 17 萬棟可畫）：高度用 OSM 標的公尺數，沒有就用 樓層×3.2m；樓層以棟內門牌的實價登錄優先，其次 OSM 標註。
2. **市府建物圖**：臺中市全市、臺北市民國 90 年後的建照。和 OSM 重疊（外框 30% 以上被蓋住）時：
   - 臺中建物圖（對實價 53%）不如 OSM（70%），畫 OSM。
   - 臺北建照套繪圖（96%）比 OSM（64%）準，只有 OSM 最高樓層和建照一致時才改畫 OSM（例：富邦 A25 塔樓 54 層一致，OSM 把同建照的美術館分開畫）；不一致時保留建照（例：遼寧街 177 號建照 20 層、OSM 誤標 19 層）。
3. **方柱**：以上都沒有、但門牌有實價樓層的，畫成 10 公尺見方的方柱。

信心等級（依序取第一個有資料的來源；準確率是 2026-11-04 的留一驗證，`scripts/validate_infer.py`）：

| 信心 | 樓層來源 | 完全相符 |
|---|---|---|
| **高** | 這個門牌自己的實價登錄 | — |
| **高** | 同一棟（建物圖）或同一點位的其他門牌有實價，而且樓層一致 | 94–98% |
| **高** | 同一棟（OpenStreetMap 外框）的其他門牌有實價，而且樓層一致 | 97% |
| **高** | 使用執照：臺北市使照、臺南市公寓大廈清冊 | 95–96% |
| **中** | 之號門牌沒有實價，用主號的實價 | — |
| **中** | 使用執照：新北市建築執照存查、新竹市使照 | 92% |
| **中** | 臺北市建照套繪圖，門牌點在外框內（同一建照的低矮附屬建物可能被高估，所以不給高） | 96% |
| **中** | 同一條路巷、同一側、前後 6 號內至少 2 個門牌有實價，彼此最多差一層 | 93% |
| **低** | 門牌落在 OpenStreetMap 建物內，取 OSM 標註的樓層（building:levels） | 72% |
| **低** | 同一棟的其他門牌有實價，但樓層不一致（取眾數） | 62% |
| **低** | 只有臺中建物圖，高樓常被低估 | 53% |
| **待確認** | 地址沒寫鄉鎮市區，而且多個區都有這個門牌 | |
| **無** | 門牌存在，但沒有實價也推估不出來；或查不到門牌 | |

- 工廠／倉庫用同一棟或同一條巷子推估時，信心各降一級：同一棟 高→中（82%），同一條巷子 中→低（工廠 80%、倉庫 58%）。「廠辦」是分層出售的工業大樓，準確率和一般建物相當，不降。
- 實價登錄和建物圖樓層不同時，採實價登錄，並在「說明」欄註明。
- 年份與構造：實價登錄 > 使用執照（竣工年；對實價差一年內 95–98%，構造相符 96–98%）> 建物圖。地下樓層：使照 > 臺中建物圖。
- 使照只收有門牌的：臺南市使照、新竹縣使照只有地號，對不到地址。臺北、新北使照只有民國 90 年後。

## 建庫

```bash
uv run python -m sitefloor.build taichung taipei   # 建物圖、門牌點位 → data/db/sitefloor.duckdb
uv run python -m sitefloor.trades plvr             # 實價登錄 → trades
uv run python -m sitefloor.derive                  # 推估用的衍生表（同棟、同巷）
uv run python -m sitefloor.permits                 # 使用執照（data/raw/permits/，來源見 sitefloor/permits.py）
uv run python -m sitefloor.osm                     # OpenStreetMap 建物（data/raw/osm/taiwan-latest.osm.pbf，Geofabrik 下載）
uv run python -m sitefloor.coverage                # 各縣市涵蓋率（網頁統計表）
uv run python -m sitefloor.export                  # 部署用 Parquet（data/serve/，每檔 80 MB 左右）
uv run pytest -q
```

這幾步要依序跑；任何一步的資料更新後，它後面的步驟都要重跑。

建庫前要先停掉 server（DuckDB 一次只能有一個寫入者）。

## 全國進度（2026-11-04）

樓層主要來源是**實價登錄**（101S4–115S3 共 58 季＋本期，約 574 萬筆有門牌的交易／租賃）；建物圖只當備援。
臺中驗證（對實價登錄）：建物圖樓層完全相符 52%、差一層內 87%，21 層以上只有 24% 相符（門牌點常落在裙樓上）。

涵蓋率以門牌號（不含樓層）計：

| 縣市 | 門牌號 | 自己有實價 | 高＋中信心 | 含低信心 |
|---|---|---|---|---|
| 臺北市 | 26 萬 | 38.9% | 61.4% | 63.4% |
| 新北市 | 48 萬 | 35.2% | 54.1% | 54.5% |
| 臺中市 | 77 萬 | 20.6% | 38.6% | 98.6%（建物圖） |
| 其他 14 縣市（有門牌點位） | 383 萬 | 7–25% | 18–41% | 同左 |
| 全部 17 縣市 | 532 萬 | 21.0% | 36.2% | 45.1% |
| 基隆、南投、嘉義市、宜蘭、連江 | 沒有門牌點位 | 只能查實價有成交的門牌（無地圖定位） | | |

全國實價登錄：季檔放 `data/raw/plvr/seasons/<101S4…>.zip`、本期檔放 `data/raw/plvr/*.zip`，然後
`uv run python -m sitefloor.trades plvr && uv run python -m sitefloor.derive && uv run python -m sitefloor.coverage`。
臺中驗證：`PYTHONPATH=. uv run python scripts/validate.py 臺中市`；推估驗證：`PYTHONPATH=. uv run python scripts/validate_infer.py`。

臺北市實價週報：`uv run python -m sitefloor.trades taipei_weekly`（每次另存一份快照到 `data/raw/tp_weekly/`，累積後去重；要每週跑才會累積）。

門牌點位下載：`uv run python scripts/fetch_addr.py [縣市…]`（只取最新版本）。

## 結構

```
sitefloor/address.py   地址正規化（全形、台/臺、段號、樓層、之號、舊縣市名）
sitefloor/build.py     各來源 → DuckDB（buildings, addresses）
sitefloor/trades.py    實價資料 → trades 表（全國實價登錄季檔／本期、臺北市週報）
sitefloor/derive.py    推估用的衍生表：door_trade、trade_doors（同巷）、same_bldg（同棟）
sitefloor/permits.py   使用執照（臺北、新北、新竹市、臺南公寓大廈）→ permits
sitefloor/osm.py       OpenStreetMap 建物 → osm_buildings、door_osm
sitefloor/suggest.py   模糊搜尋（路名比對用 derive 產生的 roads 表）
sitefloor/coverage.py  各縣市涵蓋率 → coverage 表
sitefloor/landmarks.py 地標名稱 → 門牌
sitefloor/lookup.py    單筆/批次查詢：實價、同棟、同巷、建物圖依信心排序合併
app/server.py          FastAPI；app/static/index.html 地圖介面
docs/                  盤點報告
data/raw/              原始資料（不進版控）
```

## 授權

- 程式碼：MIT（見 `LICENSE`）。
- 資料：不在本 repo 內（`data/` 不進版控），各資料依其原授權使用：政府開放資料依「政府資料開放授權條款－第1版」，OpenStreetMap 依 ODbL。完整出處與顯名聲明見網頁的「資料來源與授權」（`app/static/about.html`）。
- ODbL 第 4.6 條：以 OSM 產生衍生資料庫（`osm_buildings`、`door_osm`、`same_osm`）的方法即本 repo 的 `sitefloor/osm.py`。

## 公開部署注意

- 每個 IP 每分鐘最多 120 次 API 請求、每小時 5 次整批查詢；整批檔案上限 4 MB、2,000 筆（`app/server.py` 的 `RATE_LIMITS` 等常數）。
- Vercel 上傳單一檔案上限 100 MB，所以不上傳 1.1 GB 的資料庫，改上傳 `sitefloor.export` 產生的 Parquet（約 340 MB，每檔 80 MB 左右）。部署環境（`VERCEL` 環境變數，或本機設 `SITEFLOOR_PARQUET=1` 測試）由 `db.connect()` 直接讀 Parquet，查詢速度與資料庫檔相近。
- 部署：更新資料後先跑 `sitefloor.export`，再在專案根目錄 `vercel deploy --prod --yes`（從本機上傳，資料不在 git 裡）。
