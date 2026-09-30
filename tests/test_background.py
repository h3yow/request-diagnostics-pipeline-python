"""Background task context propagation and finalization tests."""

import asyncio

import pytest

from diagnostics.background import spawn_background
from diagnostics.context import (
    DiagnosticContext,
    bind_context,
    clear_context,
    current_context,
)
from diagnostics.errors import ErrorCategory
from diagnostics.storage import DiagnosticStore


def test_success_propagates_ids_and_persists() -> None:
    seen: dict[str, str] = {}

    async def job() -> None:
        await asyncio.sleep(0.005)
        ctx = current_context()
        seen["rid"] = ctx.request_id
        seen["bid"] = ctx.business_id

    async def scenario() -> None:
        store = DiagnosticStore(10)
        bind_context(DiagnosticContext(request_id="r-ok", business_id="b-ok",
                                       sampled=True))
        task = spawn_background(job(), name="ok", store=store)
        clear_context()  # request side tears down its binding
        assert await task is None
        await asyncio.sleep(0.01)
        assert seen == {"rid": "r-ok", "bid": "b-ok"}
        rec = store.get("r-ok")
        assert rec is not None and rec["phase"] == "background"
        assert rec["error_category"] is None and rec["status_code"] == 200

    asyncio.run(scenario())


def test_failure_is_categorized_and_not_raised() -> None:
    async def job() -> None:
        raise ConnectionError("boom")

    async def scenario() -> None:
        store = DiagnosticStore(10)
        bind_context(DiagnosticContext(request_id="r-fail", sampled=True))
        task = spawn_background(job(), name="fail", store=store)
        assert await task is None  # swallowed after finalization
        rec = store.get("r-fail")
        assert rec["error_category"] == ErrorCategory.DEPENDENCY
        assert rec["status_code"] == 502

    asyncio.run(scenario())


def test_cancellation_finalizes_and_re_raises_cancel() -> None:
    async def slow() -> None:
        await asyncio.sleep(3600)

    async def scenario() -> None:
        store = DiagnosticStore(10)
        bind_context(DiagnosticContext(request_id="r-cancel", sampled=True))
        task = spawn_background(slow(), name="slow", store=store)
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rec = store.get("r-cancel")
        assert rec["error_category"] == ErrorCategory.CANCELLED
        assert rec["status_code"] == 499

    asyncio.run(scenario())


def test_unsampled_background_does_not_persist_but_still_logs() -> None:
    async def job() -> None:
        return None

    async def scenario() -> None:
        store = DiagnosticStore(10)
        bind_context(DiagnosticContext(request_id="r-nosample", sampled=False))
        task = spawn_background(job(), name="ok", store=store)
        await task
        assert store.get("r-nosample") is None
        assert len(store) == 0

    asyncio.run(scenario())


def test_concurrent_background_tasks_keep_distinct_identities() -> None:
    async def job(cid: str) -> str:
        await asyncio.sleep(0)
        return current_context().request_id

    async def scenario() -> None:
        store = DiagnosticStore(100)
        tasks = []
        for i in range(50):
            bind_context(DiagnosticContext(request_id=f"br-{i}",
                                           business_id=f"bb-{i}", sampled=True))
            tasks.append(spawn_background(job(f"br-{i}"), name=f"j{i}",
                                          store=store))
        clear_context()
        results = await asyncio.gather(*tasks)
        assert sorted(results) == sorted(f"br-{i}" for i in range(50))
        for i in range(50):
            rec = store.get(f"br-{i}")
            assert rec["business_id"] == f"bb-{i}"

    asyncio.run(scenario())


def test_spawn_without_running_loop_raises() -> None:
    async def noop() -> None:
        return None

    coro = noop()
    try:
        with pytest.raises(RuntimeError):
            spawn_background(coro, name="x")
    finally:
        coro.close()
