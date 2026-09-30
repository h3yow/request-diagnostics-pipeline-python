"""Diagnostics query authorization tests."""

from diagnostics.auth import authorize


def test_missing_token_denied() -> None:
    decision = authorize({}, "secret")
    assert not decision.allowed
    assert decision.reason == "missing diagnostics token"


def test_wrong_token_denied_and_not_echoed() -> None:
    decision = authorize({"X-Diagnostics-Token": "wrong-value"}, "secret")
    assert not decision.allowed
    assert decision.reason == "invalid diagnostics token"
    assert "wrong-value" not in decision.reason
    assert "secret" not in decision.reason


def test_correct_token_allowed_via_header_and_bearer() -> None:
    assert authorize({"X-Diagnostics-Token": "secret"}, "secret").allowed
    assert authorize({"Authorization": "Bearer secret"}, "secret").allowed
    assert authorize({"authorization": "bearer secret"}, "secret").allowed


def test_empty_configured_token_fails_closed() -> None:
    decision = authorize({"X-Diagnostics-Token": "anything"}, "")
    assert not decision.allowed
    assert "not configured" in decision.reason


def test_header_lookup_is_case_insensitive() -> None:
    assert authorize({"x-diagnostics-token": "secret"}, "secret").allowed
