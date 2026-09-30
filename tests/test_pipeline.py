"""Record assembly and best-effort persistence tests."""

from diagnostics.context import DiagnosticContext
from diagnostics.errors import ErrorCategory
from diagnostics.pipeline import build_record, safe_persist
from diagnostics.storage import DiagnosticStore
from diagnostics.redaction import MASK


def _ctx() -> DiagnosticContext:
    ctx = DiagnosticContext(request_id="r-1", business_id="b-1", sampled=True,
                            method="POST", path="/pay")
    ctx.error_category = ErrorCategory.DEPENDENCY
    with ctx.span("upstream", token="abc", exception_message="internal-only"):
        pass
    return ctx


def test_build_record_shape_and_span_redaction() -> None:
    record = build_record(_ctx(), status_code=502)
    assert record["request_id"] == "r-1"
    assert record["business_id"] == "b-1"
    assert record["sampled"] is True
    assert record["status_code"] == 502
    assert record["error_category"] == "dependency_error"
    assert record["error_message"] == "downstream dependency error"
    span = record["spans"][0]
    assert span["attrs"]["token"] == MASK
    # Internal exception text is dropped from the queryable record.
    assert "exception_message" not in span["attrs"]


def test_safe_persist_swallows_backend_failure() -> None:
    class Exploding:
        def put(self, record):
            raise RuntimeError("disk gone")

    assert safe_persist(Exploding(), {"request_id": "x"}) is False


def test_safe_persist_success_path() -> None:
    store = DiagnosticStore(5)
    record = build_record(_ctx(), status_code=502)
    assert safe_persist(store, record) is True
    assert store.get("r-1")["error_category"] == "dependency_error"
