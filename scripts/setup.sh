#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$DIR"
command -v brew >/dev/null || { echo "Homebrew fehlt: https://brew.sh"; exit 1; }
brew install python@3.12 ffmpeg
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Fertig. Jetzt .env und config/streamers.yaml ausfüllen."
