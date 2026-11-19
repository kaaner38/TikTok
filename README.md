# tiktok-clipper

Vollautomatische Pipeline: Livestreams (Twitch/Kick/YouTube) aufnehmen → Highlights erkennen → Claude bewertet → 9:16-Clip mit Untertiteln → Hook-Caption → 4×/Tag auf TikTok (Blotato).

```
tiktok-clipper/
├── main.py                 # Dauerbetrieb (APScheduler)
├── stats.py                # fetch | optimize | report
├── clipper/
│   ├── config.py  db.py  log.py  media.py
│   ├── watcher.py          # Live-Check alle 3 Min, Aufnahme (max. 3), VOD-Fallback, Cleanup 48 h
│   ├── chat.py             # Twitch IRC / Kick Pusher Chat-Logger
│   ├── twitch_api.py       # Top-Clips 24 h als Signal
│   ├── analyze.py          # Audio, Whisper, Keywords, Chat, Szenen -> Kandidaten
│   ├── llm.py              # Claude: Bewertung + Captions
│   ├── pipeline.py         # Kandidat -> Clip (Dedupe, Limits)
│   ├── editor.py           # ffmpeg 1080x1920, Facecam-Split/Smart-Crop, Untertitel, Punch-in, Loudnorm
│   ├── poster.py           # Queue, Blotato, Draft-Review
│   └── telegram.py         # Benachrichtigungen + Freigabe-Buttons
├── config/streamers.yaml   config/keywords.yaml
├── launchd/com.tiktok-clipper.plist.template
├── scripts/setup.sh  scripts/install_launchd.sh
├── data/ (SQLite, learned.json)  logs/  recordings/  output/clips/  output/review/
├── requirements.txt  .env.example
```

## Setup in 5 Schritten

1. **Klonen & installieren**
   ```bash
   git clone <repo> ~/tiktok-clipper && cd ~/tiktok-clipper && ./scripts/setup.sh
   ```
2. **`.env` ausfüllen**: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (@BotFather, dann Chat-ID via `getUpdates`), `BLOTATO_API_KEY` + `BLOTATO_TIKTOK_ACCOUNT_ID`, optional `TWITCH_CLIENT_ID/SECRET`, `TIKTOK_ACCESS_TOKEN` (Display API, Scope `video.list`). `DRAFT_MODE=true` lassen.
3. **Streamer eintragen** in `config/streamers.yaml` (platform, channel, language, category, credit, priority).
4. **Testlauf** im Vordergrund: `.venv/bin/python main.py` – Logs in `logs/clipper.log`, fertige Clips in `output/clips/`, Slots landen in `output/review/` + Telegram mit ✅/❌.
5. **Dauerbetrieb**: `./scripts/install_launchd.sh` (Auto-Start, Auto-Restart). Wenn die Clips passen: `DRAFT_MODE=false`, dann `launchctl kickstart -k gui/$(id -u)/com.tiktok-clipper`.

## Hinweise

- Blotato-Endpunkte (`/v2/media`, `/v2/posts`, Header `blotato-api-key`) gegen die aktuelle Blotato-Doku prüfen; `BLOTATO_BASE_URL` ist konfigurierbar. Für große Videos `PUBLIC_MEDIA_BASE_URL` (z. B. R2/S3-Sync von `output/clips/`) statt Base64 nutzen.
- Rechte: Nur Streamer clippen, die Clipping erlauben oder zugestimmt haben; Credit steht in Caption und im Video.
- Apple Silicon: `VIDEO_ENCODER=h264_videotoolbox` in `.env` beschleunigt den Render. Whisper läuft auf CPU (int8); bei Rückstau `WHISPER_MODEL=medium`.
- Gelerntes (Gewichte, Hook-Stile, Slots, Prioritäten) liegt in `data/learned.json`; `python stats.py report` zeigt den Stand.
