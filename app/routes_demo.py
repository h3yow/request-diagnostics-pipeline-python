"""演示用业务路由：覆盖成功、参数校验、依赖异常、超时、内部错误与后台任务。

成功路径保持简单稳定的 JSON 字段；错误路径抛出
:mod:`app.errors` 中的领域异常，由中间件统一归一化。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .background import BackgroundJobs
from .context import current_business_key, current_request_id, set_business_key
from .deps import get_background_jobs
from .errors import (
    DependencyFailure,
    InternalFailure,
    NotFoundFailure,
    TimeoutFailure,
    ValidationFailure,
)

router = APIRouter()


class EchoPayload(BaseModel):
    message: str = Field(min_length=1, max_length=200)
    business_key: str = Field(default="", max_length=64)


async def _sync_order_system() -> None:
    """模拟一个会失败且报错信息含敏感细节的下游依赖。"""
    raise DependencyFailure(
        "orders-db at 10.0.0.9:5432 password rejected for user 'app_rw'"
    )


async def _slow_dependency(delay: float) -> str:
    await asyncio.sleep(delay)
    return "done"


def _bookkeeping(business_key: str) -> str:
    """同步后台工作：在线程池里执行，也能读到请求上下文。"""
    rid = current_request_id()
    return f"bookkeeping for {business_key} under {rid}"


async def _async_hook(business_key: str) -> str:
    await asyncio.sleep(0.01)
    rid = current_request_id()
    return f"hook for {business_key} under {rid}"


@router.get("/")
def read_root() -> dict:
    """保持与原骨架完全一致的成功响应。"""
    return {"Hello": "World"}


@router.post("/echo")
async def echo(
    payload: EchoPayload,
    jobs: BackgroundJobs = Depends(get_background_jobs),
) -> dict:
    if payload.business_key:
        # 业务标识从请求体解析出来后写回上下文，后续日志/后台任务都能拿到
        set_business_key(payload.business_key)
    jobs.schedule(_bookkeeping, payload.business_key or "anonymous")
    return {
        "message": payload.message,
        "business_key": payload.business_key,
        "request_id": current_request_id(),
    }


@router.get("/items/{item_id}")
def get_item(item_id: int) -> dict:
    """item_id<0 触发参数校验失败；0 触发未找到；其余成功。"""
    if item_id < 0:
        raise ValidationFailure("item_id 不能为负数")
    if item_id == 0:
        raise NotFoundFailure("item 0 不存在")
    return {"item_id": item_id, "name": f"item-{item_id}"}


@router.get("/fail/dependency")
async def fail_dependency() -> dict:
    await _sync_order_system()
    return {"unreachable": True}


@router.get("/fail/timeout")
async def fail_timeout() -> dict:
    # 用 wait_for 模拟依赖超时；抛 TimeoutError 会被归一化为 timeout/504
    try:
        await asyncio.wait_for(_slow_dependency(2.0), timeout=0.05)
    except (TimeoutError, asyncio.TimeoutError):
        raise TimeoutFailure("downstream orders query timed out")
    return {"unreachable": True}


@router.get("/fail/internal")
def fail_internal() -> dict:
    # 内部错误只对外暴露类别；原始信息（这里故意含敏感串）不进响应
    raise InternalFailure("KeyError: 'session_secret' = 'AKIA-FAKE-SECRET'")


@router.post("/background")
async def spawn_background(
    payload: EchoPayload,
    jobs: BackgroundJobs = Depends(get_background_jobs),
) -> dict:
    if payload.business_key:
        set_business_key(payload.business_key)
    jobs.schedule(_bookkeeping, payload.business_key or "anonymous")
    jobs.schedule(_async_hook, payload.business_key or "anonymous")
    return {
        "accepted": True,
        "business_key": payload.business_key,
        "request_id": current_request_id(),
    }


@router.get("/whoami")
async def whoami() -> dict:
    """在若干 await 点后回读上下文，用于并发隔离测试：

    若上下文在并发请求间串号，返回值里的 request_id 会与响应头不一致。
    """
    observed: list[str] = []
    for _ in range(5):
        await asyncio.sleep(0)
        observed.append(current_request_id())
    return {
        "request_id": current_request_id(),
        "observed": observed,
        "business_key": current_business_key(),
    }


@router.get("/wait-disconnect")
async def wait_disconnect(request: Request) -> dict:
    """阻塞直到客户端断开，用于验证取消路径的后台任务收尾。"""
    while True:
        if await request.is_disconnected():
            raise asyncio.CancelledError()
        await asyncio.sleep(0.01)
