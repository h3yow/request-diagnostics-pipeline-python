"""诊断能力配置。

可通过代码 :func:`configure` 或环境变量覆盖：

``DIAGNOSTICS_ENABLED``         总开关（1/true/yes/on 为真）
``DIAGNOSTICS_SAMPLE_RATE``     采样率，0.0~1.0
``DIAGNOSTICS_CAPACITY``        内存中保留的诊断记录上限（正整数）
``DIAGNOSTICS_ADMIN_TOKEN``     查询诊断数据所需令牌（为空则拒绝一切查询）
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE


@dataclass(frozen=True)
class DiagnosticsConfig:
    """诊断开关、采样、容量与权限相关配置。"""

    enabled: bool = True
    sample_rate: float = 1.0
    capacity: int = 1000
    admin_token: str = "local-admin-token"

    def __post_init__(self) -> None:
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError("sample_rate 必须在 0.0~1.0 之间")
        if self.capacity <= 0:
            raise ValueError("capacity 必须为正整数")


_lock = threading.Lock()
_config: DiagnosticsConfig | None = None


def _from_env() -> DiagnosticsConfig:
    rate_raw = os.environ.get("DIAGNOSTICS_SAMPLE_RATE")
    cap_raw = os.environ.get("DIAGNOSTICS_CAPACITY")
    rate = float(rate_raw) if rate_raw not in (None, "") else 1.0
    cap = int(cap_raw) if cap_raw not in (None, "") else 1000
    return DiagnosticsConfig(
        enabled=_as_bool(os.environ.get("DIAGNOSTICS_ENABLED"), True),
        sample_rate=rate,
        capacity=cap,
        admin_token=os.environ.get("DIAGNOSTICS_ADMIN_TOKEN", "local-admin-token"),
    )


def get_config() -> DiagnosticsConfig:
    """获取当前生效配置（惰性初始化，首次调用读取环境变量）。"""
    global _config
    if _config is None:
        with _lock:
            if _config is None:
                _config = _from_env()
    return _config


def configure(
    *,
    enabled: bool | None = None,
    sample_rate: float | None = None,
    capacity: int | None = None,
    admin_token: str | None = None,
) -> DiagnosticsConfig:
    """在当前默认值/环境值基础上覆盖部分字段，主要用于测试与本地调试。"""
    global _config
    base = get_config()
    _config = DiagnosticsConfig(
        enabled=base.enabled if enabled is None else enabled,
        sample_rate=base.sample_rate if sample_rate is None else sample_rate,
        capacity=base.capacity if capacity is None else capacity,
        admin_token=base.admin_token if admin_token is None else admin_token,
    )
    return _config


def reset_config() -> None:
    """清空单例，强制下次重新读取环境变量（测试用）。"""
    global _config
    with _lock:
        _config = None
