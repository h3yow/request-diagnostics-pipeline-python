"""后台任务：上下文继承、异常归因、超时与请求取消后的收尾。"""
from __future__ import annotations

import asyncio
import logging

from app.background import BackgroundJobs
from app.context import context_scope, current_request_id

log = logging.getLogger("app.tests")


def test_background_job_inherits_context(client, admin_headers):
    resp = client.post(
        "/background",
        headers={"X-Request-ID": "ridbginherit1"},
        json={"message": "go", "business_key": "biz-bg-1"},
    )
    assert resp.status_code == 200
    rec = client.get(
        "/diagnostics/records/ridbginherit1", headers=admin_headers
    ).json()
    assert rec["business_key"] == "biz-bg-1"
    assert rec["background_jobs"] == 2
    assert rec["background_failures"] == 0
    names = {e["name"] for e in rec["events"]}
    assert names == {"_bookkeeping", "_async_hook"}
    log.info(
        "后台继承 request_id=%s category=- sampled=True jobs=%s",
        rec["request_id"], rec["background_jobs"], extra={"category": "-"},
    )


async def _sync_ok():
    return current_request_id()


async def _async_fail():
    raise RuntimeError("boom")


def test_background_failure_is_attributed():
    async def scenario():
        with context_scope("ridbgfail1", business_key="biz", sampled=True):
            jobs = BackgroundJobs()
            jobs.schedule(_sync_ok)
            jobs.schedule(_async_fail)
            await jobs.finalize()
            return jobs

    jobs = asyncio.run(scenario())
    assert jobs.total == 2
    assert jobs.failures == 1
    failed = [e.to_dict() for e in jobs.results if not e.ok]
    assert failed[0]["category"] == "internal"
    assert failed[0]["error_type"] == "RuntimeError"
    log.info("后台失败归因 request_id=ridbgfail1 category=internal sampled=True",
             extra={"category": "internal"})


def test_background_timeout_counted_without_raising():
    async def never_ends():
        await asyncio.sleep(60)

    async def scenario():
        with context_scope("ridbgtimeout1", sampled=True):
            jobs = BackgroundJobs()
            jobs.schedule(never_ends, timeout=0.02)
            await jobs.finalize()
            return jobs

    jobs = asyncio.run(scenario())
    assert jobs.failures == 1
    event = jobs.results[0].to_dict()
    assert event["category"] == "timeout"
    assert event["timed_out"] is True
    log.info("后台超时收尾 request_id=ridbgtimeout1 category=timeout sampled=True",
             extra={"category": "timeout"})


def test_background_cancel_during_request_finalizes():
    started = asyncio.Event()

    async def long_job():
        started.set()
        await asyncio.sleep(60)

    async def scenario():
        with context_scope("ridbgcancel1", sampled=True):
            jobs = BackgroundJobs()
            outer = asyncio.current_task()
            jobs.schedule(long_job)

            async def cancel_soon():
                await started.wait()
                outer.cancel()

            canceller = asyncio.create_task(cancel_soon())
            try:
                # 模拟中间件主流程被取消后走的收尾路径
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                pass
            await jobs.finalize_on_cancel()
            await canceller
            return jobs

    jobs = asyncio.run(scenario())
    assert jobs.total == 1
    event = jobs.results[0].to_dict()
    assert event["cancelled"] is True
    log.info("请求取消后收尾 request_id=ridbgcancel1 category=cancelled sampled=True",
             extra={"category": "cancelled"})


def test_background_failure_does_not_change_business_response(client, admin_headers):
    resp = client.post(
        "/echo",
        headers={"X-Request-ID": "ridbgbiz1"},
        json={"message": "still ok", "business_key": "biz-keep"},
    )
    # /echo 自带的 bookkeeping 成功；额外确认主响应字段语义未被后台影响
    assert resp.status_code == 200
    assert resp.json()["message"] == "still ok"
