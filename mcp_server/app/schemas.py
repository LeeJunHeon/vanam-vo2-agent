"""vo2-mcp-server 도구 입출력 스키마 (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ───────── search_vo2_runs ─────────

class SearchVO2RunsRequest(BaseModel):
    """sputter_runs 검색 필터. 모든 필드 optional, 비우면 최근 N건."""

    chamber: Literal["CH1", "CH2"] | None = None
    recipe_name: str | None = None
    start_after: datetime | None = None
    start_before: datetime | None = None
    sample_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class SputterRunSummary(BaseModel):
    """단일 sputter run 요약. 정확한 컬럼은 5-4-B에서 SQL과 매핑."""

    sputter_run_id: int
    chamber: str | None
    sample_id: str | None
    recipe_name: str | None
    start_time: datetime | None
    o2_ratio: float | None
    avg_power_w: float | None
    process_time_min: float | None
    thickness_nm: float | None


class SearchVO2RunsResponse(BaseModel):
    runs: list[SputterRunSummary]
    count: int
    provenance: dict
