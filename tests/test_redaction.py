"""Sensitive-data redaction tests."""

from diagnostics.redaction import MASK, redact, redact_text


def test_masks_sensitive_keys_at_every_nesting_level() -> None:
    payload = {
        "user": "alice",
        "password": "hunter2",
        "headers": {"Authorization": "Bearer abc.def", "X-Api-Key": "kkk"},
        "items": [{"token": "t1"}, {"ok": 1}],
    }
    out = redact(payload)
    assert out["user"] == "alice"
    assert out["password"] == MASK
    assert out["headers"]["Authorization"] == MASK
    assert out["headers"]["X-Api-Key"] == MASK
    assert out["items"][0]["token"] == MASK
    assert out["items"][1] == {"ok": 1}


def test_masks_secret_patterns_inside_text() -> None:
    text = "call failed; Authorization: Bearer eyJhbGciOi.JWTSIG trailing"
    out = redact_text(text)
    assert "eyJhbGciOi.JWTSIG" not in out
    assert MASK in out
    assert "trailing" in out
    assert redact_text("token=abc12345; retried") == f"token={MASK}; retried"


def test_unknown_types_render_as_marker_without_repr_leak() -> None:
    class Secret:
        def __repr__(self) -> str:
            return "Secret(password=leaked)"

    assert redact(Secret()) == "<Secret>"
    assert "leaked" not in str(redact({"obj": Secret()}))


def test_scalars_pass_through() -> None:
    assert redact(1) == 1
    assert redact(True) is True
    assert redact(None) is None
    assert redact("plain") == "plain"


def test_cyclic_structures_are_depth_limited() -> None:
    a: dict = {"password": "x"}
    a["self"] = a
    out = redact(a)
    assert out["password"] == MASK
    assert isinstance(out, dict)
