"""Permission check for the diagnostics query endpoint.

Rules:

* the whole diagnostics surface (enable + query) is disabled unless
  ``DIAG_ENABLED`` is on; the caller (router) handles that;
* when enabled, every request must present ``X-Diagnostics-Token`` (or
  ``Authorization: Bearer <token>``) matching the configured
  ``DIAG_QUERY_TOKEN``;
* comparison is constant-time; an empty configured token means *deny all*
  (fail closed), so a misconfigured deployment cannot expose diagnostics;
* the token is never echoed back in the decision reason.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    reason: str = ""


def _extract_token(headers: dict[str, str]) -> str:
    # Case-insensitive header lookup.
    lowered = {k.lower(): v for k, v in headers.items()}
    token = lowered.get("x-diagnostics-token", "").strip()
    if token:
        return token
    auth = lowered.get("authorization", "").strip()
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def authorize(headers: dict[str, str], expected_token: str) -> AuthDecision:
    expected = (expected_token or "").strip()
    if not expected:
        # Fail closed: no token configured -> nobody is authorized.
        return AuthDecision(False, "diagnostics token not configured")
    provided = _extract_token(headers)
    if not provided:
        return AuthDecision(False, "missing diagnostics token")
    if hmac.compare_digest(provided, expected):
        return AuthDecision(True, "ok")
    return AuthDecision(False, "invalid diagnostics token")
