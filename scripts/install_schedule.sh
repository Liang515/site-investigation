#!/bin/zsh
# 安裝／移除排程（macOS launchd），裝在存放最新資料的那台電腦上：
# - 每週一 09:00 資料更新：python -m sitefloor.update（成功就自動部署）   紀錄：data/logs/update.log
# - 每 5 分鐘自動部署：GitHub 有新程式就重建受影響的資料並部署（python -m sitefloor.autodeploy）
#                                                                      紀錄：data/logs/autodeploy.log
# 用法：scripts/install_schedule.sh          安裝（重複執行會覆蓋）
#       scripts/install_schedule.sh remove   移除
# Mac 在排程時間睡眠中，醒來後會補跑；關機則跳過那一次。部署用 vercel CLI（要先 vercel login）。
set -e
PREFIX="tw.liang515.site-investigation"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
# launchd 的 PATH 很短：補上 uv、vercel、node、git 所在的資料夾
TOOLPATH="$(for t in uv vercel node git; do p=$(command -v $t 2>/dev/null) && dirname "$p"; done | awk '!seen[$0]++' | paste -sd: -)"

for name in update deploy; do
  launchctl bootout "gui/$(id -u)/$PREFIX.$name" 2>/dev/null || true
done
if [[ "$1" == "remove" ]]; then
  rm -f "$HOME/Library/LaunchAgents/$PREFIX.update.plist" "$HOME/Library/LaunchAgents/$PREFIX.deploy.plist"
  echo "已移除排程"
  exit 0
fi
command -v vercel >/dev/null || { echo "找不到 vercel CLI（自動部署要用）：npm i -g vercel 後 vercel login"; exit 1; }

mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
# plist <名稱> <模組> <排程設定>
plist() {
  cat > "$HOME/Library/LaunchAgents/$PREFIX.$1.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$PREFIX.$1</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>python</string><string>-m</string><string>sitefloor.$2</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  $3
  <key>StandardOutPath</key><string>$ROOT/data/logs/$2.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/$2.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$TOOLPATH:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$PREFIX.$1.plist"
}
plist update update '<key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>'
plist deploy autodeploy '<key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>'
echo "已安裝排程：每週一 09:00 資料更新（成功就部署）、每 5 分鐘檢查新程式並自動部署"
echo "紀錄：$ROOT/data/logs/update.log、$ROOT/data/logs/autodeploy.log"
