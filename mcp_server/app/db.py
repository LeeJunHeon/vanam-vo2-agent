"""vo2-mcp-server DB 세션 헬퍼.

- reader_session: SELECT 전용 (shared.db의 read-only 컨텍스트 재export)
- audit_session: vo2.mcp_audit_logs INSERT 전용 (commit 필요)

둘 다 vo2_reader user를 사용. vo2_reader는 mcp_audit_logs에만 INSERT 권한
(다른 vo2.* 테이블은 SELECT only).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session, sessionmaker

# shared.db의 함수명에 맞춰 import 보정 (사전 작업 2에서 확인)
from shared.db import get_reader_engine, session_scope_reader

# SELECT 전용은 shared 컨텍스트 그대로 재export
reader_session = session_scope_reader

# audit insert 전용 (commit 필요) — 같은 reader engine 사용
_AuditSession = sessionmaker(
    bind=get_reader_engine(),
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


@contextmanager
def audit_session() -> Iterator[Session]:
    """vo2.mcp_audit_logs INSERT 전용 세션. 정상 종료 시 commit, 예외 시 rollback."""
    s = _AuditSession()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
