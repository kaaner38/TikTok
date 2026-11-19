"""Metriken abrufen (TikTok Display API) und Pipeline wöchentlich nachjustieren.
python stats.py fetch | optimize | report"""
import json, logging, math, random, sys, time
import numpy as np
import requests
from clipper import db, log as _log
from clipper.config import env, learned, save_learned

log = logging.getLogger("stats")
TOKEN = env("TIKTOK_ACCESS_TOKEN")
FIELDS = "id,video_description,create_time,view_count,like_count,share_count,comment_count"
SLOT_POOL = [f"{h:02d}:{m:02d}" for h in range(10, 24) for m in (0, 30)]


def _videos(limit=100):
    out, cursor = [], None
    while len(out) < limit:
        body = {"max_count": 20, **({"cursor": cursor} if cursor else {})}
        r = requests.post(f"https://open.tiktokapis.com/v2/video/list/?fields={FIELDS}",
                          headers={"Authorization": f"Bearer {TOKEN}"}, json=body, timeout=30).json()
        d = r.get("data") or {}
        out += d.get("videos", [])
        if not d.get("has_more"):
            break
        cursor = d.get("cursor")
    return out


def fetch():
    if not TOKEN:
        log.warning("TIKTOK_ACCESS_TOKEN fehlt - keine Stats")
        return
    vids = _videos()
    clips = db.q("SELECT id, caption, posted, tiktok_id FROM clips WHERE status='posted' AND posted>?",
                 time.time() - 30 * 86400)
    for c in clips:
        v = next((v for v in vids if v["id"] == c["tiktok_id"]), None)
        if not v:
            hook = (c["caption"] or "").split("\n")[0][:40]
            cand = [v for v in vids if abs(v["create_time"] - c["posted"]) < 3 * 3600
                    and (v.get("video_description") or "").startswith(hook[:25])]
            cand = cand or [v for v in vids if abs(v["create_time"] - c["posted"]) < 900]
            v = min(cand, key=lambda v: abs(v["create_time"] - c["posted"])) if cand else None
            if v:
                db.ex("UPDATE clips SET tiktok_id=? WHERE id=?", v["id"], c["id"])
        if v:
            db.ex("INSERT INTO stats VALUES(?,?,?,?,?,?)", c["id"], time.time(), v.get("view_count", 0),
                  v.get("like_count", 0), v.get("share_count", 0), v.get("comment_count", 0))
    log.info("Stats aktualisiert für %d Clips", len(clips))


def _dataset():
    rows = db.q("""SELECT c.*, s.views, s.likes, s.shares, s.comments FROM clips c
        JOIN stats s ON s.clip_id=c.id AND s.fetched=(SELECT MAX(fetched) FROM stats WHERE clip_id=c.id)
        WHERE c.status='posted' AND c.posted<?""", time.time() - 48 * 3600)
    for r in rows:
        r["perf"] = math.log1p(r["views"]) + 0.7 * math.log1p(r["likes"]) + 1.5 * math.log1p(r["shares"]) \
            + 0.5 * math.log1p(r["comments"])
    if len(rows) > 1:
        p = np.array([r["perf"] for r in rows])
        for r, z in zip(rows, (p - p.mean()) / (p.std() or 1)):
            r["z"] = float(z)
    return rows


def optimize():
    rows = _dataset()
    L = learned()
    if len(rows) < 8:
        log.info("Zu wenig Daten (%d) für Optimierung", len(rows))
        return
    # 1. Score-Gewichte: Korrelation Feature <-> Performance
    w = L["weights"]
    for k in w:
        xs = [(json.loads(r["features"] or "{}").get(k), r["z"]) for r in rows]
        xs = [(a, b) for a, b in xs if a is not None]
        if len(xs) >= 8 and np.std([a for a, _ in xs]) > 0:
            corr = float(np.corrcoef([a for a, _ in xs], [b for _, b in xs])[0, 1])
            w[k] = max(0.03, w[k] * (1 + 0.3 * corr))
    tot = sum(w.values())
    L["weights"] = {k: round(v / tot, 4) for k, v in w.items()}
    # 2. Hook-Stile
    for st in L["hook_styles"]:
        zs = [r["z"] for r in rows if r["hook_style"] == st]
        if len(zs) >= 2:
            L["hook_styles"][st] = round(max(0.2, 0.5 * L["hook_styles"][st] + 0.5 * math.exp(0.5 * np.mean(zs))), 3)
    # 3. Posting-Zeiten: schwächsten Slot ersetzen (Exploration)
    slots = {s: [r["z"] for r in rows if r["post_slot"] == s] for s in L["post_times"]}
    rated = {s: np.mean(z) for s, z in slots.items() if len(z) >= 3}
    if len(rated) >= 2:
        worst = min(rated, key=rated.get)
        if rated[worst] < -0.3:
            new = random.choice([s for s in SLOT_POOL if s not in L["post_times"]])
            L["post_times"] = sorted([s for s in L["post_times"] if s != worst] + [new])
            log.info("Slot %s -> %s", worst, new)
    # 4. Streamer-Prioritäten
    for name in {r["streamer"] for r in rows}:
        zs = [r["z"] for r in rows if r["streamer"] == name]
        if len(zs) >= 3:
            L["priority_mult"][name] = round(float(np.clip(math.exp(0.3 * np.mean(zs)), 0.5, 2.0)), 3)
    L["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save_learned(L)
    log.info("Optimiert: %s", json.dumps(L, ensure_ascii=False))
    return L


def report():
    for r in sorted(_dataset(), key=lambda r: -r["perf"])[:30]:
        print(f"#{r['id']:<5} {r['streamer'][:14]:<14} v={r['virality']} {r['views']:>8} views "
              f"{r['likes']:>6} likes {r['shares']:>5} shares  {r['hook_style']:<13} {r['post_slot']}")
    print(json.dumps(learned(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _log.setup()
    {"fetch": fetch, "optimize": optimize, "report": report}[sys.argv[1] if len(sys.argv) > 1 else "report"]()
