"""Incremental ETL helper — 처음 본 row 만 적재.

Phase 4 Step 23: 멱등성 키 (source_file_id, row_number) 의 한계 보완.
새 sha 가 생기면 옛 source_file_id 의 row 들과 충돌 안 나서 통째 재INSERT 됨.
이를 막기 위해 파서 시작 시 watermark 조회 후 그 이하 row 는 skip.

정책: 한 번 적재된 row 는 영원히 그대로. 운영자가 xlsx/csv 의 옛 row 를
수정해도 무시. 새 row (watermark 초과) 만 INSERT.

두 가지 watermark:
- row_number_watermark(): xlsx 의 row_number 기반 (ald, sputter)
- measured_at_watermark(): rga 의 measured_at 기반 (csv append-only, 자연 시간)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from shared.db import session_scope_writer

log = logging.getLogger("etl.parsers._incremental")


def row_number_watermark(
    table_qualified: str,
    extra_where: str = "",
    params: Optional[dict] = None,
) -> int:
    """row_number 기반 watermark 조회.

    Args:
        table_qualified: 'vo2.ald_ncd_runs' 같은 schema-qualified 이름
        extra_where: 추가 WHERE 조건 (예: "AND chemistry = :chem")
                     맨 앞에 "AND" 가 필요. 빈 문자열이면 무시.
        params: extra_where 의 named parameter dict

    Returns:
        max row_number (테이블이 비었거나 모두 NULL 이면 0)
    """
    sql = f"""
        SELECT COALESCE(MAX(row_number), 0)
        FROM {table_qualified}
        WHERE row_number IS NOT NULL
        {extra_where}
    """
    with session_scope_writer() as s:
        wm = s.execute(text(sql), params or {}).scalar_one()
    log.info(
        f"watermark {table_qualified}"
        f"{f' [{params}]' if params else ''}: row_number={wm}"
    )
    return int(wm)


def measured_at_watermark(table_qualified: str) -> Optional[datetime]:
    """measured_at 기반 watermark (rga_runs 전용).

    Returns:
        max measured_at (없거나 모두 NULL 이면 None — 이 경우 모든 row 신규)
    """
    sql = f"""
        SELECT MAX(measured_at)
        FROM {table_qualified}
        WHERE measured_at IS NOT NULL
    """
    with session_scope_writer() as s:
        wm = s.execute(text(sql)).scalar_one_or_none()
    log.info(f"watermark {table_qualified}: measured_at={wm}")
    return wm
