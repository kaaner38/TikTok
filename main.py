import logging, signal, sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from clipper import analyze, log as _log, pipeline, poster, telegram, watcher
from clipper.config import RETENTION_H, TZ, learned, streamers
import stats

_log.setup()
log = logging.getLogger("main")
sched = BlockingScheduler(timezone=TZ, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600})


def safe(fn):
    def w(*a):
        try:
            fn(*a)
        except Exception:
            log.exception("Job %s", fn.__name__)
    w.__name__ = fn.__name__
    return w


def process():
    smap = {s["name"]: s for s in streamers()}
    analyze.run_pending(smap)
    pipeline.run_pending(smap)


def schedule_posts():
    for j in sched.get_jobs():
        if j.id.startswith("post_"):
            j.remove()
    for t in learned()["post_times"]:
        h, m = map(int, t.split(":"))
        sched.add_job(safe(poster.post_slot), CronTrigger(hour=h, minute=m, timezone=TZ), args=[t], id=f"post_{t}")
    log.info("Posting-Slots: %s", learned()["post_times"])


def weekly():
    stats.optimize()
    schedule_posts()


def shutdown(*_):
    log.info("Beende...")
    watcher.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    telegram.start_polling()
    sched.add_job(safe(watcher.tick), "interval", minutes=3, id="watch", next_run_time=__import__("datetime").datetime.now())
    sched.add_job(safe(process), "interval", minutes=2, id="process")
    sched.add_job(safe(watcher.cleanup), "interval", hours=1, args=[RETENTION_H], id="cleanup")
    sched.add_job(safe(stats.fetch), CronTrigger(hour="3,15", minute=7, timezone=TZ), id="stats")
    sched.add_job(safe(weekly), CronTrigger(day_of_week="mon", hour=4, minute=17, timezone=TZ), id="optimize")
    schedule_posts()
    telegram.send("🚀 tiktok-clipper gestartet")
    sched.start()
