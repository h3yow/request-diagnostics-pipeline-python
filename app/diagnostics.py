"""诊断记录、采样与容量受限存储。

关键点：

* 采样对同一 ``request_id`` 结果确定，边界严格——采样率 0 全部不录、
  1 全部录入；
* 存储有容量上限（环形淘汰最旧记录），避免内存无限增长；
* 所有读写都在锁内快速完成，且写入路径用 try/except 兜底：诊断自身
  的任何异常都不会抛出到业务调用链。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any

from .config import DiagnosticsConfig, get_config

#: 采样分桶数：rate=0.5 即 5000/10000 个桶命中。
_BUCKETS = 10_000


@dataclass
class DiagnosticRecord:
    """一条请求诊断记录。"""

    request_id: str
    started_at: float
    finished_at: float = 0.0
    business_key: str = ""
    path: str = ""
    method: str = ""
    sampled: bool = False
    status_code: int = 0
    category: str = ""
    error_message: str = ""
    background_jobs: int = 0
    background_failures: int = 0
    cancelled: bool = False
    timed_out: bool = False
    duration_ms: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 3),
            "business_key": self.business_key,
            "path": self.path,
            "method": self.method,
            "sampled": self.sampled,
            "status_code": self.status_code,
            "category": self.category,
            "error_message": self.error_message,
            "background_jobs": self.background_jobs,
            "background_failures": self.background_failures,
            "cancelled": self.cancelled,
            "timed_out": self.timed_out,
            "events": list(self.events),
        }


def _bucket(request_id: str) -> int:
    """把 request_id 稳定映射到 [0, _BUCKETS) 的桶。"""
    # 用内置哈希的加盐部分在进程间不稳定，这里采用 FNV-1a，纯算术、确定。
    h = 0xCBF29CE484222325
    for ch in request_id.encode("utf-8"):
        h ^= ch
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h % _BUCKETS


class DiagnosticStore:
    """容量受限、线程安全的诊断存储；写入失败不影响业务。"""

    def __init__(self, config: DiagnosticsConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._records: deque[DiagnosticRecord] = deque(maxlen=max(1, config.capacity))
        self._index: dict[str, DiagnosticRecord] = {}
        #: 故障注入开关：置位后所有写入抛异常，用于验证诊断故障不波及业务
        self.fail_on_put: bool = False

    def should_sample(self, request_id: str) -> bool:
        rate = self._config.sample_rate
        if not self._config.enabled or rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return _bucket(request_id) < int(rate * _BUCKETS)

    def get(self, request_id: str) -> DiagnosticRecord | None:
        try:
            with self._lock:
                rec = self._index.get(request_id)
                return replace(rec) if rec is not None else None
        except Exception:  # 诊断链路异常绝不波及业务
            return None

    def list_recent(self, limit: int = 50) -> list[DiagnosticRecord]:
        try:
            with self._lock:
                if limit >= len(self._records):
                    snapshot = list(self._records)
                else:
                    # deque 不支持负步长切片，取最后 limit 条
                    snapshot = list(self._records)[-limit:]
            return [replace(r) for r in snapshot]
        except Exception:
            return []

    def put(self, record: DiagnosticRecord) -> None:
        try:
            if self.fail_on_put:
                raise RuntimeError("injected diagnostics write failure")
            # 存入副本，避免外部继续修改同一对象导致数据竞争
            stored = replace(record, events=list(record.events))
            with self._lock:
                evicted = self._records[0] if (
                    self._records.maxlen is not None
                    and len(self._records) == self._records.maxlen
                ) else None
                self._records.append(stored)
                self._index[stored.request_id] = stored
                if evicted is not None:
                    self._index.pop(evicted.request_id, None)
        except Exception:
            # 诊断写入失败：静默吞掉，由调用方记录日志即可
            pass

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._index.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


_store: DiagnosticStore | None = None
_store_lock = threading.Lock()


def get_store() -> DiagnosticStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DiagnosticStore(get_config())
    return _store


def configure_store(config: DiagnosticsConfig) -> DiagnosticStore:
    """按给定配置重建存储（测试/本地调试用，会清空旧记录）。"""
    global _store
    with _store_lock:
        _store = DiagnosticStore(config)
    return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        _store = None


def now_ms() -> int:
    return int(time.time() * 1000)
