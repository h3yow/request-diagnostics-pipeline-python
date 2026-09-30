"""Demo service equipped with the request-diagnostics pipeline.

The original ``GET /`` contract is preserved exactly. Additional routes exist
to exercise every diagnostics branch locally:

* ``GET /items/{item_id}``  - validation branch (non-positive id)
* ``GET /dependencies/fail``    - dependency failure branch
* ``GET /dependencies/timeout`` - timeout branch (asyncio timeout)
* ``POST /jobs``                - background task (ok / fail / slow modes)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from diagnostics import logging_setup
from diagnostics.background import spawn_background
from diagnostics.context import require_context
from diagnostics.endpoints import register, register_exception_handlers
from diagnostics.errors import (
    DependencyError,
    TimeoutError as DiagnosticTimeoutError,
    ValidationError,
)

from diagnostics.middleware import DiagnosticsMiddleware
from diagnostics.sampling import Sampler
from diagnostics.settings import DiagnosticsSettings
from diagnostics.storage import DiagnosticStore

settings = DiagnosticsSettings.from_env()
store = DiagnosticStore(settings.capacity) if settings.enabled else None
sampler = Sampler(settings.sample_rate) if settings.enabled else Sampler(0.0)

app = FastAPI(title="request-diagnostics demo")
app.add_middleware(
    DiagnosticsMiddleware,
    settings=settings,
    store=store,
    sampler=sampler,
)
register_exception_handlers(app)
register(app, settings=settings, store=store if store is not None else DiagnosticStore(1))
logging_setup.configure_logging()


# ---------------------------------------------------------------------------
# Original interface - kept byte-compatible (same field and status semantics).
# ---------------------------------------------------------------------------
@app.get("/")
def read_root() -> dict[str, str]:
    return {"Hello": "World"}


# ---------------------------------------------------------------------------
# Demo routes exercising each error branch.
# ---------------------------------------------------------------------------
@app.get("/items/{item_id}")
async def get_item(item_id: int) -> dict[str, Any]:
    if item_id <= 0:
        raise ValidationError(details={"field": "item_id"})
    ctx = require_context()
    return {
        "item_id": item_id,
        "business_id": ctx.business_id,
        "request_id": ctx.request_id,
    }


async def _call_downstream(*, fail: bool = False, delay: float = 0.0) -> str:
    await asyncio.sleep(delay)
    if fail:
        raise ConnectionError("simulated upstream refused connection")
    return "ok"


@app.get("/dependencies/fail")
async def dependency_failure() -> dict[str, str]:
    ctx = require_context()
    with ctx.span("upstream.call", kind="http"):
        try:
            await _call_downstream(fail=True)
        except ConnectionError as exc:
            raise DependencyError() from exc
    return {"status": "unreachable"}


@app.get("/dependencies/timeout")
async def dependency_timeout() -> dict[str, str]:
    try:
        with require_context().span("upstream.call", kind="http", timeout_s=0.05):
            async with asyncio.timeout(0.05):
                await _call_downstream(delay=1.0)
    except TimeoutError:
        # Builtin asyncio timeout -> stable diagnostics timeout category.
        raise DiagnosticTimeoutError() from None
    return {"status": "unreachable"}


class JobRequest(BaseModel):
    mode: str = "ok"


@app.post("/jobs")
async def create_job(payload: JobRequest) -> dict[str, str]:
    ctx = require_context()

    async def _job() -> str:
        if payload.mode == "fail":
            await asyncio.sleep(0.01)
            raise ConnectionError("job upstream down")
        if payload.mode == "slow":
            await asyncio.sleep(3600)
        await asyncio.sleep(0.01)
        return f"done:{payload.mode}"

    spawn_background(_job(), name=f"job:{payload.mode}",
                     store=store if store is not None else None)
    return {"status": "accepted", "request_id": ctx.request_id}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
