"""vo2-mcp-server MCP 프로토콜 어댑터.

기존 FastAPI REST(/health, /tools/*)와 별개로 ChatGPT custom connector 등
외부 MCP 클라이언트가 사용할 Streamable HTTP MCP 엔드포인트를 노출한다.
같은 tools/search_vo2_runs.run()을 재사용한다.

stateless_http=True: Mcp-Session-Id 추적 없음. curl 검증 단순화 + ChatGPT 호환.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.streamable_http import TransportSecuritySettings

from mcp_server.app.schemas import SearchVO2RunsRequest
from mcp_server.app.tools import search_vo2_runs as _search_tool
from shared.logging_config import setup_logging

log = setup_logging("mcp_server.main_mcp")

mcp = FastMCP(
    "vo2-mcp-server",
    stateless_http=True,
    streamable_http_path="/",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            "vo2-mcp.vanam.synology.me",
            "vo2-mcp.vanam.synology.me:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
            "https://vo2-mcp.vanam.synology.me",
            "https://vo2-mcp.vanam.synology.me:*",
        ],
    ),
)


@mcp.tool()
def search_vo2_runs(
    chamber: str | None = None,
    recipe_name: str | None = None,
    start_after: datetime | None = None,
    start_before: datetime | None = None,
    sample_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """VO2 sputter_runs를 chamber/recipe/시간 범위/sample_id로 검색.

    파라미터:
      chamber: 'CH1' 또는 'CH2' (현재 운영 데이터는 CH1만)
      recipe_name: sputter 레시피 이름 (정확 매칭)
      start_after: ISO8601 (예: '2026-05-01T00:00:00Z') 이후 run만
      start_before: ISO8601 이전 run만
      sample_id: sputter run 식별자 (현재는 run label과 매칭됨)
      limit: 1~200, 기본 50

    응답: {runs: [{sputter_run_id, chamber, ...}], count, provenance: {...}}
    """
    req = SearchVO2RunsRequest(
        chamber=chamber,
        recipe_name=recipe_name,
        start_after=start_after,
        start_before=start_before,
        sample_id=sample_id,
        limit=limit,
    )
    resp = _search_tool.run(req)
    return resp.model_dump(mode="json")


# Streamable HTTP transport ASGI app — main.py에서 mount.
# 사전 작업 3에서 확인한 메서드명이 다르면 아래를 보정.
mcp_app = mcp.streamable_http_app()
