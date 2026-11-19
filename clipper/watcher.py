import json, logging, shutil, subprocess, threading, time
from pathlib import Path
from . import db, twitch_api
from .chat import ChatLogger
from .config import REC, MAX_REC, CHUNK_S, VOD_MAX_S, streamers, stream_url, vods_url
from .media import duration

log = logging.getLogger("watcher")
_active = {}  # name -> dict(proc, ffmpeg, chat, rec_id, dir)
_vod_lock = threading.Lock()


def is_live(s):
    try:
        r = subprocess.run(["streamlink", "--json", stream_url(s)], capture_output=True, text=True, timeout=60)
        return bool(json.loads(r.stdout or "{}").get("streams"))
    except Exception as e:
        log.warning("live-check %s: %s", s["name"], e)
        return False


def _start(s):
    d = REC / f"{s['name']}_{int(time.time())}"
    d.mkdir(parents=True)
    sl = subprocess.Popen(["streamlink", "--stdout", "--twitch-disable-ads", "--retry-streams", "10",
                           "--retry-max", "3", stream_url(s), "1080p60,1080p,936p60,720p60,720p,best"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0",
                           "-c", "copy", "-f", "segment", "-segment_time", str(CHUNK_S), "-reset_timestamps", "1",
                           "-strftime", "1", str(d / "%s.ts")], stdin=sl.stdout, stderr=open(d / "ffmpeg.log", "w"))
    sl.stdout.close()
    rid = db.ex("INSERT INTO recordings(streamer,kind,source,dir,started) VALUES(?,?,?,?,?)",
                s["name"], "live", stream_url(s), str(d), time.time())
    _active[s["name"]] = dict(sl=sl, ff=ff, chat=ChatLogger(s, d / "chat.jsonl").start(), rec=rid, dir=d, s=s)
    log.info("Aufnahme gestartet: %s", s["name"])


def _stop(name):
    a = _active.pop(name)
    for p in (a["sl"], a["ff"]):
        if p.poll() is None:
            p.terminate()
    try:
        a["ff"].wait(30)
    except Exception:
        a["ff"].kill()
    a["chat"].stop.set()
    db.ex("UPDATE recordings SET ended=?, status='ended' WHERE id=?", time.time(), a["rec"])
    _register_chunks(a["rec"], a["dir"], final=True)
    log.info("Aufnahme beendet: %s", name)


def _register_chunks(rid, d, final=False, base_ts=None):
    files = sorted(Path(d).glob("*.ts"), key=lambda p: p.name)
    if not final:
        files = files[:-1]  # letzte Datei wird noch geschrieben
    offset = 0.0
    for f in files:
        dur = duration(f)
        start = (base_ts + offset) if base_ts is not None else float(f.stem)
        offset += dur
        if dur < 5:
            continue
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO chunks(recording_id,path,start_ts,duration,created) VALUES(?,?,?,?,?)",
                      (rid, str(f), start, dur, time.time()))


def _vod_scan(cands):
    if not _vod_lock.acquire(blocking=False):
        return
    try:
        for s in cands:
            if _vod_job(s):
                return
    finally:
        _vod_lock.release()


def _vod_job(s):
    try:
        info = json.loads(subprocess.run(["yt-dlp", "-J", "--flat-playlist", "--playlist-end", "1", vods_url(s)],
                                         capture_output=True, text=True, timeout=120).stdout or "{}")
        entries = info.get("entries") or []
        if not entries:
            return False
        url = entries[0].get("url") or entries[0].get("webpage_url")
        if db.one("SELECT id FROM recordings WHERE source=?", url):
            return False
        meta = json.loads(subprocess.run(["yt-dlp", "-J", url], capture_output=True, text=True, timeout=120).stdout)
        if meta.get("is_live"):
            return False
        ts = meta.get("timestamp") or meta.get("release_timestamp") or time.time()
        d = REC / f"{s['name']}_vod_{meta.get('id', int(time.time()))}"
        d.mkdir(parents=True, exist_ok=True)
        rid = db.ex("INSERT INTO recordings(streamer,kind,source,dir,started,status) VALUES(?,?,?,?,?,?)",
                    s["name"], "vod", url, str(d), ts, "downloading")
        log.info("VOD-Download: %s %s", s["name"], url)
        subprocess.run(["yt-dlp", "-q", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
                        "--download-sections", f"*0-{VOD_MAX_S}", "-o", str(d / "vod.%(ext)s"), url],
                       check=True, timeout=VOD_MAX_S * 2)
        src = next(d.glob("vod.*"))
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                        "-f", "segment", "-segment_time", str(CHUNK_S), "-reset_timestamps", "1",
                        str(d / "%05d.ts")], check=True)
        src.unlink()
        _register_chunks(rid, d, final=True, base_ts=ts)
        db.ex("UPDATE recordings SET ended=?, status='ended' WHERE id=?", time.time(), rid)
        return True
    except Exception as e:
        log.error("VOD %s: %s", s["name"], e)
        return True


def tick():
    all_s = streamers()
    by_name = {s["name"]: s for s in all_s}
    for name in list(_active):
        a = _active[name]
        if a["ff"].poll() is not None or a["sl"].poll() is not None or name not in by_name:
            _stop(name)
        else:
            _register_chunks(a["rec"], a["dir"])
    live = [s for s in all_s if s["name"] in _active or is_live(s)]
    want = [s["name"] for s in live[:MAX_REC]]
    for name in list(_active):
        if name not in want:
            _stop(name)  # von höher priorisiertem Stream verdrängt
    for name in want:
        if name not in _active:
            try:
                _start(by_name[name])
            except Exception as e:
                log.error("Start %s: %s", name, e)
    backlog = db.one("SELECT COUNT(*) n FROM chunks WHERE status='new'")["n"]
    if len(_active) < MAX_REC and backlog < 12:
        idle = [s for s in all_s if s["name"] not in _active]
        threading.Thread(target=_vod_scan, args=(idle,), daemon=True).start()
    for s in all_s:
        if s["platform"] == "twitch":
            twitch_api.top_clip_moments(s["channel"])


def shutdown():
    for name in list(_active):
        _stop(name)


def cleanup(hours):
    cutoff = time.time() - hours * 3600
    for c in db.q("SELECT * FROM chunks WHERE created<? AND status!='deleted'", cutoff):
        for p in (Path(c["path"]), Path(c["path"] + ".words.json")):
            p.unlink(missing_ok=True)
        db.ex("UPDATE chunks SET status='deleted' WHERE id=?", c["id"])
    for r in db.q("SELECT * FROM recordings WHERE status IN ('ended','done') AND COALESCE(ended,started)<?", cutoff):
        shutil.rmtree(r["dir"], ignore_errors=True)
        db.ex("UPDATE recordings SET status='deleted' WHERE id=?", r["id"])
