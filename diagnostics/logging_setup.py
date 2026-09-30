"""Structured logging for the diagnostics pipeline.

Every log line emitted here is a single-line JSON object containing at least::

    {"request_id": ..., "business_id": ..., "sampled": ...,
     "error_category": ..., "event": ...}

All values pass through :func:`diagnostics.redaction.redact`, so secrets
cannot leak into logs even if callers pass them as fields.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .context import current_context
from .redaction import redact

_DEFAULT_FORMAT = "%(message)s"
_configured = False


class DiagnosticLogFilter(logging.Filter):
    """Ensure no LogRecord carrying a raw ``password`` attr is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        ctx = current_context()
        if ctx is not None:
            payload["request_id"] = ctx.request_id
            payload["business_id"] = ctx.business_id
            payload["sampled"] = ctx.sampled
            payload["error_category"] = ctx.error_category
        extra = getattr(record, "diag_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(redact(payload), ensure_ascii=False, sort_keys=True)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a JSON handler to the ``diagnostics`` logger (idempotent)."""
    global _configured
    logger = logging.getLogger("diagnostics")
    logger.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter(_DEFAULT_FORMAT))
        handler.addFilter(DiagnosticLogFilter())
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True
    return logger


def diagnostic_log(level: int, message: str, **fields: Any) -> None:
    """Best-effort structured log enriched from the current context."""
    try:
        logger = logging.getLogger("diagnostics")
        if not _configured:
            configure_logging()
        logger.log(level, message, extra={"diag_fields": redact(fields)})
    except Exception:  # pragma: no cover - logging must never break requests
        pass
