"""vo2-mcp-server 도구 입출력 스키마 (Pydantic v2).

Phase 4 Step 20 갱신:
- 옛 search_vo2_runs schemas 제거 (DROP된 sputter_runs 테이블 가정)
- describe_schema / run_sql / get_timeseries 3개 도구 추가
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ───────── describe_schema ─────────

class DescribeSchemaRequest(BaseModel):
    """schema 조회. table=None이면 전체 요약."""

    table: str | None = Field(
        default=None,
        description=(
            "조회할 테이블 이름. bare name이면 스키마 자동 추론 "
            "(예: 'measurements' → vo2.measurements, 'equipment_logs' → equipment.equipment_logs). "
            "'vo2.X' / 'equipment.X' 형태로 명시 가능. "
            "None이면 전체 18개 테이블 요약 (vo2 11 + equipment 7) + 도메인 지식 + 관계 + 자주 쓰는 쿼리 예시. "
            "테이블 이름 주면 그 테이블의 모든 컬럼/타입/예시 5 row."
        ),
    )


# Response는 dict[str, Any] — 도구 본문이 동적으로 구성 (FastAPI는 dict 반환 OK)


# ───────── run_sql ─────────

class RunSqlRequest(BaseModel):
    """자유 SELECT 실행."""

    sql: str = Field(
        description=(
            "SELECT 또는 WITH...SELECT query. "
            "vo2 schema의 read-only 권한. "
            "INSERT/UPDATE/DELETE/DROP/ALTER 등 차단. "
            "세미콜론 multi-statement 차단. "
            "배열/큰 JSONB는 요약만 반환 (raw는 get_timeseries 사용)."
        ),
    )
    max_rows: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="반환 row 수 상한 (자동 LIMIT wrap). 기본 100, 최대 1000.",
    )


# ───────── get_timeseries ─────────

class GetTimeseriesRequest(BaseModel):
    """measurements 또는 rga_runs의 시계열 배열 raw 반환."""

    table: Literal["measurements", "rga_runs"] = Field(
        description="조회할 테이블. measurements=R-T 측정, rga_runs=가스 spectrum.",
    )
    row_id: int = Field(
        ge=1,
        description="해당 테이블의 id 컬럼 값.",
    )