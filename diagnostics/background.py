"""Background task support that propagates the diagnostic context.

``spawn_background`` schedules a coroutine as an ``asyncio`` task:

* it snapshots the current request id / business id / sampling decision at
  scheduling time and binds a *child* context inside the task, so the work
  keeps its identity even after the request context is reset, and two
  concurrent requests can never cross-talk;
* cancellation, timeout and exceptions are caught, categorized, logged with
  the propagated ids and (best-effort) persisted as a ``phase=background``
  record;
* ``CancelledError`` is re-raised after cleanup so asyncio accounting stays
  correct; nothing the task does can escape into the request path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from .context import DiagnosticContext, bind_context, current_context
from .errors import CATEGORY_STATUS, ErrorCategory, classify_exception
from .pipeline import build_record, safe_persist

if TYPE_CHECKING:
    from .storage import DiagnosticStore

log = logging.getLogger("diagnostics.background")


def _child_context(parent: DiagnosticContext | None, name: str) -> DiagnosticContext:
    if parent is None:
        return DiagnosticContext(request_id="", business_id="", sampled=False)
    child = DiagnosticContext(
        request_id=parent.request_id,
        business_id=parent.business_id,
        sampled=parent.sampled,
        method=parent.method,
        path=parent.path,
    )
    return child


async def _run(coro: Coroutine[Any, Any, Any], name: str,
               store: "DiagnosticStore | None") -> Any:
    parent = current_context()
    child = _child_context(parent, name)
    # Binding only mutates this task's copied contextvars context; the
    # parent request (and sibling tasks) are unaffected.
    bind_context(child)
    result: Any = None
    outcome = "success"
    try:
        with child.span(f"background:{name}"):
            result = await coro
    except asyncio.CancelledError:
        outcome = "cancelled"
        child.error_category = ErrorCategory.CANCELLED
        child.status_code = CATEGORY_STATUS[ErrorCategory.CANCELLED]
        _finalize(child, name, store, cancelled=True)
        raise
    except BaseException as exc:
        outcome = "error"
        # Process-exit signals must propagate untouched.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        category = classify_exception(exc)
        child.error_category = category
        child.status_code = CATEGORY_STATUS.get(category, 500)
        with child.span("background.error") as span:
            span.finish(status="error", exception_type=type(exc).__name__)
        _finalize(child, name, store, cancelled=False)
        # Fire-and-forget semantics: the failure is categorized, logged and
        # (when sampled) persisted, but not re-raised - the originator has
        # typically finished already and nobody is awaiting this task.
        return None

    if outcome == "success":
        child.status_code = 200
        _finalize(child, name, store, cancelled=False, success=True)
    return result


def _finalize(ctx: DiagnosticContext, name: str,
              store: "DiagnosticStore | None", *, cancelled: bool,
              success: bool = False) -> None:
    try:
        from .logging_setup import diagnostic_log
        diagnostic_log(
            logging.INFO if success else logging.WARNING,
            "background.finished",
            phase="background",
            task=name,
            cancelled=cancelled,
            success=success,
            error_category=ctx.error_category,
            sampled=ctx.sampled,
        )
        if store is not None and ctx.sampled:
            record = build_record(ctx)
            record["phase"] = "background"
            record["task"] = name
            safe_persist(store, record)
    except Exception:  # pragma: no cover - finalization must never raise
        log.debug("background finalization failed", exc_info=True)


def spawn_background(coro: Coroutine[Any, Any, Any], *, name: str = "job",
                     store: "DiagnosticStore | None" = None) -> asyncio.Task[Any]:
    """Schedule *coro* carrying a copy of the current diagnostic identity."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError("spawn_background requires a running event loop") from exc
    # create_task copies the current contextvars context at creation time,
    # which is exactly the identity propagation boundary we want.
    wrapped = _run(coro, name, store)
    return loop.create_task(wrapped, name=f"diag-bg:{name}")
