"""vo2-mcp-server entry point.

Phase 4 Step 20: agentic SQL 3개 도구
- /tools/describe_schema: DB 구조 + 도메인 지식 + 예시 row
- /tools/run_sql: 자유 SELECT (read-only, 안전 장치)
- /tools/get_timeseries: measurements/rga_runs 시계열 배열

mcp_app lifespan 통합 (RuntimeError "Task group" 방지).
audit은 AuditMiddleware로 처리 (security.py).
/mcp/* 는 mount된 mcp_app — lifespan으로 task group 시작.

옛 search_vo2_runs는 제거됨 (DROP된 sputter_runs 테이블 가정이었음).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI

from mcp_server.app.main_mcp import mcp_app
from mcp_server.app.schemas import (
    DescribeSchemaRequest,
    GetTimeseriesRequest,
    RunSqlRequest,
)
from mcp_server.app.security import AuditMiddleware, require_token
from mcp_server.app.tools import describe_schema, get_timeseries, run_sql
from shared.config import get_settings
from shared.logging_config import setup_logging

log = setup_logging("mcp_server.main")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — mcp_app의 task group을 같이 시작/종료.

    mcp SDK 1.27의 streamable_http_app()은 내부에 session_manager의
    anyio task group을 사용한다. 이 lifespan_context를 우리 FastAPI
    lifespan으로 감싸지 않으면 첫 POST에서
    'Task group is not initialized' RuntimeError가 발생한다.
    """
    async with mcp_app.router.lifespan_context(_app):
        log.info("=" * 50)
        log.info("vo2-mcp-server starting up")
        log.info("phase=%s, log_level=%s", settings.PHASE, settings.LOG_LEVEL)
        log.info("tools: describe_schema, run_sql, get_timeseries")
        log.info("=" * 50)
        yield
        log.info("vo2-mcp-server shutting down")


app = FastAPI(
    title="vo2-mcp-server",
    version="0.2.0",
    description=(
        "VO2 공정 데이터 MCP server (Phase 4 Step 20). "
        "agentic SQL 3 tools: describe_schema, run_sql, get_timeseries."
    ),
    lifespan=lifespan,
)

# 모든 /tools/* 호출을 vo2.mcp_audit_logs에 기록
app.add_middleware(AuditMiddleware)

# Streamable HTTP MCP endpoint (ChatGPT custom connector용)
app.mount("/mcp", mcp_app)


@app.get("/health")
async def health() -> dict[str, Any]:
    """liveness probe — DB 연결은 검사하지 않음."""
    return {
        "status": "ok",
        "service": "vo2-mcp-server",
        "version": "0.2.0",
        "phase": settings.PHASE,
        "tools": ["describe_schema", "run_sql", "get_timeseries"],
    }


# ───────── REST tools (ChatGPT Custom GPT Action 등) ─────────

@app.post("/tools/describe_schema")
def _describe_schema(
    req: DescribeSchemaRequest,
    _token: str = Depends(require_token),
) -> dict[str, Any]:
    """vo2 + equipment schema 구조 + 도메인 지식 + 예시 row.

    table=None: 전체 18개 테이블 요약 (vo2 11 + equipment 7) + 관계 + 도메인 overview + 자주 쓰는 쿼리.
    table=<이름>: 특정 테이블 상세 (모든 컬럼 + 예시 5 row). bare name이면 스키마 자동 추론.

    분석 시작 시 처음 호출 권장.
    """
    return describe_schema.run(table=req.table)


@app.post("/tools/run_sql")
def _run_sql(
    req: RunSqlRequest,
    _token: str = Depends(require_token),
) -> dict[str, Any]:
    """자유 SELECT (read-only, vo2_reader 권한).

    대상 스키마: vo2.* (공정 데이터) + equipment.* (장비 유지보수). cross-schema JOIN 가능.

    안전 장치:
    - SELECT 또는 WITH...SELECT만
    - 데이터 변경 명령 차단
    - 세미콜론 multi-statement 차단
    - 자동 LIMIT (기본 100, 최대 1000)
    - 10초 타임아웃
    - 배열/큰 JSONB는 요약만
    - equipment.equipment_photos / equipment_entry_photos의 file_data(base64)는 DB 권한에서 차단

    배열 raw가 필요하면 get_timeseries 호출.
    """
    return run_sql.run(sql=req.sql, max_rows=req.max_rows)


@app.post("/tools/get_timeseries")
def _get_timeseries(
    req: GetTimeseriesRequest,
    _token: str = Depends(require_token),
) -> dict[str, Any]:
    """measurements/rga_runs의 시계열 배열 raw + 메타데이터 + 통계 요약.

    한 번에 한 row만 (토큰 폭발 방지).

    table='measurements': temperature_c + resistance_ohm (~500점)
    table='rga_runs': intensity (Mass 1~65)
    """
    return get_timeseries.run(table=req.table, row_id=req.row_id)