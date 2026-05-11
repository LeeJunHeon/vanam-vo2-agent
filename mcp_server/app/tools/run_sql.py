"""run_sql 도구 — 자유 SELECT (read-only).

안전 장치:
- SELECT 또는 WITH...SELECT만 (정규식 검증)
- INSERT/UPDATE/DELETE/DROP/ALTER 등 키워드 차단
- 세미콜론 multi-statement 차단
- vo2_reader 권한 (PostgreSQL 단에서 read-only 강제)
- SET LOCAL statement_timeout = 10s (오래 걸리면 자동 중단)
- LIMIT max_rows 자동 wrap (없으면 추가, 있으면 그대로)
- 배열/JSONB 큰 컬럼은 길이/요약만 표시 (raw는 get_timeseries로)
- AuditMiddleware로 SQL 본문 자동 기록 (vo2.mcp_audit_logs.arguments)
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from mcp_server.app.db import reader_session


# ─────────── 안전 검증 ───────────

_SELECT_START_RE = re.compile(r'^\s*(SELECT|WITH)\b', re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r'\b('
    r'INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|'
    r'COPY|CALL|DO|EXECUTE|LISTEN|NOTIFY|LOCK|VACUUM|ANALYZE|REINDEX|'
    r'CLUSTER|REFRESH|COMMENT|SECURITY|SET\s+ROLE|SET\s+SESSION'
    r')\b',
    re.IGNORECASE,
)
# 인용 안의 키워드는 false positive — string literal/identifier 안의 매칭은 허용해야 함
# 단 위험 키워드는 일반적으로 SQL 본문에 등장하지 않으므로 단순 정규식으로 충분

_DEFAULT_MAX_ROWS = 100
_HARD_MAX_ROWS = 1000
_STATEMENT_TIMEOUT_MS = 10_000  # 10초

# 배열/JSONB 컬럼 — 결과 크기 폭발 방지용으로 요약 처리
_LARGE_TYPES = {
    "ARRAY",
    "_float8",  # double precision[]
    "_float4",  # real[]
    "_int4",    # integer[]
    "_text",    # text[]
}
_JSONB_TYPES = {"jsonb", "json"}


def _validate_sql(sql: str) -> None:
    """SQL 안전 검증. 위반 시 ValueError."""
    if not sql or not sql.strip():
        raise ValueError("SQL이 비어있습니다")

    stripped = sql.strip()
    # 트레일링 세미콜론 한 개는 허용 (실용성)
    if stripped.endswith(";"):
        stripped = stripped.rstrip(";").rstrip()
    # 그 외 세미콜론 (multi-statement) 차단
    if ";" in stripped:
        raise ValueError("multi-statement 안 됨 — 세미콜론으로 분리된 여러 SQL 차단")

    if not _SELECT_START_RE.match(stripped):
        raise ValueError("SELECT 또는 WITH로 시작하는 query만 허용됩니다 (read-only)")

    if _FORBIDDEN_RE.search(stripped):
        raise ValueError(
            "INSERT/UPDATE/DELETE/DROP/ALTER/COPY/CALL/SET ROLE 등 "
            "데이터 변경/관리 명령은 허용되지 않습니다 (vo2_reader는 SELECT only)"
        )


def _wrap_with_limit(sql: str, max_rows: int) -> str:
    """LIMIT 자동 추가. 이미 LIMIT 있으면 그대로.

    안전 wrapper로 외부 LIMIT 강제: SELECT * FROM (사용자 SQL) AS _user_q LIMIT N
    근데 ORDER BY 등 외부 영향 있을 수 있으니 정규식으로 LIMIT만 체크하는 게 안전.
    """
    # 트레일링 세미콜론 정리
    stripped = sql.strip().rstrip(";").rstrip()
    # 이미 LIMIT 있는지 (대소문자/줄바꿈 무시)
    if re.search(r'\bLIMIT\s+\d+\b', stripped, re.IGNORECASE):
        # 사용자 LIMIT 존중, 단 max_rows 초과면 wrap으로 강제
        return f"SELECT * FROM ({stripped}) AS _user_q LIMIT {max_rows}"
    return f"{stripped} LIMIT {max_rows}"


# ─────────── 결과 직렬화 ───────────

def _summarize_value(v: Any, type_name: str, udt: str) -> Any:
    """결과 row의 한 셀 값을 JSON-safe로 + 큰 컬럼 요약."""
    if v is None:
        return None

    # 배열 컬럼 — 길이만 + 첫 3개 미리보기
    if udt in _LARGE_TYPES or type_name == "ARRAY":
        if isinstance(v, list):
            return {
                "__array__": True,
                "length": len(v),
                "preview_first3": [_simple_serialize(x) for x in v[:3]],
                "hint": "전체 배열은 get_timeseries(table=..., row_id=...) 호출",
            }

    # JSONB — 그대로 (Postgres가 dict/list 반환). 단 크기 큰 경우 요약
    if udt in _JSONB_TYPES:
        if isinstance(v, (dict, list)):
            # 길이 체크 - JSON 직렬화 시 큰 경우 요약
            import json
            try:
                s = json.dumps(v, default=str, ensure_ascii=False)
                if len(s) > 2000:
                    # 큰 JSON은 키만 + 미리보기
                    if isinstance(v, dict):
                        return {
                            "__jsonb_large__": True,
                            "keys": list(v.keys())[:20],
                            "size_chars": len(s),
                            "hint": "전체 JSONB는 SELECT raw_json::text 또는 raw_json->>'key' 형태로 부분 조회",
                        }
                    else:  # list
                        return {
                            "__jsonb_large__": True,
                            "length": len(v),
                            "size_chars": len(s),
                            "hint": "큰 JSON 배열 — 부분 조회 권장",
                        }
                return v  # 작은 JSON은 그대로
            except (TypeError, ValueError):
                return str(v)[:500]
        return v

    return _simple_serialize(v)


def _simple_serialize(v: Any) -> Any:
    """기본 타입 직렬화."""
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return f"<bytes[{len(v)}]>"
    return v


# ─────────── 메인 ───────────

def run(sql: str, max_rows: int = _DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """run_sql 도구 진입.

    Args:
        sql: SELECT 또는 WITH...SELECT.
        max_rows: 1~1000, 기본 100.

    Returns:
        {columns, rows, row_count, truncated, duration_ms, sql_executed,
         note?, error?}
    """
    # max_rows 클램프
    try:
        max_rows = int(max_rows)
    except (ValueError, TypeError):
        max_rows = _DEFAULT_MAX_ROWS
    if max_rows < 1:
        max_rows = 1
    if max_rows > _HARD_MAX_ROWS:
        max_rows = _HARD_MAX_ROWS

    # 안전 검증
    try:
        _validate_sql(sql)
    except ValueError as e:
        return {
            "error": f"SQL 검증 실패: {e}",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "duration_ms": 0,
            "sql_executed": None,
        }

    # LIMIT wrap
    wrapped_sql = _wrap_with_limit(sql, max_rows)

    # 실행
    start = time.time()
    try:
        with reader_session() as s:
            # 타임아웃 설정 (transaction-local)
            s.execute(text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))

            result = s.execute(text(wrapped_sql))
            column_names = list(result.keys())

            # 타입 정보 추출 (요약 처리용)
            # SQLAlchemy result에서 컬럼 타입 정보는 limited. udt까지 알려면 prepared inspection 필요.
            # 실용적 접근: 값 자체 타입으로 추론 (list = ARRAY, dict = JSONB)
            rows_raw = result.mappings().all()

        duration_ms = int((time.time() - start) * 1000)

        # 결과 직렬화
        rows = []
        for r in rows_raw:
            row_out = {}
            for col, val in r.items():
                # 타입 추론 (list/dict로)
                if isinstance(val, list):
                    row_out[col] = _summarize_value(val, "ARRAY", "_array")
                elif isinstance(val, dict):
                    row_out[col] = _summarize_value(val, "jsonb", "jsonb")
                else:
                    row_out[col] = _simple_serialize(val)
            rows.append(row_out)

        row_count = len(rows)
        truncated = row_count >= max_rows

        return {
            "columns": column_names,
            "rows": rows,
            "row_count": row_count,
            "truncated": truncated,
            "duration_ms": duration_ms,
            "sql_executed": wrapped_sql,
            "note": (
                f"결과 {row_count} row 반환 (max_rows={max_rows} 도달 — "
                "결과가 잘렸을 수 있음. max_rows 늘리거나 WHERE/LIMIT으로 좁혀보세요)"
                if truncated else None
            ),
        }

    except SQLAlchemyError as e:
        duration_ms = int((time.time() - start) * 1000)
        # 타임아웃 / SQL 에러 등
        err_str = str(e.orig if hasattr(e, 'orig') else e)
        return {
            "error": f"SQL 실행 실패: {type(e).__name__}: {err_str[:500]}",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "duration_ms": duration_ms,
            "sql_executed": wrapped_sql,
        }
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "error": f"예상치 못한 오류: {type(e).__name__}: {str(e)[:500]}",
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "duration_ms": duration_ms,
            "sql_executed": wrapped_sql,
        }