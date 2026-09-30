"""pytest 公共夹具：每个测试使用独立的配置与诊断存储，避免串号。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import configure, reset_config
from app.diagnostics import reset_store
from app.logging_setup import DiagnosticsFilter, setup_logging
from app.main import create_app

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture
def app_instance():
    reset_config()
    reset_store()
    configure(enabled=True, sample_rate=1.0, capacity=100, admin_token=ADMIN_TOKEN)
    application = create_app()
    setup_logging(logging.INFO)
    yield application
    reset_config()
    reset_store()


@pytest.fixture
def client(app_instance):
    with TestClient(app_instance) as c:
        yield c


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _install_record_defaults() -> None:
    """给 pytest 的实时日志处理器补上诊断字段默认值，避免格式化 KeyError。"""
    old_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        # 不能预置 category：调用方通过 extra={"category": ...} 传入时，
        # 预置会触发 logging 的"禁止覆盖"错误；category 由过滤器补齐。
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        if not hasattr(record, "business_key"):
            record.business_key = "-"
        if not hasattr(record, "sampled"):
            record.sampled = False
        return record

    logging.setLogRecordFactory(factory)
    for handler in logging.getLogger().handlers:
        handler.addFilter(DiagnosticsFilter())


_install_record_defaults()
