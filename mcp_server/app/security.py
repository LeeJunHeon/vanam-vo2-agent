"""vo2-mcp-server 인증 + audit log middleware + rate limit.

Phase 3 Step 7:
- 두 token 분리 (INTERNAL_API_TOKEN=portal, CHATGPT_API_TOKEN=chatgpt_connector)
- caller_kind 자동 분기 (어느 token이 매칭됐는지에 따라)
- rate limit 분당 30회 / token별 (in-memory deque)
- AuditMiddleware가 /tools/* 와 /mcp/* 둘 다 잡음
- /mcp/* 는 mount된 ASGI app이라 Depends 적용 불가 → middleware가 직접 token 검증
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Header, HTTPException, Request
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from mcp_server.app.db import audit_session
from shared.config import get_settings

log = logging.getLogger("mcp_server.security")
settings = get_settings()


# ───────── token 매칭 ─────────

_PORTAL_TOKEN = settings.INTERNAL_API_TOKEN.get_secret_value()
_CHATGPT_TOKEN = (
    settings.CHATGPT_API_TOKEN.get_secret_value()
    if settings.CHATGPT_API_TOKEN is not None
    else None
)


def _match_token(token: str) -> str | None:
    """token을 매칭해서 caller_kind 반환. 매칭 실패 시 None.

    portal과 chatgpt_connector가 서로 다른 token이면 caller_kind도 분기됨.
    """
    if token == _PORTAL_TOKEN:
        return "portal"
    if _CHATGPT_TOKEN is not None and token == _CHATGPT_TOKEN:
        return "chatgpt_connector"
    return None


def require_token(authorization: str | None = Header(default=None)) -> str:
    """FastAPI Depends용. /tools/* 라우트에서 사용.

    Authorization: Bearer <token> 헤더 검증. 통과 시 token 자체 반환.
    호출 측은 _match_token으로 caller_kind 결정 가능.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.removeprefix("Bearer ").strip()
    if _match_token(token) is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return token


# ───────── rate limit (token별 in-memory sliding window) ─────────

_RATE_LIMIT_PER_MIN = 30
_rate_window: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(token: str) -> bool:
    """token별 분당 호출 수 체크. True면 통과, False면 한도 초과.

    sliding window 60초 기준. 컨테이너 재기동 시 리셋 (in-memory).
    """
    now = time.time()
    window = _rate_window[token]
    # 60초 이전 호출 기록 정리
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= _RATE_LIMIT_PER_MIN:
        return False
    window.append(now)
    return True


# ───────── audit insert (기존 그대로 + 약간 보강) ─────────

_AUDIT_SQL = text(
    """
    INSERT INTO vo2.mcp_audit_logs
        (caller_kind, caller_id, tool_name, arguments,
         success, error, duration_ms, ip_address)
    VALUES
        (:caller_kind, :caller_id, :tool_name, CAST(:arguments AS JSONB),
         :success, :error, :duration_ms, CAST(:ip_address AS INET))
    """
)


def _record_audit(
    tool_name: str,
    arguments: dict[str, Any],
    success: bool,
    error: str | None,
    duration_ms: int,
    caller_kind: str,
    caller_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """vo2.mcp_audit_logs INSERT. 실패해도 raise 안 함."""
    try:
        with audit_session() as s:
            s.execute(
                _AUDIT_SQL,
                {
                    "caller_kind": caller_kind,
                    "caller_id": caller_id,
                    "tool_name": tool_name,
                    "arguments": json.dumps(
                        arguments, default=str, ensure_ascii=False
                    ),
                    "success": success,
                    "error": (error or "")[:1000] if error else None,
                    "duration_ms": duration_ms,
                    "ip_address": ip_address,
                },
            )
    except Exception:
        log.exception("audit insert failed (tool=%s)", tool_name)


# ───────── audit + auth middleware ─────────

class AuditMiddleware(BaseHTTPMiddleware):
    """모든 /tools/* 와 /mcp/* 호출을 vo2.mcp_audit_logs에 기록.

    - /tools/* 는 라우트 Depends(require_token)이 인증을 처리. middleware는 audit만.
    - /mcp/* 는 mount된 ASGI app이라 Depends 적용 불가. middleware가 직접 token + rate limit 검증.
    - 401/403 등 도구 진입 전 거부도 기록 (token 누출 탐지에 핵심).
    """

    TOOLS_PREFIX = "/tools/"
    MCP_PREFIX = "/mcp/"

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        is_tools = path.startswith(self.TOOLS_PREFIX)
        is_mcp = path.startswith(self.MCP_PREFIX) or path == "/mcp"

        # 비도구 경로 (예: /health) 즉시 통과
        if not (is_tools or is_mcp):
            return await call_next(request)

        # body 캡처 (best-effort)
        arguments: dict[str, Any] = {}
        try:
            body_bytes = await request.body()

            async def _receive():
                return {
                    "type": "http.request",
                    "body": body_bytes,
                    "more_body": False,
                }

            request._receive = _receive  # type: ignore[attr-defined]

            if body_bytes:
                try:
                    parsed = json.loads(body_bytes.decode("utf-8"))
                    if isinstance(parsed, dict):
                        arguments = parsed
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        except Exception:
            log.exception("audit body capture failed")

        client_ip = request.client.host if request.client else None

        # /mcp/* 는 middleware가 직접 token + rate limit 검증
        caller_kind: str | None = None
        if is_mcp:
            authz = request.headers.get("authorization") or ""
            if not authz.startswith("Bearer "):
                _record_audit(
                    tool_name="_mcp_auth",
                    arguments=arguments,
                    success=False,
                    error="http_401_missing_token",
                    duration_ms=0,
                    caller_kind="unknown",
                    ip_address=client_ip,
                )
                return JSONResponse(
                    {"detail": "missing token"}, status_code=401
                )
            token = authz.removeprefix("Bearer ").strip()
            caller_kind = _match_token(token)
            if caller_kind is None:
                _record_audit(
                    tool_name="_mcp_auth",
                    arguments=arguments,
                    success=False,
                    error="http_401_invalid_token",
                    duration_ms=0,
                    caller_kind="unknown",
                    ip_address=client_ip,
                )
                return JSONResponse(
                    {"detail": "invalid token"}, status_code=401
                )
            if not _check_rate_limit(token):
                _record_audit(
                    tool_name="_mcp_rate_limit",
                    arguments=arguments,
                    success=False,
                    error="http_429_rate_limit",
                    duration_ms=0,
                    caller_kind=caller_kind,
                    ip_address=client_ip,
                )
                return JSONResponse(
                    {"detail": "rate limit exceeded (30/min)"},
                    status_code=429,
                )

        # /tools/* 는 라우트 Depends가 검증. caller_kind는 audit 시점에서 token 헤더로 재추출.
        elif is_tools:
            authz = request.headers.get("authorization") or ""
            if authz.startswith("Bearer "):
                token = authz.removeprefix("Bearer ").strip()
                caller_kind = _match_token(token)
                # rate limit도 적용
                if caller_kind is not None and not _check_rate_limit(token):
                    _record_audit(
                        tool_name=path.removeprefix(self.TOOLS_PREFIX),
                        arguments=arguments,
                        success=False,
                        error="http_429_rate_limit",
                        duration_ms=0,
                        caller_kind=caller_kind,
                        ip_address=client_ip,
                    )
                    return JSONResponse(
                        {"detail": "rate limit exceeded (30/min)"},
                        status_code=429,
                    )

        # tool_name 결정
        if is_tools:
            tool_name = path.removeprefix(self.TOOLS_PREFIX)
        else:  # is_mcp
            # /mcp/* 는 JSON-RPC라 path만으로 도구명 알 수 없음.
            # arguments에 JSON-RPC method가 들어있으면 활용.
            tool_name = arguments.get("method") or "_mcp_call"
            # tools/call 인 경우 실제 도구명 추출
            if tool_name == "tools/call":
                inner = arguments.get("params", {}).get("name")
                if inner:
                    tool_name = inner

        start = time.time()
        success = True
        err: str | None = None
        try:
            response = await call_next(request)
            if response.status_code >= 400:
                success = False
                err = f"http_{response.status_code}"
            return response
        except Exception as e:
            success = False
            err = str(e)
            raise
        finally:
            duration_ms = int((time.time() - start) * 1000)
            _record_audit(
                tool_name=tool_name,
                arguments=arguments,
                success=success,
                error=err,
                duration_ms=duration_ms,
                caller_kind=caller_kind or "unknown",
                ip_address=client_ip,
            )
