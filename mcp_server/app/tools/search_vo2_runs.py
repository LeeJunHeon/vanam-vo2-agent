"""search_vo2_runs — sputter_runs 검색 도구.

chamber/recipe/시간 범위/sample_id 필터, LIMIT 적용.
Phase 1b: sputter_runs 단일 테이블만 (samples LEFT JOIN은 Phase 2).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from mcp_server.app.db import reader_session
from mcp_server.app.schemas import (
    SearchVO2RunsRequest,
    SearchVO2RunsResponse,
    SputterRunSummary,
)


_SEARCH_SQL = text(
    """
    SELECT
        sr.id              AS sputter_run_id,
        sr.chamber         AS chamber,
        sr.sputter_run_id  AS run_label,
        sr.recipe_name     AS recipe_name,
        sr.start_time      AS start_time,
        sr.o2_ratio        AS o2_ratio,
        sr.avg_power_w     AS avg_power_w,
        sr.process_time_min AS process_time_min,
        sr.thickness_nm    AS thickness_nm
    FROM vo2.sputter_runs sr
    WHERE 1=1
        AND (CAST(:chamber AS TEXT)             IS NULL OR sr.chamber         = CAST(:chamber AS TEXT))
        AND (CAST(:recipe_name AS TEXT)         IS NULL OR sr.recipe_name     = CAST(:recipe_name AS TEXT))
        AND (CAST(:start_after AS TIMESTAMPTZ)  IS NULL OR sr.start_time     >= CAST(:start_after AS TIMESTAMPTZ))
        AND (CAST(:start_before AS TIMESTAMPTZ) IS NULL OR sr.start_time     <= CAST(:start_before AS TIMESTAMPTZ))
        AND (CAST(:sample_id AS TEXT)           IS NULL OR sr.sputter_run_id = CAST(:sample_id AS TEXT))
    ORDER BY sr.start_time DESC NULLS LAST
    LIMIT :limit
    """
)


def run(req: SearchVO2RunsRequest) -> SearchVO2RunsResponse:
    """sputter_runs 검색. 항상 LIMIT 적용, read-only."""
    with reader_session() as s:
        rows = s.execute(
            _SEARCH_SQL,
            {
                "chamber":      req.chamber,
                "recipe_name":  req.recipe_name,
                "start_after":  req.start_after,
                "start_before": req.start_before,
                "sample_id":    req.sample_id,
                "limit":        req.limit,
            },
        ).mappings().all()

    runs_out = [
        SputterRunSummary(
            sputter_run_id=r["sputter_run_id"],
            chamber=r["chamber"],
            sample_id=r["run_label"],
            recipe_name=r["recipe_name"],
            start_time=r["start_time"],
            o2_ratio=_to_float(r["o2_ratio"]),
            avg_power_w=_to_float(r["avg_power_w"]),
            process_time_min=_to_float(r["process_time_min"]),
            thickness_nm=_to_float(r["thickness_nm"]),
        )
        for r in rows
    ]

    return SearchVO2RunsResponse(
        runs=runs_out,
        count=len(runs_out),
        provenance={
            "queried_at":     datetime.now(timezone.utc).isoformat(),
            "limit_applied":  req.limit,
            "source_table":   "vo2.sputter_runs",
        },
    )


def _to_float(v) -> float | None:
    """NUMERIC/Decimal/None 안전 변환."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
