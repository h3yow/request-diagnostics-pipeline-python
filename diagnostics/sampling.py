"""Deterministic request sampling.

A request id is mapped uniformly into the ``[0.0, 1.0)`` interval with a
salted SHA-256 hash. The request is sampled when ``bucket < rate``:

* ``rate == 0.0`` -> nothing is ever sampled;
* ``rate == 1.0`` -> everything is always sampled;
* otherwise the boundary is stable: the same request id always gets the same
  decision, and a monotonically increasing rate only *adds* sampled ids.
"""

from __future__ import annotations

import hashlib

_SALT = b"diag-sampler-v1:"
_DENOMINATOR = float(1 << 32)


class Sampler:
    def __init__(self, rate: float) -> None:
        if rate < 0.0 or rate > 1.0:
            raise ValueError("sample rate must be within [0.0, 1.0]")
        self._rate = float(rate)

    @property
    def rate(self) -> float:
        return self._rate

    def bucket(self, request_id: str) -> float:
        """Expose the raw ``[0,1)`` bucket (useful for boundary tests)."""
        digest = hashlib.sha256(_SALT + request_id.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") / _DENOMINATOR

    def sample(self, request_id: str) -> bool:
        """Return True if the request should be recorded."""
        if self._rate <= 0.0:
            return False
        if self._rate >= 1.0:
            return True
        return self.bucket(request_id) < self._rate
