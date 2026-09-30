"""Pure-ASGI diagnostics middleware.

Responsibilities, all best-effort and isolated from business logic:

1. resolve (or generate) ``request_id`` / ``business_id`` and validate inbound
   ids to prevent header/log injection;
2. make a deterministic sampling decision and bind a request-scoped
   :class:`DiagnosticContext` (contextvars => concurrent requests are
   isolated, including inside background tasks);
3. capture method/path/status/timing and any error category;
4. render a stable, redacted error body for uncaught exceptions without
   duplicating a response the app already started;
5. log one structured completion line and, when sampled, persist a record;
6. on client disconnect (``CancelledError``) finalize cleanly and re-raise so
   the ASGI server stays correct.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from .context import DiagnosticContext, bind_context, clear_context
from .errors import (
    CATEGORY_MESSAGES,
    CATEGORY_STATUS,
    ErrorCategory,
    category_for_status as _category_for_status,
    classify_exception,
    error_response,
)
from .logging_setup import diagnostic_log
from .pipeline import build_record, safe_persist
from .sampling import Sampler
from .settings import DiagnosticsSettings, get_settings

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_BUSINESS_ID_LEN = 200


def _valid_identifier(value: str) -> bool:
    return bool(_REQUEST_ID_RE.fullmatch(value))


def _resolve_ids(headers: dict[str, str], query: dict[str, str]) -> tuple[str, str]:
    request_id = headers.get("x-request-id", "").strip()
    if not request_id or not _valid_identifier(request_id):
        request_id = uuid.uuid4().hex
    business_id = headers.get("x-business-id", "").strip() or query.get("business_id", "")
    business_id = business_id[:_MAX_BUSINESS_ID_LEN]
    # Keep business ids free of control chars / newlines (log injection).
    business_id = re.sub(r"[\r\n\t]", "", business_id)
    return request_id, business_id


def _parse_query(query_string: bytes) -> dict[str, str]:
    from urllib.parse import parse_qsl
    return dict(parse_qsl(query_string.decode("latin-1"), keep_blank_values=True))


def _header_list(scope: dict[str, Any]) -> dict[str, str]:
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}


class DiagnosticsMiddleware:
    def __init__(self, app: Any, settings: DiagnosticsSettings | None = None,
                 store: DiagnosticStore | None = None,
                 sampler: Sampler | None = None) -> None:
        self.app = app
        self.settings = settings or get_settings()
        self.store = store
        rate = self.settings.sample_rate if self.settings.enabled else 0.0
        self.sampler = sampler or Sampler(rate)

    async def _send_error(self, send: Any, category: str, request_id: str,
                          details: dict[str, Any] | None = None) -> None:
        status = CATEGORY_STATUS.get(category, 500)
        body = error_response(category, CATEGORY_MESSAGES[category], request_id, details)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-request-id", request_id.encode("latin-1")),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = _header_list(scope)
        query = _parse_query(scope.get("query_string", b""))
        request_id, business_id = _resolve_ids(headers, query)
        sampled = self.settings.enabled and self.sampler.sample(request_id)

        context = DiagnosticContext(
            request_id=request_id,
            business_id=business_id,
            sampled=sampled,
            method=scope.get("method", ""),
            path=scope.get("path", ""),
        )
        bind_context(context)
        response_started = False

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                status = int(message.get("status", 0))
                context.status_code = status
                # FastAPI exception handlers convert the exception into a
                # normal response, so it never propagates here. Infer the
                # category from the status when the handler did not tag the
                # context explicitly.
                if status >= 400 and context.error_category is None:
                    context.error_category = _category_for_status(status)
                response_started = True
                msg_headers = message.setdefault("headers", [])
                if not any(k.lower() == b"x-request-id" for k, _ in msg_headers):
                    msg_headers.append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            category = classify_exception(exc)
            context.error_category = category
            if category == ErrorCategory.CANCELLED:
                self._finalize(context)
                raise
            if not response_started:
                details = None
                if hasattr(exc, "details") and isinstance(exc.details, dict):
                    validation = exc.details.get("validation")
                    if validation:
                        details = {"validation": validation}
                try:
                    await self._send_error(send, category, request_id, details)
                    context.status_code = CATEGORY_STATUS.get(category, 500)
                except Exception:  # pragma: no cover - client may be gone
                    pass
            self._finalize(context)
            # A response has been produced (or already started): do not let the
            # business exception propagate as an opaque 500 to the server log.
            return
        else:
            self._finalize(context)
        finally:
            clear_context()

    def _finalize(self, context: DiagnosticContext) -> None:
        # The query surface must not record itself (it would otherwise pollute
        # the very list it returns); it still gets the x-request-id header.
        is_surface = context.path.startswith("/diagnostics")
        try:
            level = logging.INFO
            if context.error_category and context.error_category != ErrorCategory.CANCELLED:
                level = logging.WARNING
            if not is_surface:
                diagnostic_log(
                    level,
                    "request.finished",
                    phase="request",
                    status_code=context.status_code,
                    error_category=context.error_category,
                    sampled=context.sampled,
                    duration_ms=context.elapsed_ms(),
                )
            if (self.settings.enabled and context.sampled and self.store is not None
                    and not is_surface):
                record = build_record(context)
                record["phase"] = "request"
                safe_persist(self.store, record)
        except Exception:  # pragma: no cover - diagnostics never breaks request
            logging.getLogger("diagnostics.middleware").debug(
                "finalize failed", exc_info=True)
