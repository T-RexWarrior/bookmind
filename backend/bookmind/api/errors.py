"""Unified error format — PRODUCTIZATION §8.3.

Every /api/* failure returns a structured error body, never a Python stack:

    {"error": {"code": "...", "message": "...", "can_retry": bool,
               "action": "...", "request_id": "..."}}

The frontend branches on ``code``; ``message`` is a safe fallback. Python
tracebacks are logged server-side and never serialized to the client.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from ..errors import AppError


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {
            "code": exc.code,
            "message": exc.message,
            "can_retry": exc.can_retry,
            "action": exc.action,
            "request_id": request.headers.get("x-request-id", str(uuid.uuid4())[:8]),
        }},
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize framework HTTP errors so the frontend never receives `detail`."""
    messages = {
        401: "登录状态已失效，正在重新建立会话。",
        403: "你无权访问这项内容。",
        404: "没有找到这项内容，它可能已经被删除。",
        409: "当前状态已发生变化，请刷新后重试。",
    }
    message = messages.get(exc.status_code, "请求没有成功，请稍后重试。")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {
            "code": f"HTTP_{exc.status_code}",
            "message": message,
            "can_retry": exc.status_code >= 500 or exc.status_code == 409,
            "action": "RETRY" if exc.status_code >= 500 or exc.status_code == 409 else "",
            "request_id": request.headers.get("x-request-id", str(uuid.uuid4())[:8]),
        }},
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {
            "code": "INVALID_INPUT",
            "message": "输入内容不完整或格式不正确，请检查后重试。",
            "can_retry": True,
            "action": "EDIT",
            "request_id": request.headers.get("x-request-id", str(uuid.uuid4())[:8]),
        }},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log the real traceback server-side; return a safe generic message.
    import logging
    logging.getLogger("bookmind").exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {
            "code": "INTERNAL_ERROR",
            "message": "服务内部错误，请稍后重试。",
            "can_retry": True,
            "action": "RETRY",
            "request_id": request.headers.get("x-request-id", str(uuid.uuid4())[:8]),
        }},
    )


async def scope_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A cross-scope access attempt (§11.2) returns 403, not a 500 stack."""
    return JSONResponse(
        status_code=403,
        content={"error": {
            "code": "FORBIDDEN",
            "message": "无权访问该资源。",
            "can_retry": False,
            "action": "",
            "request_id": request.headers.get("x-request-id", str(uuid.uuid4())[:8]),
        }},
    )
