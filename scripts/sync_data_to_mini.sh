#!/bin/zsh
# 在「筆電」上執行：把 data/（約 3.7 GB，不在 GitHub）複製到 Mac mini。
# Mac mini 要先打開「系統設定 → 一般 → 共享 → 遠端登入」。
# 用法：zsh scripts/sync_data_to_mini.sh
# 中途斷掉就重跑同一行，會從斷掉的地方接著傳。
set -euo pipefail
cd "$(dirname "$0")/.."

MINI="${MINI:-liangchen@Mac-mini.local}"
DEST="${DEST:-site-investigation/data/}"   # 相對於 Mac mini 家目錄：~/site-investigation/data/

if [[ ! -d data ]]; then
  echo "找不到 $(pwd)/data：請在有資料的那台筆電上執行" >&2
  exit 1
fi

host="${MINI#*@}"
if ! nc -z -G 5 "$host" 22 2>/dev/null; then
  echo "連不到 $host 的遠端登入（port 22）。" >&2
  echo "請在 Mac mini 打開「系統設定 → 一般 → 共享 → 遠端登入」，並確認兩台在同一個網路。" >&2
  exit 1
fi

echo "複製 $(pwd)/data/ → $MINI:~/$DEST（$(du -sh data | cut -f1)）"
ssh "$MINI" "mkdir -p ~/$DEST"
rsync -a --partial --progress data/ "$MINI:$DEST"
echo "完成。回到 Mac mini 繼續第 4 步（vercel login）。"
