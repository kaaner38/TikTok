#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST="$HOME/Library/LaunchAgents/com.tiktok-clipper.plist"
sed "s#__DIR__#$DIR#g" "$DIR/launchd/com.tiktok-clipper.plist.template" > "$DST"
launchctl bootout "gui/$(id -u)/com.tiktok-clipper" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl enable "gui/$(id -u)/com.tiktok-clipper"
echo "läuft: launchctl print gui/$(id -u)/com.tiktok-clipper | head; Logs: $DIR/logs/"
