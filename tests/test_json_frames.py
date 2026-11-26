"""Peer-controlled frames that are not JSON objects never crash a transport."""

import asyncio
from typing import Any

import anyenv
import anyio
import pytest

from aclientp._json import decode_object, single_line
from aclientp.bridge.ws_server import ACPWebSocketServer
from aclientp.http_server import _AgentOutput, _Backlog
from aclientp.ws_transport import _FrameReader


TIMEOUT = 2
JUNK = [b"1", b"[1]", b'"x"', b"null", b"{", b"\xff\xfe"]


@pytest.mark.parametrize("data", JUNK)
def test_decode_object_rejects_non_objects(data: bytes) -> None:
    assert decode_object(data) is None


def test_decode_object_accepts_objects() -> None:
    assert decode_object('{"a": 1}') == {"a": 1}
    assert decode_object(b'{"a": 1}') == {"a": 1}


def test_single_line_keeps_pretty_printed_json_intact() -> None:
    pretty = anyenv.dump_json({"a": "x\ny", "b": [1, 2]}, indent=True)
    assert "\n" in pretty
    line = single_line(pretty.replace("\n", "\r\n"))
    assert "\n" not in line
    assert "\r" not in line
    assert anyenv.load_json(line) == {"a": "x\ny", "b": [1, 2]}


async def test_agent_output_drops_non_objects() -> None:
    delivered: list[dict[str, Any]] = []
    output = _AgentOutput(delivered.append, _Backlog(10_000, 1), lambda: None)
    await output.send(b"\n".join([*JUNK, b'{"jsonrpc": "2.0", "method": "x"}', b""]))
    assert delivered == [{"jsonrpc": "2.0", "method": "x"}]


class _FakeClientSocket:
    def __init__(self, frames: list[str | bytes]) -> None:
        self.frames = frames

    async def recv(self) -> str | bytes:
        from websockets.exceptions import ConnectionClosed

        if not self.frames:
            raise ConnectionClosed(None, None)
        return self.frames.pop(0)


async def test_websocket_client_joins_multi_line_frames() -> None:
    pretty = '{\n  "jsonrpc": "2.0",\r\n  "method": "x"\n}'
    reader = _FrameReader(_FakeClientSocket([b"binary", pretty]))  # type: ignore[arg-type]
    data = await reader.receive()
    assert data.count(b"\n") == 1
    assert anyenv.load_json(data) == {"jsonrpc": "2.0", "method": "x"}
    with pytest.raises(anyio.EndOfStream):
        await reader.receive()


class _FakeAgentStdout:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def receive(self, max_bytes: int = 65536) -> bytes:
        data, self.data = self.data, b""
        return data


async def test_ws_bridge_skips_non_object_agent_output() -> None:
    bridge = ACPWebSocketServer(command="true")
    bridge._reader = _FakeAgentStdout(b"\n".join([*JUNK, b'{"id": 1}', b""]))  # type: ignore[assignment]
    async with asyncio.timeout(TIMEOUT):
        assert await bridge._read_jsonrpc_message() == {"id": 1}
        assert await bridge._read_jsonrpc_message() is None


class _FakeServerSocket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[dict[str, Any]] = []

    def __aiter__(self) -> _FakeServerSocket:
        return self

    async def __anext__(self) -> str:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(anyenv.load_json(data, return_type=dict))


async def test_ws_bridge_answers_non_object_client_frames() -> None:
    bridge = ACPWebSocketServer(command="true")
    forwarded: list[dict[str, Any]] = []

    async def to_agent(message: dict[str, Any]) -> None:
        forwarded.append(message)

    bridge._send_to_agent = to_agent  # type: ignore[method-assign]
    socket = _FakeServerSocket(["1", "[1]", "{", '{"jsonrpc": "2.0", "method": "x"}'])
    async with asyncio.timeout(TIMEOUT):
        await bridge._handle_client(socket)  # type: ignore[arg-type]
    assert forwarded == [{"jsonrpc": "2.0", "method": "x"}]
    error = {"code": -32700, "message": "Parse error"}
    assert socket.sent == [{"jsonrpc": "2.0", "id": None, "error": error}] * 3
