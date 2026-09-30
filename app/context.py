"""请求上下文：请求标识、业务标识的跨同步/异步/后台任务传递。

基于 :mod:`contextvars`：asyncio 会为每个任务复制上下文，因此并发请求
之间互不串号；后台任务通过 ``asyncio 任务`` / ``contextvars.copy_context``
显式继承所属请求的快照。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field

#: 当前请求的请求标识。
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
#: 当前请求的关键业务标识。
business_key_ctx: ContextVar[str] = ContextVar("business_key", default="")
#: 当前请求是否命中采样。
sampled_ctx: ContextVar[bool] = ContextVar("sampled", default=False)
#: 附加元数据（如来源、取消标记等）。
metadata_ctx: ContextVar[dict[str, str]] = ContextVar("request_metadata", default={})


@dataclass
class RequestContext:
    """一次请求的诊断上下文快照（可安全传给后台任务）。"""

    request_id: str
    business_key: str = ""
    sampled: bool = False
    metadata: dict[str, str] = field(default_factory=dict)


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex


def current_request_id() -> str:
    return request_id_ctx.get()


def current_business_key() -> str:
    return business_key_ctx.get()


def is_sampled() -> bool:
    return sampled_ctx.get()


def set_business_key(business_key: str) -> None:
    """在当前请求作用域内更新业务标识（如从请求体解析后调用）。"""
    business_key_ctx.set(business_key)


def current_context() -> RequestContext:    return RequestContext(
        request_id=request_id_ctx.get(),
        business_key=business_key_ctx.get(),
        sampled=sampled_ctx.get(),
        # 复制一份，避免后台任务与请求体共享可变对象
        metadata=dict(metadata_ctx.get()),
    )


class context_scope:
    """上下文作用域：进入时绑定新值，退出时恢复旧值。

    即使嵌套使用或 with 块内抛异常也能正确还原，适合中间件包裹整条
    HTTP 调用链（含异常分支）。
    """

    def __init__(
        self,
        request_id: str,
        *,
        business_key: str = "",
        sampled: bool = False,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self._request_id = request_id
        self._business_key = business_key
        self._sampled = sampled
        self._metadata = dict(metadata or {})
        self._tokens: tuple = ()

    def __enter__(self) -> "context_scope":
        t1 = request_id_ctx.set(self._request_id)
        t2 = business_key_ctx.set(self._business_key)
        t3 = sampled_ctx.set(self._sampled)
        t4 = metadata_ctx.set(self._metadata)
        self._tokens = (t1, t2, t3, t4)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        t1, t2, t3, t4 = self._tokens
        metadata_ctx.reset(t4)
        sampled_ctx.reset(t3)
        business_key_ctx.reset(t2)
        request_id_ctx.reset(t1)
        return None


def bind_context(
    request_id: str,
    *,
    business_key: str = "",
    sampled: bool = False,
    metadata: dict[str, str] | None = None,
) -> context_scope:
    """返回一个已进入的作用域（调用方负责 :meth:`context_scope.__exit__`）。"""
    scope = context_scope(
        request_id, business_key=business_key, sampled=sampled, metadata=metadata
    )
    return scope.__enter__()
