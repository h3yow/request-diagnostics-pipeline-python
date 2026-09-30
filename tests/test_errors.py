"""Error classification and normalization tests."""

import asyncio

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTP

from diagnostics.errors import (
    CATEGORY_STATUS,
    DependencyError,
    DiagnosticError,
    ErrorCategory,
    classify_exception,
    error_response,
    normalize,
)


def test_classifies_common_exception_families() -> None:
    assert classify_exception(ValueError("bad")) == ErrorCategory.VALIDATION
    assert classify_exception(KeyError("f")) == ErrorCategory.VALIDATION
    assert classify_exception(ConnectionError("down")) == ErrorCategory.DEPENDENCY
    assert classify_exception(OSError("io")) == ErrorCategory.DEPENDENCY
    assert classify_exception(asyncio.TimeoutError()) == ErrorCategory.TIMEOUT
    assert classify_exception(RuntimeError("?")) == ErrorCategory.INTERNAL


def test_diagnostic_subtypes_keep_category_and_status() -> None:
    assert DependencyError().category == ErrorCategory.DEPENDENCY
    assert DependencyError().status_code == CATEGORY_STATUS[ErrorCategory.DEPENDENCY]
    for category in (ErrorCategory.VALIDATION, ErrorCategory.TIMEOUT,
                     ErrorCategory.INTERNAL, ErrorCategory.PERMISSION,
                     ErrorCategory.NOT_FOUND, ErrorCategory.DEPENDENCY,
                     ErrorCategory.CANCELLED):
        assert category in CATEGORY_STATUS


def test_cancelled_error_is_distinct_category() -> None:
    async def scenario() -> None:
        try:
            await asyncio.sleep(10)
        except BaseException as exc:
            assert classify_exception(exc) == ErrorCategory.CANCELLED
            raise

    async def runner() -> None:
        task = asyncio.ensure_future(scenario())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(runner())


def test_starlette_status_mapping() -> None:
    assert classify_exception(StarletteHTTP(status_code=400)) == ErrorCategory.VALIDATION
    assert classify_exception(StarletteHTTP(status_code=403)) == ErrorCategory.PERMISSION
    assert classify_exception(StarletteHTTP(status_code=404)) == ErrorCategory.NOT_FOUND
    assert classify_exception(StarletteHTTP(status_code=504)) == ErrorCategory.TIMEOUT
    assert classify_exception(StarletteHTTP(status_code=503)) == ErrorCategory.DEPENDENCY


def test_request_validation_error_normalizes_with_safe_fields() -> None:
    class M(BaseModel):
        x: int

    try:
        M.model_validate({"x": "not-int"})
    except PydanticValidationError as exc:
        request_exc = RequestValidationError(exc.errors())

    category = classify_exception(request_exc)
    assert category == ErrorCategory.VALIDATION
    normalized = normalize(request_exc)
    assert normalized.category == ErrorCategory.VALIDATION
    fields = normalized.details["validation"]
    assert fields and fields[0]["field"] == "x"


def test_public_body_carries_no_internal_detail() -> None:
    normalized = normalize(RuntimeError("db password=supersecret failed"))
    body = error_response(normalized.category, str(normalized), "req-9")
    serialized = str(body)
    assert "supersecret" not in serialized
    assert body["error"] == {
        "category": "internal_error",
        "message": "internal server error",
        "request_id": "req-9",
    }


def test_unknown_category_in_response_falls_back_to_internal() -> None:
    body = error_response("made-up", "x", "r")
    assert body["error"]["category"] == ErrorCategory.INTERNAL


def test_invalid_category_constructor_rejected() -> None:
    with pytest.raises(ValueError):
        DiagnosticError(category="nope")
