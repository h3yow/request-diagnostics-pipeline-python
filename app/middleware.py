"""诊断 ASGI 中间件：贯穿同步/异步/后台任务/异常分支。

执行顺序与职责：

1. 为每个 HTTP 请求生成（或沿用上游 ``X-Request-ID``）请求标识，
   建立 :mod:`contextvars` 作用域并完成采样决策；
2. 把本请求专属的 :class:`~app.background.BackgroundJobs` 放入
   ``request.state``，供路由依赖注入；
3. 包裹下游应用，捕获一切异常并经 :mod:`app.errors` 归一化后返回
   统一、脱敏的错误响应；
4. 响应发出后等待后台任务收尾（取消/超时只记录），再写入容量受限的
   诊断存储；任何诊断环节异常都不会改变业务结果。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .background import BackgroundJobs
from .config import get_config
from .context import context_scope, current_business_key, new_request_id
from .diagnostics import DiagnosticRecord, get_store
from .errors import ErrorCategory, categorize, safe_error_body

#: 错误类别经响应头从异常处理器回传给中间件。
CATEGORY_HEADER = b"x-diagnostics-category"
REQUEST_ID_HEADER = "x-request-id"

logger = logging.getLogger("app.diagnostics")


def _extract_request_id(headers: Headers) -> str:
    rid = headers.get(REQUEST_ID_HEADER, "").strip()
    # 只接受简单、有限长度的标识，避免把任意头值带进日志与存储
    if rid and len(rid) <= 128 and all(c.isalnum() or c in "-_" for c in rid):
        return rid
    return new_request_id()


class DiagnosticsMiddleware:
    """为每个请求建立上下文、采样、异常归一化与诊断记录收尾。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = _extract_request_id(headers)
        config = get_config()
        store = get_store()
        sampled = store.should_sample(request_id) if config.enabled else False

        started_wall = time.time()
        started_perf = time.perf_counter()
        record = DiagnosticRecord(
            request_id=request_id,
            started_at=started_wall,
            path=scope.get("path", ""),
            method=scope.get("method", ""),
            sampled=sampled,
        )
        jobs = BackgroundJobs()

        status_holder: dict[str, Any] = {"status": 200, "category": ""}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 200)
                raw_headers = message.get("headers", [])
                # 响应头已确定后追加请求标识；类别由异常处理器通过专用头回传
                raw_headers.append(
                    (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))
                )
                for name, value in raw_headers:
                    if name.lower() == CATEGORY_HEADER:
                        status_holder["category"] = value.decode("latin-1")
                message["headers"] = raw_headers
            await send(message)

        async def _finish(cancelled: bool = False) -> None:
            # 后台任务先收尾（请求体已经发出），其异常不影响主请求
            try:
                if cancelled:
                    await jobs.finalize_on_cancel()
                else:
                    await jobs.finalize()
            except Exception:  # pragma: no cover - finalize 自身不应抛出
                logger.debug("background finalize failed", exc_info=True)

            record.finished_at = time.time()
            record.duration_ms = (time.perf_counter() - started_perf) * 1000
            record.business_key = current_business_key()
            record.status_code = status_holder["status"]
            record.category = status_holder["category"]
            record.background_jobs = jobs.total
            record.background_failures = jobs.failures
            record.cancelled = cancelled
            record.timed_out = record.category == ErrorCategory.TIMEOUT.value
            record.events = [r.to_dict() for r in jobs.results]

            if sampled:
                store.put(record)
            logger.info(
                "request finished status=%s category=%s sampled=%s jobs=%s/%s",
                record.status_code,
                record.category or "-",
                sampled,
                record.background_failures,
                record.background_jobs,
                extra={"category": record.category or "-"},
            )

        with context_scope(
            request_id,
            sampled=sampled,
            metadata={"path": record.path, "method": record.method},
        ):
            # Starlette 会把 scope['state'] 这个 dict 包装成 State 对象，
            # 因此这里必须以键赋值，路由侧通过 request.state.xxx 读取。
            state = scope.setdefault("state", {})
            state["background_jobs"] = jobs
            state["diagnostics_record"] = record
            try:
                await self.app(scope, receive, send_wrapper)
            except asyncio.CancelledError:
                # 客户端断开或服务端关停：取消后台任务并做有界收尾后重新抛出，
                # 保持 ASGI 对取消语义的正确处理。
                category, status = ErrorCategory.CANCELLED, 499
                status_holder["status"] = status
                status_holder["category"] = category.value
                record.error_message = "client disconnected / request cancelled"
                await _finish(cancelled=True)
                raise
            except Exception as exc:
                category, status = categorize(exc)
                logger.info(
                    "exception normalized type=%s category=%s",
                    type(exc).__name__,
                    category.value,
                    extra={"category": category.value},
                )
                body = safe_error_body(category, request_id)
                # 只记录异常类型名，绝不把可能含下游地址/凭据的原始信息
                # 写入诊断存储或日志。
                record.error_message = type(exc).__name__
                response = JSONResponse(
                    status_code=status,
                    content=body,
                    headers={
                        REQUEST_ID_HEADER: request_id,
                        CATEGORY_HEADER.decode(): category.value,
                    },
                )
                status_holder["status"] = status
                status_holder["category"] = category.value
                await response(scope, receive, send)
                await _finish()
            else:
                await _finish()
