import logging, time
from datetime import datetime, timedelta, timezone
import requests
from .config import env

log = logging.getLogger("twitch_api")
CID, SECRET = env("TWITCH_CLIENT_ID"), env("TWITCH_CLIENT_SECRET")
_tok = {"v": None, "exp": 0}
_cache = {}


def _headers():
    if time.time() > _tok["exp"]:
        r = requests.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": CID, "client_secret": SECRET, "grant_type": "client_credentials"}, timeout=20).json()
        _tok.update(v=r["access_token"], exp=time.time() + r["expires_in"] - 300)
    return {"Client-Id": CID, "Authorization": f"Bearer {_tok['v']}"}


def _get(path, **params):
    return requests.get(f"https://api.twitch.tv/helix/{path}", headers=_headers(), params=params, timeout=20).json()


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def top_clip_moments(login):
    """Absolute Epoch-Zeitpunkte der Top-Clips (24 h) für einen Twitch-Kanal."""
    if not (CID and SECRET):
        return []
    c = _cache.get(login)
    if c and time.time() - c[0] < 1800:
        return c[1]
    try:
        users = _get("users", login=login).get("data", [])
        if not users:
            return []
        uid = users[0]["id"]
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        clips = _get("clips", broadcaster_id=uid, started_at=since, first=50).get("data", [])
        vods = {v["id"]: _ts(v["created_at"]) for v in _get("videos", user_id=uid, type="archive", first=10).get("data", [])}
        out = []
        for cl in clips:
            if cl.get("video_id") in vods and cl.get("vod_offset") is not None:
                t = vods[cl["video_id"]] + cl["vod_offset"] + cl["duration"] / 2
            else:
                t = _ts(cl["created_at"]) - 20
            out.append((t, cl["view_count"]))
        _cache[login] = (time.time(), out)
        return out
    except Exception as e:
        log.warning("top clips %s: %s", login, e)
        return []
