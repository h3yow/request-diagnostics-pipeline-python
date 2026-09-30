"""Diagnostics configuration loaded from environment variables.

Env vars (all prefixed ``DIAG_``):

============================= ==============================================
``DIAG_ENABLED``              ``1/true/yes/on`` to switch diagnostics on.
``DIAG_SAMPLE_RATE``          float in ``[0.0, 1.0]``; fraction recorded.
``DIAG_CAPACITY``             max number of retained records (oldest dropped).
``DIAG_QUERY_TOKEN``          bearer token required by the query endpoint.
``DIAG_LOG_SENSITIVE``        never honor ``1`` from env: logs stay redacted;
                              kept only as a dataclass field for explicit
                              in-process test configuration.
============================= ==============================================
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = frozenset({"1", "true", "yes", "on", "y"})


def _as_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def _clamp_rate(raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        rate = float(raw)
    except ValueError:
        return default
    if rate < 0.0:
        return 0.0
    if rate > 1.0:
        return 1.0
    return rate


def _as_int(raw: str | None, default: int, *, minimum: int = 1) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class DiagnosticsSettings:
    enabled: bool = False
    sample_rate: float = 1.0
    capacity: int = 1000
    query_token: str = ""
    # Safe-by-default: even when set, logs go through redaction unless a
    # caller explicitly opts in inside a trusted process.
    log_sensitive: bool = False

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "DiagnosticsSettings":
        source = os.environ if env is None else env
        return DiagnosticsSettings(
            enabled=_as_bool(source.get("DIAG_ENABLED"), False),
            sample_rate=_clamp_rate(source.get("DIAG_SAMPLE_RATE"), 1.0),
            capacity=_as_int(source.get("DIAG_CAPACITY"), 1000, minimum=1),
            query_token=source.get("DIAG_QUERY_TOKEN", "").strip(),
            # Environment can never enable sensitive logging.
            log_sensitive=False,
        )


_cached: DiagnosticsSettings | None = None


def get_settings() -> DiagnosticsSettings:
    """Process-cached settings; tests can replace via :func:`reset_settings`."""
    global _cached
    if _cached is None:
        _cached = DiagnosticsSettings.from_env()
    return _cached


def reset_settings(settings: DiagnosticsSettings | None = None) -> None:
    """Test helper: override or clear the cached configuration."""
    global _cached
    _cached = settings
