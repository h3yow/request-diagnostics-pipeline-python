"""后台任务封装：继承上下文、异常归因、超时/取消收尾。

任务在登记时立即作为 asyncio 任务启动（可与请求处理并发执行），在请求
收尾时由 :class:`~app.middleware.DiagnosticsMiddleware` 统一
:meth:`BackgroundJobs.finalize`：

* 每个任务都在与发起请求相同的 ``request_id / business_key / sampled``
  上下文里执行，复制的是快照，并发请求互不串号；
* 同步函数丢到线程池，仍通过 ``Context.run`` 继承上下文；
* 单个任务超时只记为该任务失败，不影响其它任务与业务响应；
* 请求被取消时，取消所有未完成任务并等待其收尾；
* 后台任务的异常一律被归因并计数，绝不向上抛出改变业务结果。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .context import RequestContext, context_scope, current_context
from .errors import categorize

#: 单个后台任务的默认超时（秒）。
DEFAULT_JOB_TIMEOUT = 5.0
#: 请求取消后等待任务收尾的最长时间。
SHUTDOWN_TIMEOUT = 2.0


@dataclass
class JobResult:
    name: str
    ok: bool
    duration_ms: float
    category: str = ""
    error_type: str = ""
    timed_out: bool = False
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "duration_ms": round(self.duration_ms, 3),
            "category": self.category,
            "error_type": self.error_type,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
        }


async def _run_async(
    ctx: RequestContext, func: Callable[..., Any], args: tuple, kwargs: dict
) -> Any:
    with context_scope(
        ctx.request_id,
        business_key=ctx.business_key,
        sampled=ctx.sampled,
        metadata=ctx.metadata,
    ):
        return await func(*args, **kwargs)


def _run_sync(
    ctx: RequestContext, func: Callable[..., Any], args: tuple, kwargs: dict
) -> Any:
    import contextvars

    def _target() -> Any:
        with context_scope(
            ctx.request_id,
            business_key=ctx.business_key,
            sampled=ctx.sampled,
            metadata=ctx.metadata,
        ):
            return func(*args, **kwargs)

    return contextvars.copy_context().run(_target)


class BackgroundJobs:
    """随请求登记后台任务，并在请求收尾时统一等待与归因。"""

    def __init__(self, ctx: RequestContext | None = None) -> None:
        # 允许显式注入（测试用）；默认在每次 schedule 时取当前上下文快照，
        # 这样路由层从请求体解析出的 business_key 也能被后台任务继承。
        self._explicit_ctx = ctx
        self._tasks: dict[asyncio.Task[Any], tuple[str, float]] = {}
        self._results: list[JobResult] = []
        self._failures = 0

    def schedule(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout: float = DEFAULT_JOB_TIMEOUT,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """登记一个同步或异步后台任务，自动继承当前请求上下文。"""
        ctx = self._explicit_ctx or current_context()
        job_name = name or getattr(func, "__name__", "background_job")
        started = time.perf_counter()
        if inspect.iscoroutinefunction(func):
            coro = _run_async(ctx, func, args, kwargs)
        else:
            coro = asyncio.to_thread(_run_sync, ctx, func, args, kwargs)

        async def _guarded() -> Any:
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except BaseException:
                raise

        task = asyncio.create_task(_guarded(), name=f"bg:{job_name}")
        self._tasks[task] = (job_name, started)

    async def finalize(self) -> None:
        """等待所有后台任务结束；异常/超时/取消只记录，不抛出。"""
        if not self._tasks:
            return
        tasks = list(self._tasks)
        try:
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            # 极少见（gather(return_exceptions=True) 基本不会抛），
            # 走取消收尾路径保证任务不泄漏。
            await self._cancel_all()
            return
        for task, outcome in zip(tasks, gathered):
            job_name, started = self._tasks[task]
            self._record(job_name, started, outcome)

    async def _cancel_all(self) -> None:
        pending = [t for t in self._tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            job_name, started = self._tasks[task]
            self._record(job_name, started, asyncio.CancelledError())

    async def finalize_on_cancel(self) -> None:
        """请求已取消时调用：取消后台任务并做有界等待，避免悬挂。"""
        try:
            await asyncio.wait_for(self._cancel_all(), timeout=SHUTDOWN_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
            # 收尾本身超时或再次被取消都不允许继续扩散
            pass

    def _record(self, job_name: str, started: float, outcome: Any) -> None:
        duration_ms = (time.perf_counter() - started) * 1000
        if isinstance(outcome, BaseException):
            self._failures += 1
            # wait_for 把内部 CancelledError 包成 TimeoutError
            timed_out = isinstance(outcome, TimeoutError | asyncio.TimeoutError)
            cancelled = isinstance(outcome, asyncio.CancelledError) and not timed_out
            category, _ = categorize(outcome)
            result = JobResult(
                name=job_name,
                ok=False,
                duration_ms=duration_ms,
                category=category.value,
                error_type=type(outcome).__name__,
                timed_out=timed_out,
                cancelled=cancelled,
            )
        else:
            result = JobResult(name=job_name, ok=True, duration_ms=duration_ms)
        self._results.append(result)

    @property
    def total(self) -> int:
        return len(self._tasks)

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def results(self) -> list[JobResult]:
        return list(self._results)
