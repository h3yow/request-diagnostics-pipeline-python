"""路由可用的依赖注入工具。"""
from __future__ import annotations

from fastapi import Request

from .background import BackgroundJobs


def get_background_jobs(request: Request) -> BackgroundJobs:
    """返回当前请求专属的 :class:`BackgroundJobs`。

    正常情况下由诊断中间件预先放入 ``request.state``；此处兜底创建，
    保证即使中间件未装载，路由也能工作（但任务不会被统一收尾）。
    """
    jobs = getattr(request.state, "background_jobs", None)
    if jobs is None:
        jobs = BackgroundJobs()
        request.state.background_jobs = jobs
    return jobs
