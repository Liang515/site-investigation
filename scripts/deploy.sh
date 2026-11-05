#!/bin/zsh
# 部署到 Vercel 正式站：部署前檢查 → 部署 → 只保留最新 KEEP 個部署版本。
# Vercel 免費方案的 Function Storage 只有 10 GB，每個部署版本（含 Parquet 資料）約 400–500 MB，
# 舊版本不刪會累積超量（2026-11-05 曾因 27 個版本超過上限）。
# 用法：scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP=2

SITEFLOOR_PARQUET=1 uv run python -m sitefloor.smoke
vercel deploy --prod --yes

# 部署清單由新到舊；第 KEEP+1 個之後的刪掉（正式站一定是最新的那個）
old=$(vercel ls site-investigation 2>/dev/null \
  | grep -oE "https://site-investigation-[a-z0-9]+-[a-z0-9-]+\.vercel\.app" | awk '!seen[$0]++' | tail -n +$((KEEP + 1)))
for u in ${(f)old}; do
  [[ -n "$u" ]] && vercel rm "$u" --yes >/dev/null && echo "已刪除舊部署 $u"
done
echo "完成。保留最新 $KEEP 個部署版本。"
