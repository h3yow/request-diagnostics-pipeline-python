"""异常链路归一化测试。

每个断言都用日志打印 request_id、错误类别与采样结果，方便本地定位。
"""
from __future__ import annotations

import logging


log = logging.getLogger("app.tests")


def _assert_error(client, path: str, expected_category: str, expected_status: int):
    resp = client.get(path)
    rid = resp.headers.get("x-request-id", "")
    assert resp.status_code == expected_status
    body = resp.json()
    assert body["error"]["category"] == expected_category
    assert body["error"]["request_id"] == rid
    log.info(
        "错误链路 path=%s request_id=%s category=%s sampled=True status=%s",
        path, rid, expected_category, expected_status,
        extra={"category": expected_category},
    )
    return rid, body


def test_validation_error_category(client):
    rid, body = _assert_error(client, "/items/-3", "validation", 422)
    assert body["error"]["message"] == "请求参数校验失败"
    assert rid.startswith("req_")


def test_pydantic_validation_error_category(client):
    resp = client.post("/echo", json={"message": ""})
    rid = resp.headers["x-request-id"]
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["category"] == "validation"
    assert body["error"]["request_id"] == rid
    # 只暴露字段位置，不回显输入
    assert {"loc": ["body", "message"], "type": "string_too_short"} in body["error"]["fields"]
    log.info("Pydantic 校验 request_id=%s category=validation sampled=True", rid,
             extra={"category": "validation"})


def test_dependency_error_category(client):
    rid, body = _assert_error(client, "/fail/dependency", "dependency", 502)
    # 敏感的下游地址/账号绝不能出现在响应中
    assert "10.0.0.9" not in str(body)
    assert "password" not in str(body)


def test_timeout_error_category(client):
    _assert_error(client, "/fail/timeout", "timeout", 504)


def test_not_found_error_category(client):
    _assert_error(client, "/items/0", "not_found", 404)


def test_internal_error_category(client):
    rid, body = _assert_error(client, "/fail/internal", "internal", 500)
    assert "AKIA" not in str(body)
    assert "session_secret" not in str(body)


def test_diagnostic_record_contains_no_sensitive_detail(client, admin_headers):
    # 即使走受控的诊断查询接口，存储里也只有异常类型名，没有原始敏感信息
    rid, _ = _assert_error(client, "/fail/dependency", "dependency", 502)
    rec = client.get(
        f"/diagnostics/records/{rid}", headers=admin_headers
    ).json()
    assert rec["error_message"] == "DependencyFailure"
    assert "10.0.0.9" not in str(rec)
    assert "password" not in str(rec)
    rid, _ = _assert_error(client, "/fail/internal", "internal", 500)
    rec = client.get(
        f"/diagnostics/records/{rid}", headers=admin_headers
    ).json()
    assert rec["error_message"] == "InternalFailure"
    assert "AKIA" not in str(rec)


def test_success_response_unchanged(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}
    assert resp.headers.get("x-request-id", "").startswith("req_")
    resp = client.get("/items/9")
    assert resp.json() == {"item_id": 9, "name": "item-9"}


def test_diagnostic_record_has_category(client, admin_headers):
    rid, _ = _assert_error(client, "/fail/timeout", "timeout", 504)
    rec = client.get(
        f"/diagnostics/records/{rid}", headers=admin_headers
    ).json()
    assert rec["category"] == "timeout"
    assert rec["timed_out"] is True
    assert rec["status_code"] == 504
    log.info("诊断记录 request_id=%s category=%s sampled=%s",
             rid, rec["category"], rec["sampled"], extra={"category": "timeout"})
