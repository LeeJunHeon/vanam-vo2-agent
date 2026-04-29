"""로그 — 한국 시간대(KST), 표준 포맷.

포맷 예: [2026-04-29 17:30:45] [INFO] [etl.parsers.sputter_xlsx] xlsx 58 rows parsed

여러 모듈이 setup_logging을 여러 번 호출해도 핸들러 중복되지 않게 처리.
"""
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from shared.config import get_settings

_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_KST = ZoneInfo("Asia/Seoul")


def _kst_converter(timestamp):
    """time.struct_time을 Asia/Seoul 시간으로 변환."""
    dt = datetime.fromtimestamp(timestamp, tz=_KST)
    return dt.timetuple()


def setup_logging(name: str = "vo2-agent") -> logging.Logger:
    """루트 로거 한 번 설정 + named logger 반환.

    Idempotent — 여러 번 호출해도 핸들러 중복 안 됨.
    """
    settings = get_settings()
    root = logging.getLogger()

    if not root.handlers:
        level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        root.setLevel(level)

        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
        formatter.converter = _kst_converter  # type: ignore[assignment]
        handler.setFormatter(formatter)
        root.addHandler(handler)

    return logging.getLogger(name)
