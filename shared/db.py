"""DB 연결 — vo2 schema 전용.

writer / reader / admin 3개 engine + 각각 session_scope context manager.
ETL=writer, MCP=reader, Alembic migration=admin.

모든 connection은 search_path를 vo2,public 으로 설정 — vo2.* 명시 안 해도
vo2 schema 테이블이 우선 lookup. 단 SQL 작성 시 명시적 vo2.* 사용을 권장.
"""
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from shared.config import get_settings

_SEARCH_PATH = "vo2, public"


def _set_search_path(dbapi_connection, connection_record):
    """모든 새 connection에 vo2 search_path 설정."""
    cur = dbapi_connection.cursor()
    cur.execute(f"SET search_path TO {_SEARCH_PATH}")
    cur.close()


def _create_engine(url: str) -> Engine:
    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=10,
        future=True,
    )
    event.listen(eng, "connect", _set_search_path)
    return eng


@lru_cache(maxsize=1)
def get_writer_engine() -> Engine:
    return _create_engine(get_settings().DATABASE_URL_WRITER.get_secret_value())


@lru_cache(maxsize=1)
def get_reader_engine() -> Engine:
    return _create_engine(get_settings().DATABASE_URL_READER.get_secret_value())


@lru_cache(maxsize=1)
def get_admin_engine() -> Engine:
    return _create_engine(get_settings().DATABASE_URL_ADMIN.get_secret_value())


_writer_factory = sessionmaker(expire_on_commit=False, future=True)
_reader_factory = sessionmaker(expire_on_commit=False, future=True)
_admin_factory = sessionmaker(expire_on_commit=False, future=True)


@contextmanager
def session_scope_writer() -> Iterator[Session]:
    """ETL/analysis worker용 — with 종료 시 자동 commit/rollback."""
    session = _writer_factory(bind=get_writer_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope_reader() -> Iterator[Session]:
    """MCP server용 — read-only, commit 안 함."""
    session = _reader_factory(bind=get_reader_engine())
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope_admin() -> Iterator[Session]:
    """Alembic migration 또는 schema 변경 작업용."""
    session = _admin_factory(bind=get_admin_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
