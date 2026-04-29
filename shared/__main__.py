"""shared 패키지 동작 검증 — python -m shared.

1. config 로드 + 항목 확인 (비밀번호/토큰/URL은 [HIDDEN])
2. reader engine으로 SELECT 실행해 DB 연결 확인
3. logger 포맷 (KST, 레벨) 확인
"""
from sqlalchemy import text

from shared.config import get_settings
from shared.db import session_scope_reader
from shared.logging_config import setup_logging


def _is_secret(field: str) -> bool:
    return any(kw in field for kw in ("PASSWORD", "TOKEN", "URL"))


def main() -> None:
    log = setup_logging("shared.verify")
    log.info("=== shared module verification start ===")

    settings = get_settings()
    log.info("Config loaded:")
    for field, value in settings.model_dump().items():
        if _is_secret(field):
            length = len(str(value)) if value is not None else 0
            log.info(f"  {field} = [HIDDEN, length={length}]")
        else:
            log.info(f"  {field} = {value}")

    log.info("Testing reader engine (SELECT)...")
    with session_scope_reader() as s:
        row = s.execute(text(
            "SELECT 'shared module OK' AS status, "
            "current_user, current_database(), current_schema()"
        )).fetchone()
        if row is None:
            log.error("SELECT returned no row — DB or permissions issue")
            return
        log.info(f"  status            = {row[0]}")
        log.info(f"  current_user      = {row[1]}")
        log.info(f"  current_database  = {row[2]}")
        log.info(f"  current_schema    = {row[3]}  (vo2 expected)")

    log.warning("This is a WARNING — for log format inspection.")
    log.info("=== verification done ===")


if __name__ == "__main__":
    main()
