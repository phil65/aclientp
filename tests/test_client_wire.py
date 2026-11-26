"""Wire shape of the requests `ClientSideConnection` sends.

Required fields that have a default in the model (``mcpServers: []``, an MCP
server's ``type`` discriminator) must stay on the wire.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from aclientp.client.connection import ClientSideConnection
from aclientp.client.implementations.noop_client import NoOpClient
from aclientp.schema import (
    ForkSessionRequest,
    HttpMcpServer,
    LoadSessionRequest,
    NewSessionRequest,
    ResumeSessionRequest,
)
from tests.streams import TIMEOUT, Peer, make_streams


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from pydantic import BaseModel


@pytest.fixture
async def pair() -> AsyncIterator[tuple[ClientSideConnection, Peer]]:
    writer, reader, peer = make_streams()
    conn = ClientSideConnection(NoOpClient(), writer, reader)
    yield conn, peer
    await conn.close()


async def _wire_params(
    peer: Peer, call: Callable[[], Awaitable[Any]], result: dict[str, Any]
) -> dict[str, Any]:
    """Run `call`, answer its request with `result` and return the params sent."""
    task = asyncio.create_task(call())
    request = await peer.receive()
    await peer.send({"jsonrpc": "2.0", "id": request["id"], "result": result})
    await asyncio.wait_for(task, TIMEOUT)
    return request["params"]


SESSION_RESULT = {"sessionId": "s1"}


@pytest.mark.parametrize(
    ("method", "params", "result"),
    [
        ("new_session", NewSessionRequest(cwd="/w"), SESSION_RESULT),
        ("load_session", LoadSessionRequest(cwd="/w", session_id="s1"), {}),
        ("resume_session", ResumeSessionRequest(cwd="/w", session_id="s1"), {}),
        ("fork_session", ForkSessionRequest(cwd="/w", session_id="s1"), SESSION_RESULT),
    ],
)
async def test_session_setup_requests_send_empty_mcp_servers(
    pair: tuple[ClientSideConnection, Peer],
    method: str,
    params: BaseModel,
    result: dict[str, Any],
) -> None:
    conn, peer = pair
    sent = await _wire_params(peer, lambda: getattr(conn, method)(params), result)
    assert sent["mcpServers"] == []


async def test_load_session_keeps_mcp_server_type(
    pair: tuple[ClientSideConnection, Peer],
) -> None:
    conn, peer = pair
    server = HttpMcpServer(name="tools", url="http://localhost:8000/mcp")
    request = LoadSessionRequest(cwd="/w", session_id="s1", mcp_servers=[server])
    sent = await _wire_params(peer, lambda: conn.load_session(request), {})
    assert sent["mcpServers"] == [
        {"name": "tools", "url": "http://localhost:8000/mcp", "headers": [], "type": "http"}
    ]
