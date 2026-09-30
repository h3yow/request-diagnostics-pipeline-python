"""请求取消/超时后的收尾（ASGI 层面）。"""
from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

log = logging.getLogger("app.tests")


@pytest.mark.asyncio
async def test_disconnect_cancels_request_and_finalizes(app_instance):
    """客户端断开 -> 路由的 CancelledError 被归一化，服务保持健康。"""
    got_start = asyncio.Event()

    async def transport_receive():
        # 第一次给出请求，第二次通知断开
        yield {"type": "http.request", "body": b"", "more_body": False}
        yield {"type": "http.disconnect"}

    req_messages = []
    receive_iter = transport_receive().__aiter__()

    async def receive():
        return await receive_iter.__anext__()

    async def send(message):
        req_messages.append(message)
        if message["type"] == "http.response.start":
            got_start.set()

    # /wait-disconnect 会轮询 is_disconnected()；断开后抛 CancelledError。
    # 中间件先完成收尾（记录落盘、后台任务取消），再按 ASGI 语义重新抛出。
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/wait-disconnect",
        "raw_path": b"/wait-disconnect",
        "query_string": b"",
        "headers": [(b"x-request-id", b"ridcancel0001")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    with pytest.raises(asyncio.CancelledError):
        await app_instance(scope, receive, send)

    # 收尾已经完成：诊断记录标记为 cancelled
    from app.diagnostics import get_store

    rec = get_store().get("ridcancel0001")
    assert rec is not None
    assert rec.cancelled is True
    assert rec.category == "cancelled"
    log.info("客户端断开收尾完成 request_id=ridcancel0001 category=cancelled sampled=True",
             extra={"category": "cancelled"})


@pytest.mark.asyncio
async def test_route_level_timeout_recorded_via_asgi(app_instance):
    transport = httpx.ASGITransport(app=app_instance)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac:
        resp = await ac.get("/fail/timeout")
    assert resp.status_code == 504
    body = resp.json()
    assert body["error"]["category"] == "timeout"
    assert resp.headers["x-request-id"].startswith("req_")
    log.info("超时归一化 request_id=%s category=timeout sampled=True",
             resp.headers["x-request-id"], extra={"category": "timeout"})
