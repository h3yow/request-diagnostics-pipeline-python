"""采样开关、采样边界与容量上限测试。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import configure, reset_config
from app.diagnostics import configure_store, get_store, reset_store

log = logging.getLogger("app.tests")


@pytest.fixture
def _isolated(app_instance):
    yield app_instance
    reset_config()
    reset_store()


def _drive(client, n: int = 200):
    for i in range(n):
        client.get(f"/items/{i + 1}")


def test_sample_rate_zero_records_nothing(_isolated):
    configure(sample_rate=0.0, capacity=1000)
    configure_store(configure())
    with TestClient(_isolated) as client:
        _drive(client, 100)
    size = len(get_store())
    log.info("采样率=0 request_id=- category=- sampled=False stored=%s", size,
             extra={"category": "-"})
    assert size == 0


def test_sample_rate_one_records_everything(_isolated):
    configure(sample_rate=1.0, capacity=1000)
    configure_store(configure())
    with TestClient(_isolated) as client:
        _drive(client, 100)
    size = len(get_store())
    log.info("采样率=1 category=- sampled=True stored=%s", size,
             extra={"category": "-"})
    assert size == 100


def test_sample_rate_half_around_half_and_deterministic(_isolated):
    configure(sample_rate=0.5, capacity=10000)
    configure_store(configure())
    with TestClient(_isolated) as client:
        _drive(client, 2000)
    first = len(get_store())
    ratio = first / 2000
    log.info("采样率=0.5 category=- sampled=mixed stored=%s ratio=%.3f", first, ratio,
             extra={"category": "-"})
    assert 0.35 <= ratio <= 0.65

    # 同一批 request_id 的采样决策必须确定（重放不会改变结果）
    from app.diagnostics import DiagnosticStore
    store = DiagnosticStore(configure())
    decisions = {f"req_{i}": store.should_sample(f"req_{i}") for i in range(500)}
    again = {f"req_{i}": store.should_sample(f"req_{i}") for i in range(500)}
    assert decisions == again


def test_capacity_is_bounded(_isolated):
    configure(sample_rate=1.0, capacity=50)
    configure_store(configure())
    with TestClient(_isolated) as client:
        _drive(client, 300)
    assert len(get_store()) == 50
    # 最旧的记录被淘汰，最新的仍可查询
    recent = get_store().list_recent(limit=1)[0]
    assert recent.path != ""
    log.info("容量上限 category=- sampled=True stored=%s", len(get_store()),
             extra={"category": "-"})


def test_unsampled_request_still_succeeds(_isolated):
    # 未采样只影响诊断写入，绝不能影响业务响应
    configure(sample_rate=0.0, capacity=100)
    configure_store(configure())
    with TestClient(_isolated) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}
    assert resp.headers["x-request-id"]
