"""Turn a finished :class:`DiagnosticContext` into a stored record."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .context import DiagnosticContext
from .errors import CATEGORY_MESSAGES
from .redaction import redact

if TYPE_CHECKING:
    from .storage import DiagnosticStore

log = logging.getLogger("diagnostics.pipeline")

#: Attr keys that may legitimately carry technical detail server-side but are
#: dropped from the stored (queryable) record.
_DROP_ATTR_KEYS = {"exception_message", "stack", "traceback"}


def _sanitized_spans(context: DiagnosticContext) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for span in context.spans:
        attrs = {k: v for k, v in span.attrs.items() if k not in _DROP_ATTR_KEYS}
        spans.append({
            "name": span.name,
            "status": span.status,
            "duration_ms": span.duration_ms,
            "attrs": redact(attrs),
        })
    return spans


def build_record(context: DiagnosticContext, *, method: str = "", path: str = "",
                 status_code: int = 0) -> dict[str, Any]:
    category = context.error_category
    record: dict[str, Any] = {
        "request_id": context.request_id,
        "business_id": context.business_id,
        "sampled": context.sampled,
        "method": method or context.method,
        "path": path or context.path,
        "status_code": status_code or context.status_code,
        "duration_ms": context.elapsed_ms(),
        "error_category": category,
        "error_message": CATEGORY_MESSAGES.get(category, "") if category else "",
        "spans": _sanitized_spans(context),
    }
    return record


def safe_persist(store: "DiagnosticStore", record: dict[str, Any]) -> bool:
    """Write a record, swallowing and logging store failures. Never raises."""
    try:
        store.put(record)
        return True
    except Exception:
        log.debug("persist failed", exc_info=True)
        return False
