"""When DIAG_ENABLED is off, the service behaves exactly as a plain app."""

from __future__ import annotations

import httpx

from diagnostics.endpoints import register, register_exception_handlers
from diagnostics.middleware import DiagnosticsMiddleware
from diagnostics.sampling import Sampler
from diagnostics.settings import DiagnosticsSettings
from diagnostics.storage import DiagnosticStore


def _run(app, path, headers=None):
    import asyncio

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            return await c.get(path, headers=headers or {})
    return asyncio.run(go())


def test_disabled_diagnostics_is_transparent() -> None:
    from fastapi import FastAPI

    settings = DiagnosticsSettings(enabled=False, sample_rate=0.0)
    store = DiagnosticStore(10)
    app = FastAPI()
    app.add_middleware(DiagnosticsMiddleware, settings=settings, store=store,
                       sampler=Sampler(0.0))
    register_exception_handlers(app)
    register(app, settings=settings, store=store)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"Hello": "World"}

    r = _run(app, "/")
    assert r.status_code == 200 and r.json() == {"Hello": "World"}
    # Request id still helps tracing even when diagnostics is disabled...
    assert r.headers.get("x-request-id")
    # ...but nothing is recorded and the query surface does not exist.
    assert len(store) == 0
    assert _run(app, "/diagnostics/requests",
                headers={"X-Diagnostics-Token": "whatever"}).status_code == 404
