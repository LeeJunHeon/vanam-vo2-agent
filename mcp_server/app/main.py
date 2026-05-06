"""vo2-mcp-server entry point.

Phase 1b Step 5-4: /health + /tools/search_vo2_runs.
audit은 AuditMiddleware로 처리 (security.py).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI

from mcp_server.app.main_mcp import mcp_app
from mcp_server.app.schemas import SearchVO2RunsRequest, SearchVO2RunsResponse
from mcp_server.app.security import AuditMiddleware, require_token
from mcp_server.app.tools import search_vo2_runs
from shared.config import get_settings
from shared.logging_config import setup_logging

log = setup_logging("mcp_server.main")
settings = get_settings()

app = FastAPI(
    title="vo2-mcp-server",
    version="0.1.0",
    description="VO2 Sputter MCP server (Phase 1b)",
)

# 모든 /tools/* 호출을 vo2.mcp_audit_logs에 기록
app.add_middleware(AuditMiddleware)

# Streamable HTTP MCP endpoint (ChatGPT custom connector용)
# AuditMiddleware는 현재 /tools/만 잡음 — Step 7에서 /mcp도 audit 잡도록 확장 예정
app.mount("/mcp", mcp_app)


@app.on_event("startup")
async def on_startup() -> None:
    log.info("=" * 50)
    log.info("vo2-mcp-server starting up")
    log.info("phase=%s, log_level=%s", settings.PHASE, settings.LOG_LEVEL)
    log.info("=" * 50)


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
