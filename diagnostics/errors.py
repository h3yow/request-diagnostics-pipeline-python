"""Stable error categories, normalized exception hierarchy and classification.

The public surface is intentionally small and stable so clients can branch on
it:

* ``validation_error``  - bad client input (400/422)
* ``permission_denied`` - missing/insufficient credentials (401/403)
* ``not_found``         - referenced resource does not exist (404)
* ``timeout_error``     - a dependency or the request itself timed out (504)
* ``cancelled``         - the client went away / the task was cancelled (499)
* ``dependency_error``  - a downstream dependency failed (502)
* ``internal_error``    - anything else (500)
"""

from __future__ import annotations

import asyncio
from typing import Any

try:  # anyio is always present with FastAPI, but keep the import optional
    import anyio
except ImportError:  # pragma: no cover
    anyio = None  # type: ignore[assignment]


class ErrorCategory:
    VALIDATION = "validation_error"
    DEPENDENCY = "dependency_error"
    TIMEOUT = "timeout_error"
    CANCELLED = "cancelled"
    INTERNAL = "internal_error"
    PERMISSION = "permission_denied"
    NOT_FOUND = "not_found"


ALL_CATEGORIES: frozenset[str] = frozenset({
    ErrorCategory.VALIDATION,
    ErrorCategory.DEPENDENCY,
    ErrorCategory.TIMEOUT,
    ErrorCategory.CANCELLED,
    ErrorCategory.INTERNAL,
    ErrorCategory.PERMISSION,
    ErrorCategory.NOT_FOUND,
})

#: Stable HTTP status for each category.
CATEGORY_STATUS: dict[str, int] = {
    ErrorCategory.VALIDATION: 422,
    ErrorCategory.PERMISSION: 403,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.CANCELLED: 499,
    ErrorCategory.DEPENDENCY: 502,
    ErrorCategory.INTERNAL: 500,
}

#: Generic, non-sensitive wording per category.
CATEGORY_MESSAGES: dict[str, str] = {
    ErrorCategory.VALIDATION: "request validation failed",
    ErrorCategory.PERMISSION: "permission denied",
    ErrorCategory.NOT_FOUND: "resource not found",
    ErrorCategory.TIMEOUT: "request timed out",
    ErrorCategory.CANCELLED: "request cancelled",
    ErrorCategory.DEPENDENCY: "downstream dependency error",
    ErrorCategory.INTERNAL: "internal server error",
}

#: Reverse map for responses produced inside the framework (exception
#: handlers) where the original exception does not reach the middleware.
STATUS_CATEGORY: dict[int, str] = {
    400: ErrorCategory.VALIDATION,
    422: ErrorCategory.VALIDATION,
    401: ErrorCategory.PERMISSION,
    403: ErrorCategory.PERMISSION,
    404: ErrorCategory.NOT_FOUND,
    499: ErrorCategory.CANCELLED,
    504: ErrorCategory.TIMEOUT,
}


def category_for_status(status_code: int) -> str:
    """Best-effort category for an outbound HTTP status code."""
    if status_code in STATUS_CATEGORY:
        return STATUS_CATEGORY[status_code]
    if status_code == 500:
        return ErrorCategory.INTERNAL
    if 502 <= status_code < 600:
        return ErrorCategory.DEPENDENCY
    return ErrorCategory.INTERNAL


_ANYIO_CANCELLED: type[BaseException] | None = None


def _cancelled_classes() -> tuple[type[BaseException], ...]:
    global _ANYIO_CANCELLED
    classes: list[type[BaseException]] = [asyncio.CancelledError]
    # Newer anyio versions require a running loop to resolve the backend's
    # cancellation class; resolve lazily and cache it for later (sync) calls.
    if anyio is not None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            try:
                _ANYIO_CANCELLED = anyio.get_cancelled_exc_class()
            except Exception:  # pragma: no cover - defensive
                _ANYIO_CANCELLED = None
        if _ANYIO_CANCELLED is not None and _ANYIO_CANCELLED not in classes:
            classes.append(_ANYIO_CANCELLED)
    return tuple(classes)


class DiagnosticError(Exception):
    """Base class for all normalized, attributable errors."""

    category: str = ErrorCategory.INTERNAL
    public_message: str = CATEGORY_MESSAGES[ErrorCategory.INTERNAL]

    def __init__(self, message: str = "", *, category: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        cat = category or type(self).category
        if cat not in ALL_CATEGORIES:
            raise ValueError(f"unknown error category: {cat!r}")
        self.category = cat
        # Internal detail may carry technical info; never serialized to clients
        # verbatim unless it is whitelisted/sanitized by the caller.
        self.details: dict[str, Any] = dict(details or {})
        public = message or type(self).public_message or CATEGORY_MESSAGES[cat]
        super().__init__(public)

    @property
    def status_code(self) -> int:
        return CATEGORY_STATUS[self.category]


class ValidationError(DiagnosticError):
    category = ErrorCategory.VALIDATION
    public_message = CATEGORY_MESSAGES[ErrorCategory.VALIDATION]


class DependencyError(DiagnosticError):
    category = ErrorCategory.DEPENDENCY
    public_message = CATEGORY_MESSAGES[ErrorCategory.DEPENDENCY]


class TimeoutError(DiagnosticError):  # noqa: A001 - mirrors builtin concept
    category = ErrorCategory.TIMEOUT
    public_message = CATEGORY_MESSAGES[ErrorCategory.TIMEOUT]


class PermissionDeniedError(DiagnosticError):
    category = ErrorCategory.PERMISSION
    public_message = CATEGORY_MESSAGES[ErrorCategory.PERMISSION]


class NotFoundError(DiagnosticError):
    category = ErrorCategory.NOT_FOUND
    public_message = CATEGORY_MESSAGES[ErrorCategory.NOT_FOUND]


def classify_exception(exc: BaseException) -> str:
    """Map an arbitrary exception to one of the stable category strings."""
    if isinstance(exc, DiagnosticError):
        return exc.category

    cancelled = _cancelled_classes()
    if isinstance(exc, cancelled):
        return ErrorCategory.CANCELLED

    # Builtin timeout (asyncio.wait_for, futures).
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return ErrorCategory.TIMEOUT
    timeout_exc = getattr(__import__("builtins"), "TimeoutError")
    if isinstance(exc, timeout_exc):  # pragma: no cover - belt & braces
        return ErrorCategory.TIMEOUT

    # Framework-aware classification (imports kept local/optional).
    try:
        import fastapi
        from fastapi.exceptions import RequestValidationError
        if isinstance(exc, RequestValidationError):
            return ErrorCategory.VALIDATION
    except ImportError:  # pragma: no cover
        fastapi = None  # type: ignore[assignment]

    try:
        from starlette.exceptions import HTTPException as StarletteHTTP
        if isinstance(exc, StarletteHTTP):
            return _classify_http_status(exc.status_code)
    except ImportError:  # pragma: no cover
        pass

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ErrorCategory.VALIDATION

    # OSError / ConnectionError usually mean a downstream call failed.
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorCategory.DEPENDENCY

    return ErrorCategory.INTERNAL


def _classify_http_status(status_code: int) -> str:
    if status_code in (400, 422):
        return ErrorCategory.VALIDATION
    if status_code in (401, 403):
        return ErrorCategory.PERMISSION
    if status_code == 404:
        return ErrorCategory.NOT_FOUND
    if status_code == 504:
        return ErrorCategory.TIMEOUT
    if status_code >= 500:
        return ErrorCategory.DEPENDENCY
    return ErrorCategory.INTERNAL


def normalize(exc: BaseException) -> DiagnosticError:
    """Wrap an arbitrary exception into a :class:`DiagnosticError`."""
    if isinstance(exc, DiagnosticError):
        return exc
    category = classify_exception(exc)

    safe_fields: dict[str, Any] | None = None
    try:
        from fastapi.exceptions import RequestValidationError
        if isinstance(exc, RequestValidationError):
            safe_fields = {"validation": _safe_validation(exc)}
    except ImportError:  # pragma: no cover
        pass

    # The original message stays in internal ``details`` (server-side only);
    # the public message is the generic category wording.
    return DiagnosticError(
        category=category,
        details={"exception_type": type(exc).__name__, "exception_message": str(exc)[:500],
                 **(safe_fields or {})},
    )


def _safe_validation(exc: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for err in getattr(exc, "errors", lambda: [])() or []:
        items.append({
            "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
            "type": err.get("type", ""),
            "message": str(err.get("msg", ""))[:200],
        })
    return items[:20]


def error_response(category: str, message: str, request_id: str,
                   details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the stable, public error body. No sensitive data included."""
    if category not in ALL_CATEGORIES:
        category = ErrorCategory.INTERNAL
    body: dict[str, Any] = {
        "error": {
            "category": category,
            "message": message or CATEGORY_MESSAGES[category],
            "request_id": request_id,
        }
    }
    # Validation field errors are client-safe; callers pass them explicitly.
    if details:
        body["error"]["details"] = details
    return body
