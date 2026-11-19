import logging, sys
from logging.handlers import RotatingFileHandler
from .config import LOGS

_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


class _TelegramHandler(logging.Handler):
    def emit(self, record):
        try:
            from . import telegram
            telegram.send(f"⚠️ {record.name}: {record.getMessage()[:3500]}")
        except Exception:
            pass


def setup():
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOGS / "clipper.log", maxBytes=10_000_000, backupCount=5)
    sh = logging.StreamHandler(sys.stdout)
    th = _TelegramHandler(level=logging.ERROR)
    for h in (fh, sh, th):
        h.setFormatter(_fmt)
        root.addHandler(h)
    for noisy in ("httpx", "apscheduler", "urllib3", "websockets", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
