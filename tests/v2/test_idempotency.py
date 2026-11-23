"""Tests for v2 prompt deduplication by ``idempotencyKey`` (upstream #2847)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import MISSING, ValidationError
import pytest

from aclientp.schema.v2 import (
    Implementation,
    InitializeRequest,
    InitializeResponse,
    PromptRequest,
    PromptResponse,
)
from aclientp.v2.agent import Agent, AgentSideConnection
from aclientp.v2.idempotency import (
    IdempotencyBackend,
    IdempotencyStore,
    InMemoryIdempotencyBackend,
)
from tests.streams import TIMEOUT, make_streams


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.streams import Peer


# --- the store ------------------------------------------------------------------


async def test_retry_returns_the_recorded_message() -> None:
    store = IdempotencyStore()
    async with store.claim("s", "k") as claim:
        assert not claim.is_retry
        await claim.record("m1")
    async with store.claim("s", "k") as retry:
        assert retry.is_retry
        assert retry.message_id == "m1"
        with pytest.raises(RuntimeError):
            await retry.record("m2")
    assert await store.lookup("s", "k") == "m1"


async def test_keys_are_scoped_to_the_session_and_case_sensitive() -> None:
    store = IdempotencyStore()
    async with store.claim("s", "k") as claim:
        await claim.record("m1")
    for session, key in (("other", "k"), ("s", "K")):
        async with store.claim(session, key) as claim:
            assert claim.message_id is None


@pytest.mark.parametrize("key", [None, MISSING])
async def test_no_key_never_deduplicates(key: Any) -> None:
    store = IdempotencyStore()
    for message_id in ("m1", "m2"):
        async with store.claim("s", key) as claim:
            assert not claim.is_retry
            await claim.record(message_id)


async def test_failed_attempt_records_nothing() -> None:
    store = IdempotencyStore()
    with pytest.raises(ValueError, match="insert failed"):
        async with store.claim("s", "k"):
            raise ValueError("insert failed")
    async with store.claim("s", "k") as claim:
        assert not claim.is_retry


async def test_concurrent_attempts_insert_once() -> None:
    store = IdempotencyStore()
    inserted: list[str] = []

    async def attempt(n: int) -> str:
        async with store.claim("s", "k") as claim:
            if claim.message_id is not None:
                return claim.message_id
            await asyncio.sleep(0.01)  # the other attempts arrive meanwhile
            inserted.append(f"m{n}")
            await claim.record(f"m{n}")
            return f"m{n}"

    results = await asyncio.gather(*(attempt(n) for n in range(5)))

    assert inserted == ["m0"]
    assert set(results) == {"m0"}
    assert store._locks == {}  # released


async def test_custom_backend_survives_a_new_store() -> None:
    class DictBackend:  # e.g. the agent's session storage
        def __init__(self, data: dict[tuple[str, str], str]) -> None:
            self.data = data

        async def get(self, session_id: str, key: str) -> str | None:
            return self.data.get((session_id, key))

        async def put(self, session_id: str, key: str, message_id: str) -> None:
            self.data[session_id, key] = message_id

    persisted: dict[tuple[str, str], str] = {}
    assert isinstance(DictBackend(persisted), IdempotencyBackend)
    async with IdempotencyStore(DictBackend(persisted)).claim("s", "k") as claim:
        await claim.record("m1")

    restarted = IdempotencyStore(DictBackend(persisted))

    assert await restarted.lookup("s", "k") == "m1"


async def test_in_memory_backend_forgets_a_session() -> None:
    backend = InMemoryIdempotencyBackend()
    await backend.put("s", "k", "m1")
    backend.forget_session("s")
    assert await backend.get("s", "k") is None


# --- key validation -------------------------------------------------------------


@pytest.mark.parametrize("key", ["", "x" * 256, 42, ["k"]])
def test_invalid_keys_fail_validation(key: object) -> None:
    with pytest.raises(ValidationError):
        PromptRequest.model_validate({
            "sessionId": "s",
            "prompt": [{"type": "text", "text": "hi"}],
            "idempotencyKey": key,
        })


@pytest.mark.parametrize("key", ["k", "x" * 255, "é" * 255, None])
def test_valid_keys(key: str | None) -> None:
    request = PromptRequest.model_validate({
        "sessionId": "s",
        "prompt": [{"type": "text", "text": "hi"}],
        "idempotencyKey": key,
    })
    assert request.idempotency_key == key


# --- over a connection ----------------------------------------------------------


class DedupAgent(Agent):
    """Inserts one message per key, like an agent advertising ``capabilities.session``."""

    store: ClassVar[IdempotencyStore] = IdempotencyStore()
    turns: ClassVar[list[str]] = []

    def __init__(self, conn: AgentSideConnection) -> None:
        self.conn = conn

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        return InitializeResponse(protocol_version=2, info=Implementation(name="a", version="1"))

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        async with self.store.claim(params.session_id, params.idempotency_key) as claim:
            if claim.message_id is not None:
                return PromptResponse(message_id=claim.message_id)
            message_id = f"m{len(self.turns)}"
            await claim.record(message_id)
        self.turns.append(message_id)
        return PromptResponse(message_id=message_id)


@pytest.fixture
async def peer() -> AsyncIterator[Peer]:
    DedupAgent.store = IdempotencyStore()
    DedupAgent.turns = []
    writer, reader, peer = make_streams()
    conn = AgentSideConnection(DedupAgent, writer, reader)
    params = {"protocolVersion": 2, "info": {"name": "c", "version": "1"}}
    await peer.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": params})
    await asyncio.wait_for(peer.receive(), TIMEOUT)
    yield peer
    await conn.close()


async def _prompt(peer: Peer, request_id: int, key: object = MISSING) -> dict[str, Any]:
    params: dict[str, Any] = {"sessionId": "s", "prompt": [{"type": "text", "text": "hi"}]}
    if key is not MISSING:
        params["idempotencyKey"] = key
    await peer.send({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/prompt",
        "params": params,
    })
    reply: dict[str, Any] = await asyncio.wait_for(peer.receive(), TIMEOUT)
    assert reply["id"] == request_id
    return reply


async def test_retry_over_the_wire_returns_the_original_message(peer: Peer) -> None:
    first = await _prompt(peer, 1, "key-1")
    retry = await _prompt(peer, 2, "key-1")
    other = await _prompt(peer, 3, "key-2")

    assert first["result"] == retry["result"] == {"messageId": "m0"}
    assert other["result"] == {"messageId": "m1"}
    assert DedupAgent.turns == ["m0", "m1"]


@pytest.mark.parametrize("key", ["", "x" * 256, 7])
async def test_invalid_key_over_the_wire_is_invalid_params(peer: Peer, key: object) -> None:
    reply = await _prompt(peer, 1, key)

    assert reply["error"]["code"] == -32602
    assert DedupAgent.turns == []
