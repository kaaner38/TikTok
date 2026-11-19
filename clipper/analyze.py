import json, logging, re, subprocess, tempfile
from pathlib import Path
import numpy as np
from . import db, twitch_api
from .config import env, keywords, learned

log = logging.getLogger("analyze")
_whisper = None
CHAT_DELAY = 4.0  # Chat reagiert verzögert


def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(env("WHISPER_MODEL", "large-v3-turbo"), device=env("WHISPER_DEVICE", "auto"),
                                compute_type="int8")
    return _whisper


def transcribe(chunk, lang):
    out = Path(chunk["path"] + ".words.json")
    if out.exists():
        return json.loads(out.read_text())
    segs, _ = whisper().transcribe(chunk["path"], language=lang, word_timestamps=True, vad_filter=True,
                                   condition_on_previous_text=False)
    words = [{"s": chunk["start_ts"] + w.start, "e": chunk["start_ts"] + w.end, "w": w.word.strip()}
             for seg in segs for w in (seg.words or []) if w.word.strip()]
    out.write_text(json.dumps(words, ensure_ascii=False))
    return words


def _norm(x):
    x = np.asarray(x, float)
    if not len(x) or np.ptp(x) == 0:
        return np.zeros_like(x)
    lo, hi = np.percentile(x, 50), np.percentile(x, 99.5)
    return np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1)


def _smooth(x, w=8):
    return np.convolve(x, np.ones(w) / w, mode="same") if len(x) else x


def audio_feature(path, n):
    import librosa
    with tempfile.NamedTemporaryFile(suffix=".wav") as t:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", t.name],
                       check=True)
        y, sr = librosa.load(t.name, sr=16000)
    rms = librosa.feature.rms(y=y, frame_length=8000, hop_length=8000)[0]  # 0.5 s
    db_ = librosa.amplitude_to_db(rms + 1e-9)
    per_s = np.array([db_[i * 2:i * 2 + 2].max() if len(db_[i * 2:i * 2 + 2]) else -80 for i in range(n)])
    base = np.array([np.median(per_s[max(0, i - 30):i + 1]) for i in range(n)])
    jump = per_s - base
    return 0.6 * _norm(jump) + 0.4 * _norm(per_s)


def keyword_feature(words, t0, n, lang):
    kw = keywords()
    groups = kw.get(lang) or kw["en"]
    pats = [(re.compile(r"\b" + re.escape(p) + r"\b"), 1.0) for g in groups.values() for p in g]
    x = np.zeros(n)
    for w in words:
        i = int(w["s"] - t0)
        if 0 <= i < n:
            tok = w["w"].lower()
            for p, wt in pats:
                if p.search(tok):
                    x[i] += wt
            if "!" in tok or tok.isupper() and len(tok) > 2:
                x[i] += 0.5
    # Phrasen über Wortgrenzen
    text_idx = [(int(w["s"] - t0), w["w"].lower()) for w in words]
    for k in range(len(text_idx) - 2):
        tri = " ".join(t for _, t in text_idx[k:k + 3])
        for p, wt in pats:
            if " " in p.pattern and p.search(tri):
                i = text_idx[k][0]
                if 0 <= i < n:
                    x[i] += wt
    # Sprechtempo-Ausbrüche
    rate = np.zeros(n)
    for i, _ in text_idx:
        if 0 <= i < n:
            rate[i] += 1
    return 0.7 * _norm(_smooth(x, 5)) + 0.3 * _norm(_smooth(rate, 5))


def chat_feature(chat_path, t0, n):
    if not chat_path.exists():
        return None
    emotes = {e.lower() for e in keywords().get("emotes", [])}
    cnt, emo = np.zeros(n), np.zeros(n)
    all_ts = []
    with open(chat_path, encoding="utf-8") as f:
        for line in f:
            try:
                m = json.loads(line)
            except Exception:
                continue
            all_ts.append(m["t"])
            i = int(m["t"] - CHAT_DELAY - t0)
            if 0 <= i < n:
                cnt[i] += 1
                toks = m["m"].lower().split()
                if toks and (sum(t in emotes for t in toks) / len(toks) > 0.5 or len(set(toks)) == 1 and len(toks) > 2):
                    emo[i] += 1
    if not all_ts:
        return None
    vel = _smooth(cnt, 10)
    base = max(np.median(vel), 0.2)
    return 0.6 * _norm(vel / base) + 0.4 * _norm(_smooth(emo, 10))


def scene_feature(path, n):
    try:
        from scenedetect import detect, ContentDetector
        cuts = detect(path, ContentDetector(threshold=27), show_progress=False)
        x = np.zeros(n)
        for s, _ in cuts:
            i = int(s.get_seconds())
            if 0 <= i < n:
                x[i] = 1
        return _norm(_smooth(x, 10))
    except Exception as e:
        log.warning("scenedetect: %s", e)
        return np.zeros(n)


def clip_feature(streamer, t0, n):
    if streamer["platform"] != "twitch":
        return None
    x = np.zeros(n)
    for t, views in twitch_api.top_clip_moments(streamer["channel"]):
        i = int(t - t0)
        if -30 <= i < n + 30:
            for k in range(max(0, i - 15), min(n, i + 15)):
                x[k] = max(x[k], np.log1p(views))
    return _norm(x) if x.any() else None


def analyze_chunk(chunk, streamer, rec):
    n = int(chunk["duration"])
    t0 = chunk["start_ts"]
    words = transcribe(chunk, streamer["language"])
    feats = {
        "audio": audio_feature(chunk["path"], n),
        "keywords": keyword_feature(words, t0, n, streamer["language"]),
        "chat": chat_feature(Path(rec["dir"]) / "chat.jsonl", t0, n),
        "scene": scene_feature(chunk["path"], n),
        "twitch_clip": clip_feature(streamer, t0, n),
    }
    w = learned()["weights"]
    active = {k: v for k, v in feats.items() if v is not None}
    tot = sum(w[k] for k in active)
    score = sum(w[k] / tot * _smooth(active[k], 6) for k in active)
    picks, s = [], score.copy()
    for _ in range(4):
        i = int(np.argmax(s))
        if s[i] < 0.35:
            break
        picks.append(i)
        s[max(0, i - 90):i + 90] = 0
    out = []
    for i in picks:
        sl = slice(max(0, i - 5), i + 5)
        fv = {k: round(float(v[sl].mean()), 3) for k, v in active.items()}
        out.append(dict(peak_ts=t0 + i, score=float(score[i]), features=fv))
    return out


def run_pending(streamer_map):
    for c in db.q("SELECT c.*, r.streamer, r.dir FROM chunks c JOIN recordings r ON r.id=c.recording_id "
                  "WHERE c.status='new' ORDER BY c.start_ts LIMIT 3"):
        s = streamer_map.get(c["streamer"])
        if not s:
            db.ex("UPDATE chunks SET status='skipped' WHERE id=?", c["id"])
            continue
        try:
            db.ex("UPDATE chunks SET status='analyzing' WHERE id=?", c["id"])
            for cand in analyze_chunk(c, s, {"dir": c["dir"]}):
                db.ex("INSERT INTO candidates(streamer,recording_id,peak_ts,win_start,win_end,score,features,created)"
                      " VALUES(?,?,?,?,?,?,?,?)", s["name"], c["recording_id"], cand["peak_ts"],
                      cand["peak_ts"] - 60, cand["peak_ts"] + 120, cand["score"], json.dumps(cand["features"]),
                      db.now())
            db.ex("UPDATE chunks SET status='analyzed' WHERE id=?", c["id"])
            log.info("Chunk analysiert: %s %s", s["name"], Path(c["path"]).name)
        except Exception as e:
            log.error("Analyse %s: %s", c["path"], e)
            db.ex("UPDATE chunks SET status='error' WHERE id=?", c["id"])


def words_between(rec_id, a, b):
    out = []
    for c in db.q("SELECT path FROM chunks WHERE recording_id=? AND start_ts<? AND start_ts+duration>? "
                  "ORDER BY start_ts", rec_id, b, a):
        p = Path(c["path"] + ".words.json")
        if p.exists():
            out += [w for w in json.loads(p.read_text()) if a <= w["s"] < b]
    return out
