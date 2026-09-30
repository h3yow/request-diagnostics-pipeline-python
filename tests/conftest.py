"""Shared pytest fixtures.

Each test runs with diagnostics **enabled**, sampling at 100% and a fresh
in-memory store. A memory log handler captures the structured JSON log lines
so tests can assert on ``request_id`` / ``error_category`` / ``sampled``
without polluting pytest output.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from diagnostics import logging_setup
from diagnostics.middleware import DiagnosticsMiddleware
from diagnostics.sampling import Sampler
from diagnostics.settings import DiagnosticsSettings
from diagnostics.storage import DiagnosticStore


@dataclass
class DiagEnv:
    settings: DiagnosticsSettings
    store: DiagnosticStore
    logs: list[dict[str, Any]]


class _MemoryHandler(logging.Handler):
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging_setup._JsonFormatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(self.format(record))
        except Exception:  # pragma: no cover
            return
        self.sink.append(payload)
        # Concise one-liner so local test runs (use ``pytest -s``) and failure
        # output always show the ids/category/sampling decision.
        print(
            "[diag-test] event={event} request_id={rid} "
            "error_category={cat} sampled={sampled}".format(
                event=payload.get("event"),
                rid=payload.get("request_id", "-"),
                cat=payload.get("error_category", "-"),
                sampled=payload.get("sampled", "-"),
            )
        )


@pytest.fixture
def diag_env() -> DiagEnv:
    sink: list[dict[str, Any]] = []
    handler = _MemoryHandler(sink)
    logger = logging.getLogger("diagnostics")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    env = DiagEnv(
        settings=DiagnosticsSettings(
            enabled=True, sample_rate=1.0, capacity=100, query_token="test-token"),
        store=DiagnosticStore(100),
        logs=sink,
    )
    try:
        yield env
    finally:
        logger.removeHandler(handler)


def build_app(env: DiagEnv, *, rate: float | None = None) -> Any:
    """Construct a FastAPI app wired with diagnostics at the given rate."""
    from fastapi import FastAPI

    from diagnostics.endpoints import register, register_exception_handlers

    settings = env.settings
    if rate is not None:
        settings = DiagnosticsSettings(
            enabled=True, sample_rate=rate, capacity=env.settings.capacity,
            query_token=env.settings.query_token)
    app = FastAPI()
    app.add_middleware(
        DiagnosticsMiddleware,
        settings=settings,
        store=env.store,
        sampler=Sampler(settings.sample_rate),
    )
    register_exception_handlers(app)
    register(app, settings=settings, store=env.store)
    return app


@pytest.fixture
def app_factory(diag_env: DiagEnv):
    return lambda **kw: build_app(diag_env, **kw)


@pytest.fixture
def arequest():
    """Perform an ASGI request without a real socket (httpx 0.28 async API)."""

    async def _request(app: Any, method: str, url: str, *,
                       headers: dict[str, str] | None = None,
                       json: Any = None) -> Any:
        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as client:
            return await client.request(method, url, headers=headers or {},
                                        json=json)

    def call(app: Any, method: str, url: str, **kwargs: Any) -> Any:
        import asyncio
        return asyncio.run(_request(app, method, url, **kwargs))

    return call


@pytest.fixture
def uvicorn_server():
    """Run an ASGI app on a real ephemeral port (needed so background tasks
    outlive the request instead of being cancelled by the in-process ASGI
    transport)."""
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    procs: list[subprocess.Popen] = []

    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def start(module_app: str) -> str:
        port = _free_port()
        code = (
            "import uvicorn;"
            f"import {module_app} as m;"
            f"uvicorn.run(m.app, host='127.0.0.1', port={port}, log_level='error')"
        )
        project_root = str(Path(__file__).resolve().parent.parent)
        tests_dir = str(Path(__file__).resolve().parent)
        env = {
            "PYTHONPATH": project_root + os.pathsep + tests_dir,
            "PATH": os.environ.get("PATH", ""),
            "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
        }
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
        procs.append(proc)
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/health", timeout=1) as resp:
                    if resp.status == 200:
                        return base
            except Exception:
                time.sleep(0.1)
        proc.kill()
        raise RuntimeError("test server failed to start")

    yield start

    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
