"""Request diagnostics & error attribution toolkit."""

from __future__ import annotations

from .context import (
    DiagnosticContext,
    ContextVarStorage,
    Span,
    bind_context,
    clear_context,
    current_context,
    require_context,
)
from .errors import (
    DependencyError,
    DiagnosticError,
    ErrorCategory,
    TimeoutError as DiagnosticTimeoutError,
    ValidationError,
    classify_exception,
    error_response,
    normalize,
)
from .redaction import redact, redact_text
from .settings import DiagnosticsSettings, get_settings
from .sampling import Sampler
from .storage import DiagnosticStore
from .auth import authorize, AuthDecision
from .middleware import DiagnosticsMiddleware
from .background import spawn_background
from .pipeline import build_record, safe_persist
from . import logging_setup

__all__ = [
    "DiagnosticContext",
    "ContextVarStorage",
    "Span",
    "bind_context",
    "clear_context",
    "current_context",
    "require_context",
    "DependencyError",
    "DiagnosticError",
    "ErrorCategory",
    "DiagnosticTimeoutError",
    "ValidationError",
    "classify_exception",
    "error_response",
    "normalize",
    "redact",
    "redact_text",
    "DiagnosticsSettings",
    "get_settings",
    "Sampler",
    "DiagnosticStore",
    "authorize",
    "AuthDecision",
    "DiagnosticsMiddleware",
    "spawn_background",
    "build_record",
    "safe_persist",
    "logging_setup",
]
