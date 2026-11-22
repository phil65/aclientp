"""``serve()`` with a v2 agent routes each connection on ``initialize``."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from aclientp.agent.implementations.testing import TestAgent as V1TestAgent
from aclientp.transports import StreamTransport, serve
from tests.streams import TIMEOUT, make_streams
from tests.v2.test_runtime import EchoAgent


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine
    from pathlib import Path

    from tests.streams import Peer


def _init(version: int) -> dict[str, Any]:
    params = {"protocolVersion": version, "clientCapabilities": {}}
    if version >= 2:
        params = {"protocolVersion": version, "info": {"name": "c", "version": "1"}}
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


async def _serving(run: Coroutine[Any, Any, None], shutdown: asyncio.Event) -> AsyncIterator[None]:
    task = asyncio.create_task(run)
    yield
    shutdown.set()
    await asyncio.wait_for(task, TIMEOUT)


async def _initialize(peer: Peer, version: int) -> dict[str, Any]:
    await peer.send(_init(version))
    reply: dict[str, Any] = await asyncio.wait_for(peer.receive(), TIMEOUT)
    return reply


async def _serve_and_initialize(
    version: int, *, v1: bool, v2: bool, debug_file: str | None = None
) -> dict[str, Any]:
    writer, reader, peer = make_streams()
    shutdown = asyncio.Event()
    run = serve(
        V1TestAgent() if v1 else None,
        StreamTransport(reader=reader, writer=writer),
        v2_agent=EchoAgent if v2 else None,
        shutdown_event=shutdown,
        debug_file=debug_file,
    )
    async for _ in _serving(run, shutdown):
        return await _initialize(peer, version)
    msg = "unreachable"
    raise AssertionError(msg)


async def test_v1_only_serves_v1() -> None:
    reply = await _serve_and_initialize(1, v1=True, v2=False)
    assert reply["result"]["protocolVersion"] == 1


async def test_v1_only_answers_a_v2_client_with_v1() -> None:
    """Without a v2 agent nothing changes: the v1 agent answers (and negotiates)."""
    reply = await _serve_and_initialize(2, v1=True, v2=False)
    assert reply["result"]["agentInfo"]["name"] == "test-agent"  # The v1 agent answered


async def test_routed_v2_reply_is_a_v2_initialize_response() -> None:
    reply = await _serve_and_initialize(2, v1=True, v2=True)
    assert "agentInfo" not in reply["result"]
    assert reply["result"]["info"]["name"] == "test"  # The v2 agent answered


async def test_routed_serves_v2_to_a_v2_client() -> None:
    reply = await _serve_and_initialize(2, v1=True, v2=True)
    assert reply["result"]["protocolVersion"] == 2


async def test_routed_serves_v1_to_a_v1_client() -> None:
    reply = await _serve_and_initialize(1, v1=True, v2=True)
    assert reply["result"]["protocolVersion"] == 1


async def test_v2_only_rejects_a_v1_client() -> None:
    reply = await _serve_and_initialize(1, v1=False, v2=True)
    assert reply["error"]["code"] == -32022


async def test_routed_connection_writes_the_debug_file(tmp_path: Path) -> None:
    debug_file = tmp_path / "acp-debug.jsonl"
    await _serve_and_initialize(2, v1=True, v2=True, debug_file=str(debug_file))
    methods = [json.loads(line).get("method") for line in debug_file.read_text().splitlines()]
    assert "initialize" in methods


async def test_serve_needs_an_agent() -> None:
    writer, reader, _ = make_streams()
    try:
        await serve(None, StreamTransport(reader=reader, writer=writer))
    except ValueError:
        return
    msg = "serve() accepted no agent"
    raise AssertionError(msg)
