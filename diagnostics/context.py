"""Request-scoped diagnostic context.

Storage is a :class:`contextvars.ContextVar`, which gives automatic isolation
between concurrent asyncio tasks (and between threads). A background task
created via :func:`diagnostics.background.spawn_background` receives an
explicit copy of the originating context, so ids propagate without leaking
between requests.
"""

from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class Span:
    name: str
    started_at: float = 0.0
    ended_at: float = 0.0
    status: str = "ok"
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if not self.ended_at:
            return 0.0
        return round((self.ended_at - self.started_at) * 1000.0, 3)

    def finish(self, status: str = "ok", **attrs: Any) -> None:
        self.ended_at = time.perf_counter()
        self.status = status
        if attrs:
            self.attrs.update(attrs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attrs": self.attrs,
        }


@dataclass
class DiagnosticContext:
    request_id: str = ""
    business_id: str = ""
    sampled: bool = False
    spans: list[Span] = field(default_factory=list)
    error_category: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    method: str = ""
    path: str = ""
    status_code: int = 0
    finished: bool = False

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Span]:
        span = Span(name=name, started_at=time.perf_counter(), attrs=dict(attrs))
        self.spans.append(span)
        try:
            yield span
        except BaseException:
            if not span.ended_at:
                span.finish(status="error")
            raise
        else:
            if not span.ended_at:
                span.finish()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started_at) * 1000.0, 3)

    def snapshot(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "business_id": self.business_id,
            "sampled": self.sampled,
            "method": self.method,
            "path": self.path,
            "status_code": self.status_code,
            "error_category": self.error_category,
            "duration_ms": self.elapsed_ms(),
            "spans": [span.to_dict() for span in self.spans],
        }


_CURRENT: contextvars.ContextVar[DiagnosticContext | None] = (
    contextvars.ContextVar("diagnostic_context", default=None)
)


class ContextVarStorage:
    """Thin wrapper over the ContextVar to ease substitution in tests."""

    def get(self) -> DiagnosticContext | None:
        return _CURRENT.get()

    def set(self, ctx: DiagnosticContext) -> None:
        _CURRENT.set(ctx)

    def reset(self) -> None:
        _CURRENT.set(None)


_default_storage = ContextVarStorage()
_context = _default_storage  # public module-level accessor


def current_context() -> DiagnosticContext | None:
    return _context.get()


def require_context() -> DiagnosticContext:
    ctx = _context.get()
    if ctx is None:
        raise LookupError("no active diagnostic context")
    return ctx


def bind_context(ctx: DiagnosticContext) -> None:
    _context.set(ctx)


def clear_context() -> None:
    _context.reset()
