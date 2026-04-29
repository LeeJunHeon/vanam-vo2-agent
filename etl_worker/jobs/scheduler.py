"""APScheduler 5분 tick.

cron 패턴 매 5분 (*/5 * * * *), timezone Asia/Seoul.
첫 tick은 컨테이너 시작 직후 즉시 실행 (warm-up).
"""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from shared.config import get_settings
from etl_worker.jobs.sync_sputter import sync_all

log = logging.getLogger("etl.scheduler")


def start_scheduler() -> None:
    """5분 cron + 시작 직후 즉시 1회 실행."""
    settings = get_settings()
    sched = BlockingScheduler(timezone=settings.TZ)

    sched.add_job(
        sync_all,
        trigger=CronTrigger(minute="*/5", timezone=settings.TZ),
        id="sync_sputter",
        max_instances=1,        # 동시 실행 방지 (이전 tick 안 끝났으면 skip)
        coalesce=True,          # 밀린 tick은 합쳐서 1회만
        misfire_grace_time=120, # 2분 이내 늦으면 그래도 실행
    )

    log.info(f"scheduler started, tz={settings.TZ}, cron=*/5")
    log.info("running first tick immediately (warm-up)...")

    # warm-up: 첫 tick 즉시 실행
    try:
        sync_all()
    except Exception as e:
        log.error(f"first tick failed: {e}", exc_info=True)

    sched.start()  # blocking, 영구 루프 진입
