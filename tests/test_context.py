"""Context binding and concurrent-isolation tests."""

import asyncio

import pytest

from diagnostics.context import (
    DiagnosticContext,
    bind_context,
    clear_context,
    current_context,
    require_context,
)


async def _worker(cid: str, rounds: int = 20) -> tuple[str, list[float]]:
    ctx = DiagnosticContext(request_id=cid, business_id=f"biz-{cid}", sampled=True)
    bind_context(ctx)
    durations = []
    for _ in range(rounds):
        with ctx.span("step") as span:
            await asyncio.sleep(0)
        assert current_context() is ctx
        assert require_context().request_id == cid
        durations.append(span.duration_ms)
    return cid, durations



def test_concurrent_requests_never_cross_talk() -> None:
    n = 200

    async def scenario() -> None:
        results = await asyncio.gather(*(_worker(f"req-{i}") for i in range(n)))
        ids = [cid for cid, _ in results]
        assert len(set(ids)) == n
        for cid, durations in results:
            assert all(d >= 0.0 for d in durations)

    asyncio.run(scenario())


def test_require_context_raises_outside_request() -> None:
    clear_context()
    with pytest.raises(LookupError):
        require_context()
    assert current_context() is None


def test_span_marks_error_and_keeps_duration() -> None:
    ctx = DiagnosticContext(request_id="r", business_id="b")
    with pytest.raises(RuntimeError):
        with ctx.span("boom"):
            raise RuntimeError("bad")
    span = ctx.spans[0]
    assert span.status == "error"
    assert span.duration_ms >= 0.0
    snap = ctx.snapshot()
    assert snap["request_id"] == "r"
    assert snap["spans"][0]["status"] == "error"


def test_child_task_inherits_contextvars_copy() -> None:
    async def scenario() -> None:
        parent = DiagnosticContext(request_id="parent", business_id="pb")
        bind_context(parent)

        async def child() -> str:
            await asyncio.sleep(0)
            return require_context().request_id

        task = asyncio.ensure_future(child())
        clear_context()  # parent resets/rebinds after scheduling
        assert await task == "parent"
        assert current_context() is None

    asyncio.run(scenario())
