import base64, json, logging, shutil, time
from pathlib import Path
import requests
from . import db, telegram
from .config import DRAFT_MODE, MAX_PER_DAY, OUT_REVIEW, env
from .pipeline import day_start

log = logging.getLogger("poster")
BASE = env("BLOTATO_BASE_URL", "https://backend.blotato.com/v2").rstrip("/")
KEY, ACCOUNT = env("BLOTATO_API_KEY"), env("BLOTATO_TIKTOK_ACCOUNT_ID")
PUBLIC = env("PUBLIC_MEDIA_BASE_URL")


def _h():
    return {"blotato-api-key": KEY, "Content-Type": "application/json"}


def _media_url(path):
    if PUBLIC:
        src = f"{PUBLIC.rstrip('/')}/{Path(path).name}"
    else:
        src = "data:video/mp4;base64," + base64.b64encode(Path(path).read_bytes()).decode()
    r = requests.post(f"{BASE}/media", headers=_h(), json={"url": src}, timeout=600)
    r.raise_for_status()
    return r.json()["url"]


def blotato_post(clip):
    if not (KEY and ACCOUNT):
        raise RuntimeError("BLOTATO_API_KEY / BLOTATO_TIKTOK_ACCOUNT_ID fehlt")
    body = {"post": {
        "accountId": ACCOUNT,
        "content": {"text": clip["caption"], "mediaUrls": [_media_url(clip["path"])], "platform": "tiktok"},
        "target": {"targetType": "tiktok", "privacyLevel": "PUBLIC_TO_EVERYONE", "disabledComments": False,
                   "disabledDuet": False, "disabledStitch": False, "isBrandedContent": False,
                   "isYourBrand": False, "isAiGenerated": False},
    }}
    r = requests.post(f"{BASE}/posts", headers=_h(), json=body, timeout=120)
    r.raise_for_status()
    return str(r.json().get("postSubmissionId") or r.json())


def pick_best():
    blocked = {r["streamer"] for r in db.q(
        "SELECT streamer, COUNT(*) n FROM clips WHERE status IN ('posted','review') AND posted>? GROUP BY streamer "
        "HAVING n>=?", day_start(), MAX_PER_DAY)}
    for c in db.q("SELECT * FROM clips WHERE status='queued' AND created>? ORDER BY score DESC, created DESC",
                  time.time() - 72 * 3600):
        if c["streamer"] not in blocked and c["path"] and Path(c["path"]).exists():
            return c
    return None


def _publish(clip, slot):
    pid = blotato_post(clip)
    db.ex("UPDATE clips SET status='posted', posted=?, post_slot=?, post_id=? WHERE id=?",
          time.time(), slot, pid, clip["id"])
    log.info("Gepostet #%d (%s) -> %s", clip["id"], clip["streamer"], pid)
    telegram.send(f"✅ Gepostet #{clip['id']} ({clip['streamer']}, v={clip['virality']})\n{clip['caption'][:300]}")


def post_slot(slot):
    clip = pick_best()
    if not clip:
        log.warning("Slot %s: kein Clip in der Queue", slot)
        telegram.send(f"ℹ️ Slot {slot}: keine Clips in der Queue")
        return
    if DRAFT_MODE:
        dst = OUT_REVIEW / Path(clip["path"]).name
        shutil.copy2(clip["path"], dst)
        Path(dst).with_suffix(".txt").write_text(clip["caption"], encoding="utf-8")
        db.ex("UPDATE clips SET status='review', posted=?, post_slot=? WHERE id=?", time.time(), slot, clip["id"])
        a = json.loads(clip["analysis"] or "{}")
        telegram.send_video(dst, f"🎬 #{clip['id']} {clip['streamer']} | Viralität {clip['virality']}/10 | "
                                 f"Slot {slot}\n{a.get('reason', '')[:250]}\n\n{clip['caption']}",
                            [telegram.btn("✅ Posten", f"approve:{clip['id']}"),
                             telegram.btn("❌ Verwerfen", f"reject:{clip['id']}")])
    else:
        try:
            _publish(clip, slot)
        except Exception as e:
            db.ex("UPDATE clips SET status='post_error' WHERE id=?", clip["id"])
            log.error("Posting #%d fehlgeschlagen: %s", clip["id"], e)


def on_approve(arg):
    c = db.one("SELECT * FROM clips WHERE id=?", int(arg))
    if not c or c["status"] != "review":
        return "bereits erledigt"
    _publish(c, c["post_slot"])
    return "gepostet"


def on_reject(arg):
    db.ex("UPDATE clips SET status='rejected', posted=NULL WHERE id=? AND status='review'", int(arg))
    return "verworfen"


telegram.on("approve", on_approve)
telegram.on("reject", on_reject)
