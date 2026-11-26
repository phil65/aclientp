"""InMemoryMessageStateStore keeps only what is in flight."""

from __future__ import annotations

from aclientp import RequestError
from aclientp.task.state import InMemoryMessageStateStore


def test_finished_incoming_requests_are_dropped() -> None:
    store = InMemoryMessageStateStore()
    records = [store.begin_incoming("fs/read_text_file", {"path": f"/{i}"}) for i in range(200)]
    assert len(store.pending_incoming) == 200
    for i, record in enumerate(records):
        if i % 2:
            store.complete_incoming(record, {"content": "x" * 10_000})
        else:
            store.fail_incoming(record, RequestError.internal_error())
    assert store.pending_incoming == []
    assert records[1].status == "completed"
    assert records[0].status == "failed"


def test_pending_incoming_request_is_visible() -> None:
    store = InMemoryMessageStateStore()
    first = store.begin_incoming("a", None)
    second = store.begin_incoming("b", None)
    store.complete_incoming(first, None)
    assert store.pending_incoming == [second]
