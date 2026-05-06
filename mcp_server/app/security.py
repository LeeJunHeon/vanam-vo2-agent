"""vo2-mcp-server 인증 + audit log 데코레이터.

Phase 1b: INTERNAL_API_TOKEN 헤더 비교만.
Phase 3: + JWT, IP allow list, rate limit (별도 단계).

audit decorator는 모든 도구 호출을 vo2.mcp_audit_logs에 기록한다.
audit insert 실패는 사용자 응답을 막지 않는다 (logger.exception으로만 흔적 남김).
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from typing import Any, Callable

from fastapi import Header, HTTPException
from sqlalchemy import text

from mcp_server.app.db import audit_session
from shared.config import get_settings

log = logging.getLogger("mcp_server.security")
settings = get_settings()


def require_token(authorization: str | None = Header(default=None)) -> str:
    """Authorization: Bearer <INTERNAL_API_TOKEN> 검증. 통과 시 토큰 반환."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    token = authorization.removeprefix("Bearer ").strip()
    expected = settings.INTERNAL_API_TOKEN.get_secret_value()
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid token")
    return token


_AUDIT_SQL = text(
    """
    INSERT INTO vo2.mcp_audit_logs
        (caller_kind, caller_id, tool_name, arguments,
         success, error, duration_ms)
    VALUES
        (:caller_kind, :caller_id, :tool_name, CAST(:arguments AS JSONB),
         :success, :error, :duration_ms)
    """
)


def _record_audit(
    tool_name: str,
    arguments: dict[str, Any],
    success: bool,
    error: str | None,
    duration_ms: int,
    caller_kind: str = "portal",
    caller_id: str | None = None,
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
                },
            )
    except Exception:
        log.exception("audit insert failed (tool=%s)", tool_name)


def audit(tool_name: str) -> Callable:
    """도구 핸들러를 감싸 mcp_audit_logs에 호출 기록.

    FastAPI가 wrapper의 원본 시그니처를 읽을 수 있도록 wrapper.__signature__를
    명시적으로 설정 (functools.wraps만으로는 부족).

    주의: require_token이 Depends로 적용된 401/403은 핸들러 진입 전에 raise되어
    audit log에 기록되지 않음 (FastAPI 동작 특성). Phase 3 middleware 전환 시 해결.

    성공: success=true, error=NULL
    도구 내부 HTTPException: success=false, error=detail
    기타 예외: success=false, error=str(e)[:1000]
    """
    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        param_names = list(sig.parameters)
        first_param = param_names[0] if param_names else None

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if args:
                req = args[0]
            elif first_param and first_param in kwargs:
                req = kwargs[first_param]
            else:
                req = None
            start = time.time()
            success = True
            err: str | None = None
            try:
                return fn(*args, **kwargs)
            except HTTPException as e:
                success = False
                err = str(e.detail)
                raise
            except Exception as e:
                success = False
                err = str(e)
                raise
            finally:
                duration_ms = int((time.time() - start) * 1000)
                args_dict = (
                    req.model_dump()
                    if (req is not None and hasattr(req, "model_dump"))
                    else {}
                )
                _record_audit(
                    tool_name=tool_name,
                    arguments=args_dict,
                    success=success,
                    error=err,
                    duration_ms=duration_ms,
                )
        wrapper.__signature__ = sig  # type: ignore[attr-defined]
        return wrapper
    return deco
