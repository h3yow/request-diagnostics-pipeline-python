"""诊断日志：日志行带上请求标识、错误类别与采样结果。

单测或本地排障时，任何一条日志都能直接看到它属于哪个请求、是否命中
采样、错误归入哪一类，例如::

    2026-.. INFO [req=req_ab12.. biz=order-7 sampled=True cat=timeout] ...

注意：日志只记录类别、异常类型名等安全信息，不记录异常原文
（可能含下游地址、参数等敏感内容）。
"""
from __future__ import annotations

import logging

from .context import current_business_key, current_request_id, is_sampled

_FORMAT = (
    "%(asctime)s %(levelname)s "
    "[req=%(request_id)s biz=%(business_key)s sampled=%(sampled)s "
    "cat=%(category)s] %(name)s: %(message)s"
)


class DiagnosticsFilter(logging.Filter):
    """为每条日志注入 request_id / business_key / sampled / category 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        record.business_key = current_business_key() or "-"
        record.sampled = is_sampled()
        record.category = getattr(record, "category", "") or "-"
        return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置 ``app`` 命名空间下的日志（幂等，重复调用不叠加 handler）。"""
    logger = logging.getLogger("app")
    logger.setLevel(level)
    if not any(getattr(h, "_diagnostics_handler", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(DiagnosticsFilter())
        handler._diagnostics_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def diag_logger() -> logging.Logger:
    return logging.getLogger("app.diagnostics")
