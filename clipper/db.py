import sqlite3, threading, time
from contextlib import contextmanager
from .config import DATA

_lock = threading.RLock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings(
  id INTEGER PRIMARY KEY, streamer TEXT, kind TEXT, source TEXT, dir TEXT,
  started REAL, ended REAL, status TEXT DEFAULT 'recording', meta TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, recording_id INTEGER, path TEXT UNIQUE, start_ts REAL, duration REAL,
  status TEXT DEFAULT 'new', created REAL);
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY, streamer TEXT, recording_id INTEGER, peak_ts REAL, win_start REAL, win_end REAL,
  score REAL, features TEXT, status TEXT DEFAULT 'new', created REAL);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY, candidate_id INTEGER, streamer TEXT, start_ts REAL, end_ts REAL,
  path TEXT, score REAL, virality INTEGER, features TEXT, analysis TEXT, caption TEXT, hook_style TEXT,
  hashtags TEXT, language TEXT, phash TEXT, transcript TEXT,
  status TEXT DEFAULT 'queued', created REAL, posted REAL, post_slot TEXT, post_id TEXT, tiktok_id TEXT);
CREATE TABLE IF NOT EXISTS stats(
  clip_id INTEGER, fetched REAL, views INTEGER, likes INTEGER, shares INTEGER, comments INTEGER);
CREATE INDEX IF NOT EXISTS ix_clips_status ON clips(status, score);
"""


def _conn():
    c = sqlite3.connect(DATA / "clipper.db", timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


_c = _conn()
_c.executescript(SCHEMA)


@contextmanager
def tx():
    with _lock:
        try:
            yield _c
            _c.commit()
        except Exception:
            _c.rollback()
            raise


def q(sql, *a):
    with _lock:
        return [dict(r) for r in _c.execute(sql, a).fetchall()]


def one(sql, *a):
    r = q(sql, *a)
    return r[0] if r else None


def ex(sql, *a):
    with tx() as c:
        return c.execute(sql, a).lastrowid


now = time.time
