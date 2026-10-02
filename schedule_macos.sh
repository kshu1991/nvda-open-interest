#!/bin/bash
# Install or remove the weekday LaunchAgent that runs run_daily.sh (macOS).
#
#   ./schedule_macos.sh install 06:30 07:00 08:00   # Mon-Fri at each time
#   ./schedule_macos.sh status
#   ./schedule_macos.sh uninstall
#
# launchd fires in the system's local time zone, so the times are Eastern only
# while this Mac is set to Eastern time. Several times are cheap: run_daily.sh
# does nothing once the day's data is saved. A fire missed while the Mac was
# asleep runs once on wake.
set -euo pipefail

LABEL="com.nvda-oi.daily"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
install)
  shift
  [ $# -ge 1 ] || { echo "usage: $0 install HH:MM [HH:MM ...]" >&2; exit 1; }
  zone="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
  if [ "$zone" != "America/New_York" ]; then
    echo "WARNING: system time zone is $zone, not Eastern; times below are local." >&2
  fi
  entries=""
  for t in "$@"; do
    [[ "$t" =~ ^([01]?[0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "bad time: $t" >&2; exit 1; }
    hour=$((10#${t%%:*})); minute=$((10#${t##*:}))
    for weekday in 1 2 3 4 5; do
      entries+="    <dict><key>Weekday</key><integer>$weekday</integer>"
      entries+="<key>Hour</key><integer>$hour</integer>"
      entries+="<key>Minute</key><integer>$minute</integer></dict>"$'\n'
    done
  done
  mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/run_daily.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
$entries  </array>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST"
  echo "Installed $PLIST: Mon-Fri at $* ($zone)."
  ;;
status)
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "Loaded: $PLIST"
    launchctl print "$DOMAIN/$LABEL" | grep -E "state|last exit code|runs" || true
  else
    echo "Not installed."
  fi
  ;;
uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL."
  ;;
*)
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
  ;;
esac
