"""Bounded store tests."""

import threading

import pytest

from diagnostics.storage import DiagnosticStore


def test_capacity_evicts_oldest_and_keeps_index_consistent() -> None:
    store = DiagnosticStore(3)
    for i in range(6):
        store.put({"request_id": f"r{i}", "n": i})
    assert len(store) == 3
    assert store.dropped == 3
    assert store.get("r0") is None
    assert store.get("r2") is None
    latest = store.list_recent()
    assert [r["n"] for r in latest] == [3, 4, 5]
    assert {r["request_id"] for r in latest} == set(store._index)


def test_multiple_records_same_id_are_all_kept_and_grouped_on_get() -> None:
    store = DiagnosticStore(12)
    store.put({"request_id": "a", "phase": "request", "v": 1})
    store.put({"request_id": "a", "phase": "background", "task": "t1", "v": 2})
    store.put({"request_id": "b", "phase": "request", "v": 1})
    assert len(store) == 3
    primary = store.get("a")
    assert primary["phase"] == "request"
    assert [r["task"] for r in primary["related"]] == ["t1"]
    # Fill 10 more slots (capacity 12, 3 already used): the oldest entry -
    # the "a" request - is evicted while the newer "a" background survives.
    for i in range(10):
        store.put({"request_id": f"e{i}"})
    assert len(store) == 12
    remaining = store.get("a")
    assert remaining is not None and remaining["phase"] == "background"


def test_failing_writes_never_raise() -> None:
    store = DiagnosticStore(2)
    store.fail_writes = True
    for i in range(10):
        store.put({"request_id": f"r{i}"})  # must not raise
    assert len(store) == 0


def test_list_respects_limit() -> None:
    store = DiagnosticStore(10)
    for i in range(5):
        store.put({"request_id": f"r{i}"})
    assert len(store.list_recent(2)) == 2
    assert store.list_recent(0) == []
    assert len(store.list_recent(100)) == 5


def test_concurrent_puts_stay_within_capacity() -> None:
    store = DiagnosticStore(50)

    def writer(base: int) -> None:
        for i in range(100):
            store.put({"request_id": f"t{base}-{i}"})

    threads = [threading.Thread(target=writer, args=(b,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store) <= 50
    assert {r["request_id"] for r in store.list_recent(50)} == set(store._index)


def test_invalid_capacity_rejected() -> None:
    with pytest.raises(ValueError):
        DiagnosticStore(0)
