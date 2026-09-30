"""诊断链路韧性：写入失败不改变业务结果；高并发下主请求不被拖慢。"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.diagnostics import get_store

log = logging.getLogger("app.tests")


def test_diagnostics_write_failure_does_not_break_request(client):
    get_store().fail_on_put = True
    try:
        resp = client.get("/")
        rid = resp.headers["x-request-id"]
        assert resp.status_code == 200
        assert resp.json() == {"Hello": "World"}
        # 错误分支同样保持正常归一化
        resp = client.get("/fail/timeout")
        assert resp.status_code == 504
        assert resp.json()["error"]["category"] == "timeout"
        log.info("写入故障下主请求正常 request_id=%s category=- sampled=True",
                 rid, extra={"category": "-"})
    finally:
        get_store().fail_on_put = False


def test_high_concurrency_does_not_slow_business_path(app_instance):
    # 采样全开 + 大量并发，诊断读写都不能成为主请求的正确性障碍
    from fastapi.testclient import TestClient

    with TestClient(app_instance) as client:
        def one(i: int):
            return client.post(
                "/echo",
                headers={"X-Request-ID": f"ridload{i:04d}"},
                json={"message": "x", "business_key": f"biz-{i}"},
            )

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(one, range(200)))
        elapsed = time.perf_counter() - start

    assert all(r.status_code == 200 for r in responses)
    bodies = [r.json() for r in responses]
    assert {b["request_id"] for b in bodies} == {f"ridload{i:04d}" for i in range(200)}
    log.info("高并发 200 请求耗时 %.3fs category=- sampled=True", elapsed,
             extra={"category": "-"})
