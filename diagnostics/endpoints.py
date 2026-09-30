"""Diagnostics query endpoints and exception-handler wiring.

The query API is doubly gated:

1. ``DIAG_ENABLED`` must be on, otherwise routes are not even registered
   (404 - surface does not exist);
2. every request must present the configured token
   (:func:`diagnostics.auth.authorize`, fail-closed).

Exception handlers give FastAPI a single place to turn
:class:`DiagnosticError` / validation errors into the stable public body while
stamping the active context with the error category.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .auth import AuthDecision, authorize
from .context import current_context
from .errors import (
    CATEGORY_MESSAGES,
    CATEGORY_STATUS,
    DiagnosticError,
    ErrorCategory,
    error_response,
    normalize,
)
from .settings import DiagnosticsSettings
from .storage import DiagnosticStore

_MAX_LIMIT = 200


def _client_details(exc: DiagnosticError) -> dict[str, Any] | None:
    validation = exc.details.get("validation")
    return {"validation": validation} if validation else None


def register_exception_handlers(app: Any) -> None:
    """Install handlers that render the stable error body for all errors."""

    def _render(exc_category: str, request: Any, request_id: str,
                details: dict[str, Any] | None = None):
        body = error_response(exc_category, CATEGORY_MESSAGES[exc_category],
                              request_id, details)
        return JSONResponse(status_code=CATEGORY_STATUS[exc_category], content=body)

    @app.exception_handler(DiagnosticError)
    async def _diagnostic_handler(request: Any, exc: DiagnosticError) -> Any:
        ctx = current_context()
        if ctx is not None:
            ctx.error_category = exc.category
        request_id = ctx.request_id if ctx is not None else ""
        return _render(exc.category, request, request_id, _client_details(exc))

    try:
        from fastapi.exceptions import RequestValidationError

        @app.exception_handler(RequestValidationError)
        async def _validation_handler(request: Any, exc: Any) -> Any:
            normalized = normalize(exc)
            ctx = current_context()
            if ctx is not None:
                ctx.error_category = ErrorCategory.VALIDATION
            request_id = ctx.request_id if ctx is not None else ""
            return _render(ErrorCategory.VALIDATION, request, request_id,
                           _client_details(normalized))
    except ImportError:  # pragma: no cover
        pass


def register(app: Any, *, settings: DiagnosticsSettings, store: DiagnosticStore) -> None:
    """Register ``/diagnostics`` query routes when diagnostics is enabled."""
    if not settings.enabled:
        return

    def _check(request: Request) -> AuthDecision:
        headers = {k: v for k, v in request.headers.items()}
        return authorize(headers, settings.query_token)

    def _denied() -> Any:
        return JSONResponse(
            status_code=403,
            content=error_response(ErrorCategory.PERMISSION,
                                   CATEGORY_MESSAGES[ErrorCategory.PERMISSION],
                                   ""),
        )

    @app.get("/diagnostics/requests", include_in_schema=False)
    async def list_requests(request: Request, limit: int = 50) -> Any:
        decision = _check(request)
        if not decision.allowed:
            return _denied()
        bounded = max(1, min(_MAX_LIMIT, limit))
        return {"records": store.list_recent(bounded), "count": len(store)}

    @app.get("/diagnostics/requests/{request_id}", include_in_schema=False)
    async def get_request(request_id: str, request: Request) -> Any:
        decision = _check(request)
        if not decision.allowed:
            return _denied()
        record = store.get(request_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content=error_response(ErrorCategory.NOT_FOUND,
                                       CATEGORY_MESSAGES[ErrorCategory.NOT_FOUND],
                                       request_id),
            )
        return record
