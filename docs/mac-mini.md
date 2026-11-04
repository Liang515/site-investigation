# 把每週更新搬到 Mac mini

更新排程要在常開機的電腦上跑。Mac mini 需要：程式（GitHub）、資料（`data/`，約 3.7 GB，不在 GitHub）、Vercel 部署權限、推播設定（在 `data/state/notify.json`，跟著資料一起搬）。

## 1. 安裝工具（Mac mini 上）

```bash
# 沒有 Homebrew 的話先裝：https://brew.sh
brew install uv node git
npm i -g vercel
```

## 2. 取得程式

```bash
mkdir -p ~/Documents && cd ~/Documents
git clone https://github.com/Liang515/site-investigation.git
cd site-investigation && uv sync
```

## 3. 搬資料（在原本這台筆電上執行）

先在 Mac mini 打開「系統設定 → 一般 → 共享 → 遠端登入」，再從筆電複製：

```bash
rsync -a --progress ~/Documents/site-investigation/data/ <Mac mini 使用者名稱>@<Mac mini 名稱>.local:~/Documents/site-investigation/data/
```

或用隨身硬碟把整個 `data/` 資料夾複製到 Mac mini 的 `~/Documents/site-investigation/data/`。

## 4. Vercel 部署權限（Mac mini 上）

```bash
cd ~/Documents/site-investigation
vercel login
vercel link --yes --project site-investigation
```

## 5. 測試（Mac mini 上）

```bash
uv run python -m sitefloor.update --check        # 列出哪些來源有新版（約 30 秒）
uv run python -m sitefloor.update --test-notify  # 手機應收到「地址樓層查詢：測試」
```

## 6. 安裝每週排程（Mac mini 上）

```bash
zsh scripts/install_schedule.sh
```

每週一 09:00 執行；紀錄在 `data/logs/update.log`。建議在「系統設定 → 能源」打開「顯示器關閉時防止自動進入睡眠」。

## 每週的流程

1. 週一早上手機收到推播：有哪些新資料、多少門牌異動、涵蓋率變化。
2. 要看完整報告：Mac mini 上的 `data/reports/<日期>.md`。
3. 沒問題就部署（Mac mini 上，或從筆電遠端）：
   ```bash
   ssh <Mac mini 使用者名稱>@<Mac mini 名稱>.local "zsh -lc 'cd ~/Documents/site-investigation && vercel deploy --prod --yes'"
   （`zsh -lc` 讓 ssh 載入 Homebrew 的路徑，否則找不到 `vercel`）
   ```

搬完之後，**筆電上的 `data/` 就不再是最新的**：之後部署、重建都在 Mac mini 上做。
