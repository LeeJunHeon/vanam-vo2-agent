"""ETL worker 진입점 — python -m etl_worker.main.

Dockerfile의 CMD가 호출하는 모듈. shared logger 초기화 후 scheduler 시작.
"""
import logging

from shared.logging_config import setup_logging
from etl_worker.jobs.scheduler import start_scheduler


def main() -> None:
    log = setup_logging("etl_worker.main")
    log.info("=========================================")
    log.info("vo2-etl-worker starting up")
    log.info("=========================================")

    try:
        start_scheduler()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received, shutting down")
    except Exception as e:
        log.error(f"fatal error in scheduler: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
