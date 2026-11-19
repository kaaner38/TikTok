import logging, time, threading
import requests
from .config import env

log = logging.getLogger("telegram")
TOKEN, CHAT = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
API = f"https://api.telegram.org/bot{TOKEN}"
_handlers = {}


def _ok():
    return bool(TOKEN and CHAT)


def send(text, buttons=None):
    if not _ok():
        return
    data = {"chat_id": CHAT, "text": text[:4096]}
    if buttons:
        data["reply_markup"] = {"inline_keyboard": [buttons]}
    requests.post(f"{API}/sendMessage", json=data, timeout=20)


def send_video(path, caption, buttons=None):
    if not _ok():
        return
    import json, os
    data = {"chat_id": CHAT, "caption": caption[:1024], "supports_streaming": "true"}
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": [buttons]})
    if os.path.getsize(path) > 49_000_000:
        return send(f"{caption}\n\n(Datei zu groß für Telegram: {path})", buttons)
    with open(path, "rb") as f:
        requests.post(f"{API}/sendVideo", data=data, files={"video": f}, timeout=300)


def btn(text, data):
    return {"text": text, "callback_data": data}


def on(prefix, fn):
    _handlers[prefix] = fn


def _poll():
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 50,
                             "allowed_updates": '["callback_query"]'}, timeout=60).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if not cq or str(cq["message"]["chat"]["id"]) != str(CHAT):
                    continue
                prefix, _, arg = cq["data"].partition(":")
                msg = "?"
                if prefix in _handlers:
                    try:
                        msg = _handlers[prefix](arg) or "ok"
                    except Exception as e:
                        log.exception("handler %s", prefix)
                        msg = f"Fehler: {e}"
                requests.post(f"{API}/answerCallbackQuery",
                              json={"callback_query_id": cq["id"], "text": str(msg)[:190]}, timeout=20)
        except Exception as e:
            log.warning("poll: %s", e)
            time.sleep(10)


def start_polling():
    if _ok():
        threading.Thread(target=_poll, daemon=True, name="tg-poll").start()
