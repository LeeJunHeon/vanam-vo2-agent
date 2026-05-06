"""vo2-mcp-server entry point.

Phase 1b Step 5-1: /health 엔드포인트만. 도구는 Step 5-4에서 추가.
"""
from __future__ import annotations

from fastapi import FastAPI

from shared.config import get_settings
from shared.logging_config import setup_logging

log = setup_logging("mcp_server.main")
settings = get_settings()

app = FastAPI(
    title="vo2-mcp-server",
    version="0.1.0",
    description="VO2 Sputter MCP server (Phase 1b)",
)


@app.on_event("startup")
async def on_startup() -> None:
    log.info("=" * 50)
    log.info("vo2-mcp-server starting up")
    log.info("phase=%s, log_level=%s", settings.PHASE, settings.LOG_LEVEL)
    log.info("=" * 50)


@app.get("/health")
async def health() -> dict:
    """liveness probe — DB 연결은 검사하지 않음 (Step 5-4 이후 readiness 별도)."""
    return {
        "status": "ok",
        "service": "vo2-mcp-server",
        "version": "0.1.0",
        "phase": settings.PHASE,
    }
