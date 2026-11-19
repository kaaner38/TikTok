import difflib, json, logging, tempfile, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from . import analyze, db, editor, llm
from .config import CLIP_MAX, CLIP_MIN, MAX_PER_DAY, MIN_VIRALITY, OUT_CLIPS, TZ

log = logging.getLogger("pipeline")


def day_start():
    return datetime.now(ZoneInfo(TZ)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _available_end(cand):
    rec = db.one("SELECT * FROM recordings WHERE id=?", cand["recording_id"])
    cs = db.q("SELECT start_ts,duration,status FROM chunks WHERE recording_id=? AND status!='deleted' "
              "ORDER BY start_ts", cand["recording_id"])
    covering = [c for c in cs if c["start_ts"] < cand["win_end"] and c["start_ts"] + c["duration"] > cand["win_start"]]
    if any(c["status"] in ("new", "analyzing") for c in covering):
        return None
    end = max((c["start_ts"] + c["duration"] for c in covering), default=0)
    waited = time.time() - cand["created"] > 1800
    if end >= cand["win_end"] or rec["status"] != "recording" or waited:
        return min(end, cand["win_end"] + 60)
    return None


def _transcript(words, t0):
    lines, cur, ct = [], [], None
    for w in words:
        if ct is None or w["s"] - ct > 4:
            if cur:
                lines.append(f"[{ct - t0:.0f}s] " + " ".join(cur))
            cur, ct = [], w["s"]
        cur.append(w["w"])
    if cur:
        lines.append(f"[{ct - t0:.0f}s] " + " ".join(cur))
    return "\n".join(lines)


def _is_dup(streamer, start, end, phash, text):
    if db.one("SELECT id FROM clips WHERE streamer=? AND start_ts<? AND end_ts>?", streamer, end, start):
        return True
    for c in db.q("SELECT phash, transcript FROM clips WHERE streamer=? AND created>?", streamer,
                  time.time() - 14 * 86400):
        if c["phash"] and editor.hamming(c["phash"], phash) < 20:
            return True
        if text and c["transcript"] and difflib.SequenceMatcher(None, c["transcript"], text).quick_ratio() > 0.85 \
                and difflib.SequenceMatcher(None, c["transcript"], text).ratio() > 0.8:
            return True
    return False


def process_candidate(cand, s):
    avail_end = _available_end(cand)
    if avail_end is None:
        return
    st = lambda x: db.ex("UPDATE candidates SET status=? WHERE id=?", x, cand["id"])
    made = db.one("SELECT COUNT(*) n FROM clips WHERE streamer=? AND created>?", s["name"], day_start())["n"]
    if made >= MAX_PER_DAY * 4:
        return st("capped")
    ws, we = cand["win_start"], min(cand["win_end"], avail_end)
    if db.one("SELECT id FROM clips WHERE streamer=? AND start_ts<? AND end_ts>?", s["name"], cand["peak_ts"] + 20,
              cand["peak_ts"] - 20):
        return st("dup")
    words = analyze.words_between(cand["recording_id"], ws, we)
    with tempfile.TemporaryDirectory() as td:
        src, base = editor.source(cand["recording_id"], ws, we, td)
        rel_peak = cand["peak_ts"] - ws
        offs = sorted({min(max(rel_peak + d, 0.5), we - ws - 0.5) for d in (-20, -6, -1, 2, 6, 15, 35, 80)})
        frames = [(o, editor.frame_at(src, ws - base + o, Path(td) / f"k{i}.jpg", 768)) for i, o in enumerate(offs)]
        res = llm.judge(s, frames, _transcript(words, ws), json.loads(cand["features"]), round(we - ws))
        peak_img = frames[min(range(len(frames)), key=lambda i: abs(frames[i][0] - rel_peak))][1]
        phash = editor.ahash(peak_img)
    if not res or res["virality"] < MIN_VIRALITY:
        log.info("Verworfen %s (%s): %s", s["name"], res and res["virality"], res and res["reason"][:120])
        return st("rejected")
    start = ws + max(0.0, res["start_offset"])
    end = min(ws + res["end_offset"], avail_end)
    if end - start < CLIP_MIN:
        end = min(start + CLIP_MIN, avail_end)
        start = max(ws, end - CLIP_MIN)
    end = min(end, start + CLIP_MAX)
    if end - start < CLIP_MIN - 5:
        return st("too_short")
    peak = ws + res["peak_offset"]
    clip_words = analyze.words_between(cand["recording_id"], start, end)
    text = " ".join(w["w"] for w in clip_words)
    if _is_dup(s["name"], start, end, phash, text):
        return st("dup")
    cap = llm.caption(s, {k: res[k] for k in ("what_happens", "game_or_topic", "situation", "people", "context")},
                      text)
    if not cap:
        return st("caption_failed")
    cid = db.ex("INSERT INTO clips(candidate_id,streamer,start_ts,end_ts,score,virality,features,analysis,caption,"
                "hook_style,hashtags,language,phash,transcript,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                cand["id"], s["name"], start, end, res["virality"] * 10 + cand["score"] * 10, res["virality"],
                cand["features"], json.dumps(res, ensure_ascii=False), cap["text"], cap["style"],
                json.dumps(cap["hashtags"]), s["language"], phash, text, "rendering", time.time())
    out = OUT_CLIPS / f"{s['name']}_{cid}.mp4"
    try:
        editor.render(cand["recording_id"], start, end, peak, clip_words, cap["on_screen"], s["credit"], out)
        db.ex("UPDATE clips SET path=?, status='queued' WHERE id=?", str(out), cid)
        st("done")
        log.info("Clip #%d bereit: %s v=%d '%s'", cid, s["name"], res["virality"], cap["hook"])
    except Exception as e:
        db.ex("UPDATE clips SET status='error' WHERE id=?", cid)
        st("error")
        raise e


def run_pending(streamer_map):
    for cand in db.q("SELECT * FROM candidates WHERE status='new' ORDER BY score DESC LIMIT 6"):
        s = streamer_map.get(cand["streamer"])
        if not s:
            db.ex("UPDATE candidates SET status='skipped' WHERE id=?", cand["id"])
            continue
        try:
            process_candidate(cand, s)
        except Exception as e:
            log.error("Kandidat %d: %s", cand["id"], e)
            db.ex("UPDATE candidates SET status='error' WHERE id=?", cand["id"])
    # Alte, nie abgedeckte Kandidaten verwerfen
    db.ex("UPDATE candidates SET status='expired' WHERE status='new' AND created<?", time.time() - 6 * 3600)
