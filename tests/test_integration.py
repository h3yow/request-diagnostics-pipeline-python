"""End-to-end integration: gate, permissions, sampling, ids, main app."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from diagnostics.endpoints import register, register_exception_handlers
from diagnostics.errors import DependencyError, ValidationError
from diagnostics.middleware import DiagnosticsMiddleware
from diagnostics.sampling import Sampler
from diagnostics.settings import DiagnosticsSettings
from diagnostics.storage import DiagnosticStore


def _app(*, enabled: bool, rate: float = 1.0, capacity: int = 50,
         token: str = "secret-token"):
    store = DiagnosticStore(capacity)
    settings = DiagnosticsSettings(enabled=enabled, sample_rate=rate,
                                   capacity=capacity, query_token=token)
    app = FastAPI()
    app.add_middleware(DiagnosticsMiddleware, settings=settings, store=store,
                       sampler=Sampler(rate))
    register_exception_handlers(app)
    register(app, settings=settings, store=store)
    return app, store


def _run(app, method, path, headers=None):
    import asyncio
    import httpx

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            return await c.request(method, path, headers=headers or {})
    return asyncio.run(go())


# ---------------------------------------------------------------------------
# enable gate
# ---------------------------------------------------------------------------
def test_diagnostics_routes_absent_when_disabled() -> None:
    app, _ = _app(enabled=False)

    @app.get("/x")
    async def x() -> dict[str, int]:
        return {"x": 1}

    assert _run(app, "GET", "/x").status_code == 200
    assert _run(app, "GET", "/diagnostics/requests",
                headers={"X-Diagnostics-Token": "secret-token"}).status_code == 404


# ---------------------------------------------------------------------------
# permission rejection through the real HTTP endpoint
# ---------------------------------------------------------------------------
def test_query_endpoint_requires_token() -> None:
    app, _ = _app(enabled=True)

    missing = _run(app, "GET", "/diagnostics/requests")
    wrong = _run(app, "GET", "/diagnostics/requests",
                 headers={"X-Diagnostics-Token": "nope"})
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing.json()["error"]["category"] == "permission_denied"


def test_query_endpoint_accepts_valid_token_and_lists_records() -> None:
    app, store = _app(enabled=True)

    @app.get("/v")
    async def v() -> dict[str, str]:
        return {"v": "ok"}

    _run(app, "GET", "/v")
    _run(app, "GET", "/v", headers={"X-Diagnostics-Token": "secret-token"})

    ok = _run(app, "GET", "/diagnostics/requests",
              {"X-Diagnostics-Token": "secret-token"})
    assert ok.status_code == 200
    records = ok.json()["records"]
    # Exactly the two /v calls; the /diagnostics query itself is not recorded.
    assert len(records) == 2
    assert {r["path"] for r in records} == {"/v"}
    assert len({r["request_id"] for r in records}) == 2

    one = _run(app, "GET",
               f"/diagnostics/requests/{records[0]['request_id']}",
               headers={"X-Diagnostics-Token": "secret-token"})
    assert one.status_code == 200 and one.json()["path"] == "/v"


def test_query_single_record_404_for_unknown_id() -> None:
    app, _ = _app(enabled=True)
    r = _run(app, "GET", "/diagnostics/requests/unknown",
             headers={"X-Diagnostics-Token": "secret-token"})
    assert r.status_code == 404
    assert r.json()["error"]["category"] == "not_found"


# ---------------------------------------------------------------------------
# sampling boundary over real HTTP traffic
# ---------------------------------------------------------------------------
def test_sampling_boundary_zero_vs_one_over_http() -> None:
    app_full, store_full = _app(enabled=True, rate=1.0, capacity=500)

    @app_full.get("/f")
    async def f() -> str:
        return "f"

    app_zero, store_zero = _app(enabled=True, rate=0.0, capacity=500)

    @app_zero.get("/z")
    async def z() -> str:
        return "z"

    for _ in range(100):
        assert _run(app_full, "GET", "/f").status_code == 200
        assert _run(app_zero, "GET", "/z").status_code == 200
    assert len(store_full) == 100
    assert len(store_zero) == 0


def test_partial_sampling_is_stable_per_request_id() -> None:
    # Same inbound request id must always get the same decision; find one id
    # on each side of the 0.5 boundary and assert determinism over repeats.
    app, store = _app(enabled=True, rate=0.5)

    @app.get("/s")
    async def s() -> str:
        return "s"

    decisions: dict[str, bool] = {}
    for i in range(60):
        rid = f"stable-{i}"
        for _ in range(3):
            _run(app, "GET", "/s", headers={"X-Request-Id": rid})
        decisions[rid] = store.get(rid) is not None
    sampled_ids = [r for r, v in decisions.items() if v]
    dropped_ids = [r for r, v in decisions.items() if not v]
    assert sampled_ids and dropped_ids  # both sides of the boundary exist
    assert 0.2 < len(sampled_ids) / len(decisions) < 0.8


# ---------------------------------------------------------------------------
# full chain: ids span request -> background -> error categorization
# ---------------------------------------------------------------------------
def test_request_background_and_error_share_request_id(uvicorn_server) -> None:
    import json
    import time
    import urllib.request

    # tests/ is on sys.path via pytest pythonpath
    base = uvicorn_server("server_app")
    req = urllib.request.Request(
        base + "/chain",
        headers={"X-Request-Id": "chain-1", "X-Business-Id": "biz-chain"})
    try:
        urllib.request.urlopen(req)
        raise AssertionError("expected 502")
    except urllib.error.HTTPError as exc:
        assert exc.code == 502
        body = json.loads(exc.read())
        assert body["error"] == {
            "category": "dependency_error",
            "message": "downstream dependency error",
            "request_id": "chain-1",
        }

    # Wait for the background task (which fails ~50ms in) to be finalized.
    diag_req = urllib.request.Request(
        base + "/diagnostics/requests",
        headers={"X-Diagnostics-Token": "secret-token"})
    records = []
    deadline = time.time() + 5
    while time.time() < deadline:
        with urllib.request.urlopen(diag_req) as resp:
            records = json.loads(resp.read())["records"]
        if any(r.get("phase") == "background" for r in records):
            break
        time.sleep(0.1)

    request_rec = [r for r in records
                   if r.get("phase") == "request" and r["request_id"] == "chain-1"]
    bg_rec = [r for r in records
              if r.get("phase") == "background" and r["request_id"] == "chain-1"]
    assert len(request_rec) == 1 and len(bg_rec) == 1
    for rec in (*request_rec, *bg_rec):
        assert rec["request_id"] == "chain-1"
        assert rec["business_id"] == "biz-chain"
    assert request_rec[0]["error_category"] == "dependency_error"
    assert bg_rec[0]["error_category"] == "dependency_error"
    assert bg_rec[0]["task"] == "chain-bg"


def test_background_task_is_finalized_when_request_scope_is_cancelled() -> None:
    # In-process ASGI transports (and real servers under client disconnect)
    # can cancel the request task tree; the background job must still be
    # categorized/finalized rather than swallowed silently.
    import asyncio

    import httpx

    app, store = _app(enabled=True, rate=1.0)

    @app.get("/chain-cancel")
    async def chain_cancel() -> dict[str, str]:
        from diagnostics.background import spawn_background
        from diagnostics.context import require_context

        async def bg() -> None:
            await asyncio.sleep(3600)  # outlives the request

        ctx = require_context()
        spawn_background(bg(), name="chain-slow", store=store)
        raise DependencyError()

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            with pytest.raises(httpx.HTTPStatusError):
                (await c.get("/chain-cancel",
                             headers={"X-Request-Id": "chain-c-1"})).raise_for_status()
        # Give the task tree cancellation + finalization a tick.
        await asyncio.sleep(0.05)

    asyncio.run(go())
    records = store.list_recent(100)
    bg = [r for r in records if r.get("phase") == "background"]
    assert bg and bg[0]["request_id"] == "chain-c-1"
    assert bg[0]["error_category"] == "cancelled"
