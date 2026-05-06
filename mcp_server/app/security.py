"""vo2-mcp-server 인증 + audit log middleware.

Phase 1b: INTERNAL_API_TOKEN 헤더 비교 + AuditMiddleware로 모든 /tools/* 호출 기록.
Phase 3: + JWT, IP allow list, rate limit (별도 단계).

audit insert 실패는 사용자 응답을 막지 않는다 (logger.exception으로만 흔적 남김).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import Header, HTTPException
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from mcp_server.app.db import audit_session
from shared.config import get_settings

log = logging.getLogger("mcp_server.security")
settings = get_settings()


# ───────── 인증 ─────────

def require_token(authorization: str | None = Header(default=None)) -> str:
    """Authorization: Bearer <INTERNAL_API_TOKEN> 검증. 통과 시 토큰 반환."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.removeprefix("Bearer ").strip()
    expected = settings.INTERNAL_API_TOKEN.get_secret_value()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return token


# ───────── audit insert ─────────

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
    caller_kind: str = "portal",
    caller_id: str | None = "portal-nextjs",
    ip_address: str | None = None,
) -> None:
    """vo2.mcp_audit_logs 한 row INSERT. 실패해도 raise 안 함."""
    try:
        with audit_session() as s:
            s.execute(
                _AUDIT_SQL,
                {
                    "caller_kind": caller_kind,
                    "caller_id": caller_id,
                    "tool_name": tool_name,
                    "arguments": json.dumps(arguments, default=str, ensure_ascii=False),
                    "success": success,
                    "error": (error or "")[:1000] if error else None,
                    "duration_ms": duration_ms,
                    "ip_address": ip_address,
                },
            )
    except Exception:
        log.exception("audit insert failed (tool=%s)", tool_name)


# ───────── audit middleware ─────────

class AuditMiddleware(BaseHTTPMiddleware):
    """모든 /tools/* 호출을 vo2.mcp_audit_logs에 기록.

    - /health 등 비-도구 경로는 즉시 통과 (audit 안 함)
    - request body는 best-effort로 캡처 (JSON parse 실패 시 빈 dict)
    - 401/403 등 도구 진입 전 거부도 status_code로 감지해서 success=false 기록
      → require_token Depends에서 raise되는 401도 정확히 audit됨
    - 데코레이터가 아니라 ASGI 레벨에서 동작하므로 FastAPI 라우트 시그니처 추론
      (자동 body 파싱, Depends DI)에 0% 간섭
    """

    AUDITED_PREFIX = "/tools/"

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        # /tools/* 외에는 통과
        if not request.url.path.startswith(self.AUDITED_PREFIX):
            return await call_next(request)

        # body 캡처 — _receive를 wrapper로 교체해서 라우트 핸들러도 body 정상 읽기 가능
        arguments: dict[str, Any] = {}
        try:
            body_bytes = await request.body()

            async def _receive():
                return {"type": "http.request", "body": body_bytes, "more_body": False}

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

        tool_name = request.url.path.removeprefix(self.AUDITED_PREFIX)
        client_ip = request.client.host if request.client else None

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
                ip_address=client_ip,
            )
