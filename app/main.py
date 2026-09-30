"""应用组装入口。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import get_config
from .context import current_request_id
from .errors import ErrorCategory, safe_error_body
from .logging_setup import setup_logging
from .middleware import CATEGORY_HEADER
from .middleware import DiagnosticsMiddleware
from .routes_demo import router as demo_router
from .routes_diagnostics import router as diagnostics_router

CATEGORY_HDR = CATEGORY_HEADER.decode()


def _error_response(category: ErrorCategory, status_code: int, extra: dict | None = None):
    body = safe_error_body(category, current_request_id())
    if extra:
        body["error"].update(extra)
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={CATEGORY_HDR: category.value},
    )


def create_app() -> FastAPI:
    setup_logging(logging.INFO)
    app = FastAPI(title="request-diagnostics-pipeline", version="0.1.0")

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        # 只返回字段位置与规则，不回显用户输入，避免敏感值出现在响应里
        fields = [
            {"loc": list(err.get("loc", [])), "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return _error_response(
            ErrorCategory.VALIDATION, 422, extra={"fields": fields}
        )

    @app.exception_handler(HTTPException)
    async def _on_http_error(request: Request, exc: HTTPException):
        from .errors import _STATUS_CATEGORY  # 复用同一份映射

        category = _STATUS_CATEGORY.get(exc.status_code)
        if category is None:
            category = (
                ErrorCategory.VALIDATION
                if 400 <= exc.status_code < 500
                else ErrorCategory.INTERNAL
            )
        # 诊断接口自身的 403/404 detail 属于控制面信息，保留无妨；
        # 业务侧统一错误体不带原始 detail。
        return _error_response(category, exc.status_code)

    app.add_middleware(DiagnosticsMiddleware)
    app.include_router(demo_router)
    app.include_router(diagnostics_router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "diagnostics_enabled": get_config().enabled}

    return app


app = create_app()
