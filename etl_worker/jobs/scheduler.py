"""APScheduler 매일 05:00 KST 1회 tick.

cron 패턴 매일 05:00 KST (0 5 * * *), timezone Asia/Seoul.
"""
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from shared.config import get_settings
from etl_worker.jobs.sync_sputter import sync_all

log = logging.getLogger("etl.scheduler")


def start_scheduler() -> None:
    """매일 05:00 KST 1회 cron 실행. warm-up 없음."""
    settings = get_settings()
    sched = BlockingScheduler(timezone=settings.TZ)

    sched.add_job(
        sync_all,
        trigger=CronTrigger(hour=5, minute=0, timezone=settings.TZ),
        id="sync_sputter",
        max_instances=1,         # 동시 실행 방지 (이전 tick 안 끝났으면 skip)
        coalesce=True,           # 밀린 tick은 합쳐서 1회만
        misfire_grace_time=3600, # 1시간 — 일일 cron이 컨테이너 재시작/장애로 밀려도 그날치 실행
    )

    log.info(f"scheduler started, tz={settings.TZ}, cron='0 5 * * *' (daily 05:00 KST)")

    sched.start()  # blocking, 영구 루프 진입
