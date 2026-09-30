"""Sensitive-data redaction so secrets never reach logs or public responses.

Rules:
* dict keys matching a sensitive name (case-insensitive substring match on a
  curated list) have their whole value replaced by ``"***REDACTED***"``;
* free-form strings are scanned for obvious secret patterns such as
  ``Authorization: Bearer ...`` or ``password=...`` and the secret portion is
  masked;
* the redactor never raises: un-redactable values are replaced by a type tag.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***REDACTED***"

#: Substrings (matched case-insensitively against dict keys) that mark a value
#: as sensitive.
DEFAULT_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "password", "passwd", "pwd",
    "secret", "token", "api_key", "apikey", "authorization", "auth",
    "credential", "private_key", "cookie", "set-cookie",
    "x-diagnostics-token", "session",
})

#: (compiled lazily) patterns for secret material embedded in free text.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)([^\s,;]+)"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|passwd|secret)\s*[=:]\s*)([^\s,;&\"']+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{8,})"),
)

SENSITIVE_PATTERNS: tuple[str, ...] = tuple(p.pattern for p in _PATTERNS)

_MAX_DEPTH = 8
_MAX_STR_LEN = 2000


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    for name in DEFAULT_SENSITIVE_KEYS:
        normalized = name.replace("-", "_")
        if normalized in lowered:
            return True
    return False


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively replace sensitive keys/values with a mask marker."""
    if _depth > _MAX_DEPTH:
        return MASK
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                result[key] = MASK
            else:
                result[key] = redact(val, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        sequence = [redact(item, _depth=_depth + 1) for item in value]
        return sequence
    if isinstance(value, str):
        if len(value) > _MAX_STR_LEN:
            value = value[:_MAX_STR_LEN] + "...(truncated)"
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Unknown types: never str() arbitrary objects (could embed a secret via
    # __repr__); emit a safe type marker instead.
    return f"<{type(value).__name__}>"


def redact_text(text: str) -> str:
    """Mask obvious secret patterns inside a free-form string."""
    if not isinstance(text, str) or not text:
        return text
    masked = text
    for pattern in _PATTERNS:
        masked = pattern.sub(lambda m: m.group(1) + MASK, masked)
    return masked
