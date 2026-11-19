import json, os
from pathlib import Path
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA, LOGS, REC = ROOT / "data", ROOT / "logs", ROOT / "recordings"
OUT_CLIPS, OUT_REVIEW = ROOT / "output" / "clips", ROOT / "output" / "review"
LEARNED = DATA / "learned.json"
for p in (DATA, LOGS, REC, OUT_CLIPS, OUT_REVIEW):
    p.mkdir(parents=True, exist_ok=True)


def env(k, d=None, cast=str):
    v = os.getenv(k)
    if v in (None, ""):
        return d
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "on")
    return cast(v)


MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")
MIN_VIRALITY = env("MIN_VIRALITY", 7, int)
DRAFT_MODE = env("DRAFT_MODE", True, bool)
TZ = env("TIMEZONE", "Europe/Berlin")
MAX_REC = env("MAX_PARALLEL_RECORDINGS", 3, int)
MAX_PER_DAY = env("MAX_CLIPS_PER_STREAMER_PER_DAY", 2, int)
RETENTION_H = env("RETENTION_HOURS", 48, int)
CHUNK_S = env("CHUNK_SECONDS", 600, int)
VOD_MAX_S = env("VOD_MAX_SECONDS", 10800, int)
CLIP_MIN, CLIP_MAX = 45, 180

DEFAULT_LEARNED = {
    "weights": {"audio": 0.25, "keywords": 0.25, "chat": 0.25, "scene": 0.10, "twitch_clip": 0.15},
    "hook_styles": {"curiosity": 1.0, "contrast": 1.0, "open_question": 1.0, "shock": 1.0, "story": 1.0},
    "post_times": env("POST_TIMES", "12:00,16:00,19:00,21:30").split(","),
    "priority_mult": {},
    "updated": None,
}


def streamers():
    with open(ROOT / "config" / "streamers.yaml") as f:
        items = yaml.safe_load(f)["streamers"]
    mult = learned()["priority_mult"]
    for s in items:
        s.setdefault("language", "de")
        s.setdefault("category", "gaming")
        s.setdefault("priority", 5)
        s.setdefault("credit", f"{s['platform']}: {s['channel']}")
        s["eff_priority"] = s["priority"] * mult.get(s["name"], 1.0)
    return sorted(items, key=lambda s: -s["eff_priority"])


def keywords():
    with open(ROOT / "config" / "keywords.yaml") as f:
        return yaml.safe_load(f)


def learned():
    d = json.loads(json.dumps(DEFAULT_LEARNED))
    if LEARNED.exists():
        d.update(json.loads(LEARNED.read_text()))
    return d


def save_learned(d):
    LEARNED.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def stream_url(s):
    p, c = s["platform"], s["channel"]
    if p == "twitch":
        return f"https://www.twitch.tv/{c}"
    if p == "kick":
        return f"https://kick.com/{c}"
    if p == "youtube":
        return f"https://www.youtube.com/{c}/live" if c.startswith("@") else f"https://www.youtube.com/channel/{c}/live"
    raise ValueError(p)


def vods_url(s):
    p, c = s["platform"], s["channel"]
    if p == "twitch":
        return f"https://www.twitch.tv/{c}/videos?filter=archives&sort=time"
    if p == "kick":
        return f"https://kick.com/{c}/videos"
    return f"https://www.youtube.com/{c}/streams" if c.startswith("@") else f"https://www.youtube.com/channel/{c}/streams"
