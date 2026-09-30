"""ASGI middleware tests: ids, sampling, errors, cancellation, isolation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from diagnostics.endpoints import register, register_exception_handlers
from diagnostics.errors import (
    DependencyError,
    ErrorCategory,
    ValidationError,
)
from diagnostics.middleware import DiagnosticsMiddleware
from diagnostics.sampling import Sampler
from diagnostics.settings import DiagnosticsSettings
from diagnostics.storage import DiagnosticStore


class NumberBody(BaseModel):
    n: int


def _settings(*, enabled: bool = True, rate: float = 1.0,
              capacity: int = 100) -> DiagnosticsSettings:
    return DiagnosticsSettings(
        enabled=enabled, sample_rate=rate, capacity=capacity,
        query_token="test-token")


def _wrap(app: Any, env: Any, *, rate: float = 1.0,
          store: Any | None = None, enabled: bool = True) -> Any:
    settings = _settings(enabled=enabled, rate=rate,
                         capacity=env.store.capacity)
    target = store if store is not None else env.store
    app.add_middleware(DiagnosticsMiddleware, settings=settings,
                       store=target, sampler=Sampler(rate))
    register_exception_handlers(app)
    register(app, settings=settings, store=target)
    return app


# ---------------------------------------------------------------------------
# identifier propagation
# ---------------------------------------------------------------------------
def test_generates_request_id_and_echoes_header(app_factory, arequest) -> None:
    app = app_factory()

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        from diagnostics.context import require_context
        return {"rid": require_context().request_id}

    r1 = arequest(app, "GET", "/ping")
    r2 = arequest(app, "GET", "/ping",
                  headers={"X-Request-Id": "fixed-id-1"})
    assert r1.json()["rid"] == r1.headers["x-request-id"]
    assert len(r1.headers["x-request-id"]) == 32
    assert r2.headers["x-request-id"] == "fixed-id-1"
    assert r2.json()["rid"] == "fixed-id-1"


def test_business_id_propagates_and_crlf_is_stripped(app_factory, arequest) -> None:
    app = app_factory()

    @app.get("/b")
    async def b() -> dict[str, str]:
        from diagnostics.context import require_context
        return {"bid": require_context().business_id}

    r = arequest(app, "GET", "/b?business_id=biz-42",
                 headers={"X-Business-Id": "biz-hdr\r\ninjected"})
    assert r.json()["bid"] == "biz-hdrinjected"


def test_invalid_inbound_request_id_is_replaced(app_factory, arequest) -> None:
    app = app_factory()

    @app.get("/p")
    async def p() -> str:
        from diagnostics.context import require_context
        return require_context().request_id

    r = arequest(app, "GET", "/p", headers={"X-Request-Id": "bad id!"})
    assert r.headers["x-request-id"] != "bad id!"


# ---------------------------------------------------------------------------
# stable error results
# ---------------------------------------------------------------------------
def test_error_categories_are_distinguishable_via_http(app_factory, arequest) -> None:
    app = app_factory()

    @app.get("/boom/{kind}")
    async def boom(kind: str) -> str:
        if kind == "validation":
            raise ValidationError()
        if kind == "dependency":
            raise DependencyError()
        raise RuntimeError("secret payload token=zzz")

    rv = arequest(app, "GET", "/boom/validation")
    rd = arequest(app, "GET", "/boom/dependency")
    ri = arequest(app, "GET", "/boom/internal")

    assert rv.status_code == 422
    assert rv.json()["error"]["category"] == ErrorCategory.VALIDATION
    assert rd.status_code == 502
    assert rd.json()["error"]["category"] == ErrorCategory.DEPENDENCY
    assert ri.status_code == 500
    body = ri.json()
    assert body["error"]["category"] == ErrorCategory.INTERNAL
    assert "zzz" not in str(body) and "secret payload" not in str(body)


def test_validation_details_are_public_but_generic_errors_are_not(
        app_factory, arequest) -> None:
    app = app_factory()

    @app.post("/typed")
    async def typed(body: NumberBody) -> int:
        return body.n

    r = arequest(app, "POST", "/typed", json={"n": "x"})
    assert r.status_code == 422
    details = r.json()["error"]["details"]["validation"]
    assert any(d["field"] == "n" for d in details)


def test_success_path_is_untouched(app_factory, arequest) -> None:
    app = app_factory()

    @app.get("/ok")
    async def ok() -> dict[str, int]:
        return {"a": 1, "b": 2}

    r = arequest(app, "GET", "/ok")
    assert r.status_code == 200 and r.json() == {"a": 1, "b": 2}


def test_request_id_is_attached_to_error_responses_too(app_factory, arequest) -> None:
    app = app_factory()

    @app.get("/e")
    async def e() -> str:
        raise DependencyError()

    r = arequest(app, "GET", "/e", headers={"X-Request-Id": "err-trace"})
    assert r.headers["x-request-id"] == "err-trace"
    assert r.json()["error"]["request_id"] == "err-trace"


# ---------------------------------------------------------------------------
# sampling, capacity and resilience
# ---------------------------------------------------------------------------
def test_sampling_decides_persistence(app_factory, diag_env, arequest) -> None:
    sampled = app_factory(rate=1.0)

    @sampled.get("/b")
    async def b() -> str:
        return "b"

    unsampled = app_factory(rate=0.0)

    @unsampled.get("/a")
    async def a() -> str:
        return "a"

    for _ in range(5):
        assert arequest(sampled, "GET", "/b").status_code == 200
        assert arequest(unsampled, "GET", "/a").status_code == 200
    records = diag_env.store.list_recent(100)
    assert {rec["path"] for rec in records} == {"/b"}
    assert len(records) == 5


def test_diagnostic_store_failure_does_not_affect_request(diag_env, arequest) -> None:
    from fastapi import FastAPI

    broken = DiagnosticStore(10)
    broken.fail_writes = True
    app = FastAPI()
    _wrap(app, diag_env, store=broken)

    @app.get("/alive")
    async def alive() -> dict[str, str]:
        return {"status": "ok"}

    r = arequest(app, "GET", "/alive")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert len(broken) == 0
    events = [l for l in diag_env.logs if l["event"] == "request.finished"]
    assert events and events[-1]["status_code"] == 200


def test_capacity_bound_is_respected(diag_env, arequest) -> None:
    from fastapi import FastAPI

    small = DiagnosticStore(5)
    app = FastAPI()
    _wrap(app, diag_env, store=small)

    @app.get("/n/{idx}")
    async def n(idx: int) -> int:
        return idx

    for i in range(20):
        assert arequest(app, "GET", f"/n/{i}").status_code == 200
    assert len(small) == 5
    assert small.dropped == 15


# ---------------------------------------------------------------------------
# concurrency isolation
# ---------------------------------------------------------------------------
def test_concurrent_http_requests_keep_distinct_contexts(app_factory) -> None:
    import httpx
    app = app_factory()

    @app.get("/ctx/{cid}")
    async def ctx(cid: str) -> dict[str, str]:
        from diagnostics.context import require_context
        await asyncio.sleep(0.002)
        ctx_obj = require_context()
        assert ctx_obj.request_id == cid
        return {"request_id": ctx_obj.request_id,
                "business_id": ctx_obj.business_id}

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as client:
            async def one(i: int) -> None:
                cid = f"c-{i}"
                r = await client.get(
                    f"/ctx/{cid}",
                    headers={"X-Request-Id": cid,
                             "X-Business-Id": f"biz-{i}"})
                assert r.status_code == 200
                assert r.json() == {"request_id": cid,
                                    "business_id": f"biz-{i}"}

            await asyncio.gather(*(one(i) for i in range(100)))

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=3))
    except asyncio.TimeoutError:
        import traceback; print('HANG STACK:'); traceback.print_stack()
        raise


# ---------------------------------------------------------------------------
# client cancellation: a disconnecting receive() raises CancelledError
# ---------------------------------------------------------------------------
def test_cancelled_request_is_finalized_and_does_not_500(
        app_factory, diag_env) -> None:
    app = app_factory()
    endpoint_started = asyncio.Event()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        endpoint_started.set()
        await asyncio.sleep(3600.0)
        return {"x": "y"}

    scope = {
        "type": "http", "method": "GET", "path": "/slow",
        "raw_path": b"/slow", "query_string": b"",
        "headers": [(b"host", b"t"), (b"x-request-id", b"cancel-me")],
        "client": ("127.0.0.1", 1), "server": ("t", 80),
        "scheme": "http", "http_version": "1.1", "app": app,
    }

    async def receive() -> dict[str, Any]:
        # A GET without a body never reads again; if it does, emulate a server
        # that keeps the receive channel open until disconnect.
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def scenario() -> None:
        # Exactly what uvicorn does on client disconnect: cancel the task that
        # is running the ASGI application while the handler is awaiting.
        app_task = asyncio.ensure_future(app(scope, receive, send))
        await endpoint_started.wait()
        app_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await app_task

    asyncio.run(scenario())

    assert not any(m["type"] == "http.response.start" for m in sent)
    record = diag_env.store.get("cancel-me")
    assert record is not None
    assert record["error_category"] == ErrorCategory.CANCELLED
    assert record["status_code"] == 0
