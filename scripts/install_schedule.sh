#!/bin/zsh
# 安裝／移除每週資料更新排程（macOS launchd）：每週一 09:00 執行 python -m sitefloor.update
# 用法：scripts/install_schedule.sh          安裝（重複執行會覆蓋）
#       scripts/install_schedule.sh remove   移除
# Mac 在排程時間睡眠中，醒來後會補跑；關機則跳過那一次。紀錄：data/logs/update.log
set -e
LABEL="tw.liang515.site-investigation.update"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [[ "$1" == "remove" ]]; then
  rm -f "$PLIST"
  echo "已移除排程"
  exit 0
fi

mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>python</string><string>-m</string><string>sitefloor.update</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/data/logs/update.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/update.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$UV"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安裝排程：每週一 09:00（$PLIST）"
