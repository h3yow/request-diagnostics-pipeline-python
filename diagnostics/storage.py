"""Bounded, best-effort in-memory diagnostic record store.

Design constraints:

* **Capacity bound** - a :class:`collections.deque` with ``maxlen`` evicts the
  oldest record when full, so memory usage is bounded.
* **Multiple records per request** - one request may produce a request record
  plus several background-task records (all sharing a ``request_id``). The
  deque keeps every record; ``get`` returns the request record with a
  ``related`` list of the others.
* **Best effort** - every public method catches its own exceptions; a broken
  store must never propagate into the request path or alter a business result.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

logger = logging.getLogger("diagnostics.storage")


class DiagnosticStore:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        # request_id -> deque of records (oldest -> newest), bounded loosely
        # by the same capacity.
        self._index: dict[str, deque[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self.dropped = 0
        # Test seam: when True, put simulates a storage backend failure.
        self.fail_writes = False

    def put(self, record: dict[str, Any]) -> None:
        try:
            with self._lock:
                if self.fail_writes:
                    raise RuntimeError("simulated storage failure")
                request_id = str(record.get("request_id", ""))
                if len(self._records) == self._capacity:
                    oldest = self._records[0]
                    self._drop_from_index(oldest)
                    self.dropped += 1
                self._records.append(record)
                if request_id:
                    bucket = self._index.setdefault(request_id, deque())
                    bucket.append(record)
                    # Index buckets only need to survive alongside the deque;
                    # trim absurdly long per-id growth defensively.
                    while len(bucket) > self._capacity:
                        bucket.popleft()
        except Exception:  # pragma: no cover - exercised via tests
            logger.debug("diagnostic record write failed", exc_info=True)

    def _drop_from_index(self, record: dict[str, Any]) -> None:
        request_id = str(record.get("request_id", ""))
        bucket = self._index.get(request_id)
        if not bucket:
            return
        try:
            bucket.remove(record)
        except ValueError:
            # Record already gone (evicted/replaced); drop stale empty buckets.
            pass
        if not bucket:
            self._index.pop(request_id, None)

    def get(self, request_id: str) -> dict[str, Any] | None:
        """Return the primary (request-phase, else newest) record plus related."""
        try:
            with self._lock:
                bucket = self._index.get(request_id)
                if not bucket:
                    return None
                primary = next(
                    (r for r in bucket if r.get("phase") == "request"),
                    bucket[-1],
                )
                related = [dict(r, _indexed=True) for r in bucket if r is not primary]
                result = dict(primary)
                if related:
                    result["related"] = [
                        {k: v for k, v in r.items() if k != "_indexed"}
                        for r in related
                    ]
                return result
        except Exception:  # pragma: no cover
            logger.debug("diagnostic record read failed", exc_info=True)
            return None

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            with self._lock:
                if limit <= 0:
                    return []
                return list(self._records)[-limit:]
        except Exception:  # pragma: no cover
            logger.debug("diagnostic record list failed", exc_info=True)
            return []

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._index.clear()
            self.dropped = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
