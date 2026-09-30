"""诊断查询的开关与权限判定测试。"""
from __future__ import annotations

import logging

log = logging.getLogger("app.tests")


def test_diagnostics_requires_token(client):
    resp = client.get("/diagnostics/records")
    assert resp.status_code == 403
    rid = resp.headers["x-request-id"]
    assert resp.json()["error"]["category"] == "unauthorized"
    log.info("无令牌拒绝 request_id=%s category=unauthorized sampled=True", rid,
             extra={"category": "unauthorized"})


def test_diagnostics_wrong_token(client):
    resp = client.get("/diagnostics/records", headers={"X-Diagnostics-Token": "nope"})
    assert resp.status_code == 403
    resp = client.get(
        "/diagnostics/records", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 403
    log.info("错误令牌拒绝 category=unauthorized sampled=True",
             extra={"category": "unauthorized"})


def test_diagnostics_accepts_bearer_and_header(client, admin_headers):
    resp = client.get("/diagnostics/records", headers=admin_headers)
    assert resp.status_code == 200
    resp = client.get(
        "/diagnostics/records", headers={"X-Diagnostics-Token": "test-admin-token"}
    )
    assert resp.status_code == 200


def test_single_record_missing_is_404(client, admin_headers):
    resp = client.get("/diagnostics/records/req_unknown", headers=admin_headers)
    assert resp.status_code == 404


def test_diagnostics_disabled_returns_404(app_instance):
    from app.config import configure
    from app.diagnostics import configure_store
    from fastapi.testclient import TestClient

    configure(enabled=False)
    configure_store(configure())  # 以关闭后的配置重建存储
    with TestClient(app_instance) as client:
        # 关闭时对所有人都表现为 404，包括带了正确令牌的请求
        resp = client.get(
            "/diagnostics/records",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        assert resp.status_code == 404
        log.info("诊断关闭 category=unauthorized sampled=False",
                 extra={"category": "unauthorized"})


def test_diagnostics_disabled_does_not_record(app_instance):
    from app.config import configure
    from app.diagnostics import configure_store, get_store
    from fastapi.testclient import TestClient

    configure(enabled=False)
    configure_store(configure())
    with TestClient(app_instance) as client:
        client.get("/")
        assert len(get_store()) == 0


def test_empty_admin_token_denies_all(app_instance):
    from app.config import configure
    from app.diagnostics import configure_store
    from fastapi.testclient import TestClient

    configure(enabled=True, admin_token="")
    configure_store(configure())
    with TestClient(app_instance) as client:
        resp = client.get(
            "/diagnostics/records",
            headers={"Authorization": "Bearer "},
        )
        assert resp.status_code == 403
