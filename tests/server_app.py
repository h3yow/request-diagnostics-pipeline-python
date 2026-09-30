"""Minimal app used by the real-server integration test.

Run as its own process via uvicorn so background tasks survive the request.
Diagnostics is enabled at 100% sampling; the in-process store can be read
back through the token-protected query API.
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DIAG_ENABLED", "1")
os.environ.setdefault("DIAG_SAMPLE_RATE", "1")
os.environ.setdefault("DIAG_CAPACITY", "500")
os.environ.setdefault("DIAG_QUERY_TOKEN", "secret-token")

from fastapi import FastAPI  # noqa: E402

from diagnostics.background import spawn_background  # noqa: E402
from diagnostics.context import require_context  # noqa: E402
from diagnostics.endpoints import register, register_exception_handlers  # noqa: E402
from diagnostics.errors import DependencyError  # noqa: E402
from diagnostics.middleware import DiagnosticsMiddleware  # noqa: E402
from diagnostics.sampling import Sampler  # noqa: E402
from diagnostics.settings import DiagnosticsSettings  # noqa: E402
from diagnostics.storage import DiagnosticStore  # noqa: E402

settings = DiagnosticsSettings.from_env()
store = DiagnosticStore(settings.capacity)

app = FastAPI()
app.add_middleware(DiagnosticsMiddleware, settings=settings, store=store,
                   sampler=Sampler(1.0))
register_exception_handlers(app)
register(app, settings=settings, store=store)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/chain")
async def chain() -> dict[str, str]:
    async def bg() -> None:
        await asyncio.sleep(0.05)
        raise ConnectionError("bg downstream down")

    ctx = require_context()
    spawn_background(bg(), name="chain-bg", store=store)
    raise DependencyError()
