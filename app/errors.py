"""错误分类与归一化。

设计目标：不同来源的异常最终映射到稳定、可区分的
:class:`ErrorCategory`，对外响应只暴露类别、简短信息与 request_id，
绝不回传堆栈、内部依赖细节等敏感内容。
"""
from __future__ import annotations

import asyncio
from enum import Enum

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError


class ErrorCategory(str, Enum):
    """稳定、可区分的错误类别。"""

    VALIDATION = "validation"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    INTERNAL = "internal"


class CategorizedError(Exception):
    """带类别的领域错误基类。"""

    category: ErrorCategory = ErrorCategory.INTERNAL
    http_status: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        category: ErrorCategory | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message or self.category.value
        if category is not None:
            self.category = category
        if status_code is not None:
            self.http_status = status_code


class ValidationFailure(CategorizedError):
    category = ErrorCategory.VALIDATION
    http_status = 422


class DependencyFailure(CategorizedError):
    category = ErrorCategory.DEPENDENCY
    http_status = 502


class TimeoutFailure(CategorizedError):
    category = ErrorCategory.TIMEOUT
    http_status = 504


class NotFoundFailure(CategorizedError):
    category = ErrorCategory.NOT_FOUND
    http_status = 404


class UnauthorizedFailure(CategorizedError):
    category = ErrorCategory.UNAUTHORIZED
    http_status = 401


class InternalFailure(CategorizedError):
    category = ErrorCategory.INTERNAL
    http_status = 500


#: HTTP 状态码 -> 错误类别（用于 HTTPException 归一化）
_STATUS_CATEGORY = {
    400: ErrorCategory.VALIDATION,
    401: ErrorCategory.UNAUTHORIZED,
    403: ErrorCategory.UNAUTHORIZED,
    404: ErrorCategory.NOT_FOUND,
    422: ErrorCategory.VALIDATION,
    502: ErrorCategory.DEPENDENCY,
    503: ErrorCategory.DEPENDENCY,
    504: ErrorCategory.TIMEOUT,
}

#: 对外可见的安全提示语（原始异常信息只进诊断记录，不进普通响应）
_PUBLIC_MESSAGE = {
    ErrorCategory.VALIDATION: "请求参数校验失败",
    ErrorCategory.DEPENDENCY: "下游依赖服务异常",
    ErrorCategory.TIMEOUT: "处理超时",
    ErrorCategory.CANCELLED: "请求已取消",
    ErrorCategory.NOT_FOUND: "资源不存在",
    ErrorCategory.UNAUTHORIZED: "未授权访问",
    ErrorCategory.INTERNAL: "服务内部错误",
}


def categorize(exc: BaseException) -> tuple[ErrorCategory, int]:
    """把任意异常归一化为 ``(错误类别, HTTP 状态码)``。

    判定顺序：取消 -> 超时 -> 领域错误 -> FastAPI/Pydantic 校验错误
    -> HTTPException -> 其它一律内部错误。
    """
    if isinstance(exc, CategorizedError):
        return exc.category, exc.http_status
    if isinstance(exc, asyncio.CancelledError):
        return ErrorCategory.CANCELLED, 499
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return ErrorCategory.TIMEOUT, 504
    if isinstance(exc, RequestValidationError):
        return ErrorCategory.VALIDATION, 422
    if isinstance(exc, HTTPException):
        category = _STATUS_CATEGORY.get(exc.status_code, ErrorCategory.INTERNAL)
        # 只有 5xx 才可能落入 internal；4xx 未映射的也归 internal 不合适，
        # 这里把未知 4xx 归为 validation。
        if category is ErrorCategory.INTERNAL and 400 <= exc.status_code < 500:
            category = ErrorCategory.VALIDATION
        return category, exc.status_code
    return ErrorCategory.INTERNAL, 500


def safe_error_body(category: ErrorCategory, request_id: str) -> dict:
    """构造对外错误体：不含敏感信息，携带 request_id 便于对账。

    成功响应保持业务字段不动；错误响应统一为::

        {"error": {"category": ..., "message": ..., "request_id": ...}}
    """
    return {
        "error": {
            "category": category.value,
            "message": _PUBLIC_MESSAGE[category],
            "request_id": request_id,
        }
    }
