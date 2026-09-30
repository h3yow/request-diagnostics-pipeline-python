"""并发隔离测试：高并发下请求标识/业务标识不能串号。"""
from __future__ import annotations

import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed


log = logging.getLogger("app.tests")


def test_concurrent_requests_do_not_mix_context(client):
    n = 80
    ids = [f"ridconcurrent{i:04d}" for i in range(n)]
    keys = [f"biz-{i}" for i in range(n)]
    results: dict[str, dict] = {}
    errors: list[BaseException] = []

    def one(i: int):
        # 让各请求在事件循环里交错
        import time
        time.sleep(random.uniform(0, 0.005))
        resp = client.post(
            "/echo",
            headers={"X-Request-ID": ids[i]},
            json={"message": f"m{i}", "business_key": keys[i]},
        )
        return resp

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(one, i): i for i in range(n)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[ids[i]] = fut.result().json()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    assert not errors
    assert len(results) == n
    for i in range(n):
        body = results[ids[i]]
        assert body["request_id"] == ids[i], f"{ids[i]} 串号成 {body['request_id']}"
        assert body["business_key"] == keys[i]
    log.info("并发隔离 request_id 范围=%s..%s category=- sampled=True 全部无串号",
             ids[0], ids[-1], extra={"category": "-"})


def test_whoami_context_stable_under_concurrency(client):
    n = 40
    ids = [f"ridwhoami{i:04d}" for i in range(n)]

    def one(i: int):
        return client.get("/whoami", headers={"X-Request-ID": ids[i]})

    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(one, range(n)))

    for i, resp in enumerate(responses):
        body = resp.json()
        assert resp.headers["x-request-id"] == ids[i]
        assert body["request_id"] == ids[i]
        assert set(body["observed"]) == {ids[i]}, f"请求 {ids[i]} 读到了别的请求上下文"
    log.info("whoami 并发 request_id 共 %s 个 category=- sampled=True", n,
             extra={"category": "-"})


def test_inbound_request_id_is_validated(client):
    # 非法（含空格/超长）的上游 X-Request-ID 不被采信，改为内部生成
    resp = client.get("/", headers={"X-Request-ID": "bad id with space"})
    rid = resp.headers["x-request-id"]
    assert rid.startswith("req_")
    long_id = "x" * 200
    resp = client.get("/", headers={"X-Request-ID": long_id})
    assert resp.headers["x-request-id"].startswith("req_")
    # 合法标识原样沿用
    resp = client.get("/", headers={"X-Request-ID": "trace_abc-123"})
    assert resp.headers["x-request-id"] == "trace_abc-123"


def test_records_are_isolated_by_request_id(client, admin_headers):
    # 并发完成后逐条查询，记录与请求必须一一对应
    ids = [f"ridrec{i:04d}" for i in range(20)]

    def one(i: int):
        client.get("/items/1", headers={"X-Request-ID": ids[i]})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(20)))

    listing = client.get("/diagnostics/records?limit=200", headers=admin_headers).json()
    seen = {r["request_id"] for r in listing["records"]}
    for rid in ids:
        assert rid in seen
    log.info("记录隔离 category=- sampled=True 命中 %s/20", len(set(ids) & seen),
             extra={"category": "-"})
