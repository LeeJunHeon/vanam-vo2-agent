"""vo2-mcp-server entry point.

Phase 1b Step 5-4: /health + /tools/search_vo2_runs.
Phase 3 Step 6-1-fix2: mcp_app lifespan 통합 (RuntimeError "Task group" 해결).

audit은 AuditMiddleware로 처리 (security.py).
/mcp/* 는 mount된 mcp_app — lifespan으로 task group 시작.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI

from mcp_server.app.main_mcp import mcp_app
from mcp_server.app.schemas import SearchVO2RunsRequest, SearchVO2RunsResponse
from mcp_server.app.security import AuditMiddleware, require_token
from mcp_server.app.tools import search_vo2_runs
from shared.config import get_settings
from shared.logging_config import setup_logging

log = setup_logging("mcp_server.main")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — mcp_app의 task group을 같이 시작/종료.

    mcp SDK 1.27의 streamable_http_app()은 내부에 session_manager의 anyio task group을
    사용한다. 이 lifespan_context를 우리 FastAPI lifespan으로 감싸지 않으면 첫 POST에서
    'Task group is not initialized' RuntimeError가 발생한다.
    """
    async with mcp_app.router.lifespan_context(_app):
        log.info("=" * 50)
        log.info("vo2-mcp-server starting up")
        log.info("phase=%s, log_level=%s", settings.PHASE, settings.LOG_LEVEL)
        log.info("=" * 50)
        yield
        log.info("vo2-mcp-server shutting down")


app = FastAPI(
    title="vo2-mcp-server",
    version="0.1.0",
    description="VO2 Sputter MCP server (Phase 1b)",
    lifespan=lifespan,
)

# 모든 /tools/* 호출을 vo2.mcp_audit_logs에 기록
app.add_middleware(AuditMiddleware)

# Streamable HTTP MCP endpoint (ChatGPT custom connector용)
# AuditMiddleware는 현재 /tools/만 잡음 — Step 7에서 /mcp도 audit 잡도록 확장 예정
app.mount("/mcp", mcp_app)


@app.get("/health")
async def health() -> dict:
    """liveness probe — DB 연결은 검사하지 않음."""
    return {
        "status": "ok",
        "service": "vo2-mcp-server",
        "version": "0.1.0",
        "phase": settings.PHASE,
    }


@app.post("/tools/search_vo2_runs", response_model=SearchVO2RunsResponse)
def _search_vo2_runs(
    req: SearchVO2RunsRequest,
    _token: str = Depends(require_token),
) -> SearchVO2RunsResponse:
    """sputter_runs 검색 (Phase 1b 첫 도구). audit은 AuditMiddleware가 처리."""
    return search_vo2_runs.run(req)
