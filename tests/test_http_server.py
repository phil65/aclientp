"""Streamable HTTP & WebSocket server, served by uvicorn and used by real clients."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
from typing import TYPE_CHECKING, Any
import uuid

import anyenv
import httpx
import pytest

from aclientp.agent.protocol import Agent
from aclientp.client.connection import ClientSideConnection
from aclientp.client.implementations import NoOpClient
from aclientp.http_server import bearer_token_auth
from aclientp.http_transport import connect_http_transport
from aclientp.schema import (
    AgentMessageChunk,
    InitializeRequest,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PromptRequest,
    PromptResponse,
    RequestPermissionRequest,
    SessionNotification,
    TextContentBlock,
    ToolCallUpdate,
)
from aclientp.transports import HttpTransport, serve
from aclientp.ws_transport import WebSocketTransportError, connect_remote_transport


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from aclientp.agent.connection import AgentSideConnection


TOKEN = "test-token"
TIMEOUT = 10


class EchoAgent(Agent):
    """Streams one chunk, asks for permission, and replays it on session/load."""

    def __init__(self, conn: AgentSideConnection) -> None:
        self.conn = conn

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        return InitializeResponse.create(
            title="Echo", name="echo", version="1", protocol_version=1, load_session=True
        )

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        return NewSessionResponse(session_id=f"sess-{uuid.uuid4().hex[:8]}")

    async def load_session(self, params: LoadSessionRequest) -> LoadSessionResponse:
        await self._say(params.session_id, "replayed")
        return LoadSessionResponse()

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        await self._say(params.session_id, "thinking")
        option = PermissionOption(option_id="allow", name="Allow", kind="allow_once")
        tool_call = ToolCallUpdate(tool_call_id="t1")
        request = RequestPermissionRequest(
            session_id=params.session_id, tool_call=tool_call, options=[option]
        )
        response = await self.conn.request_permission(request)
        outcome = response.outcome.outcome
        await self._say(params.session_id, f"permission: {outcome}")
        return PromptResponse(stop_reason="end_turn")

    async def _say(self, session_id: str, text: str) -> None:
        update = AgentMessageChunk.text(text)
        await self.conn.session_update(SessionNotification(session_id=session_id, update=update))


class SetupRequestAgent(EchoAgent):
    """Asks the client something while it creates the session (before answering)."""

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
        option = PermissionOption(option_id="allow", name="Allow", kind="allow_once")
        request = RequestPermissionRequest(
            session_id=session_id, tool_call=ToolCallUpdate(tool_call_id="setup"), options=[option]
        )
        await self.conn.request_permission(request)
        return NewSessionResponse(session_id=session_id)


class RecordingClient(NoOpClient):
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def session_update(self, params: SessionNotification) -> None:
        match params.update:
            case AgentMessageChunk(content=TextContentBlock(text=text)):
                self.texts.append(text)
            case _:
                pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(params=[EchoAgent, SetupRequestAgent], ids=["echo", "setup-request"])
async def url(request: pytest.FixtureRequest) -> AsyncIterator[str]:
    agent_type: type[EchoAgent] = request.param
    port = _free_port()
    shutdown = asyncio.Event()
    transport = HttpTransport(host="127.0.0.1", port=port, authenticate=bearer_token_auth(TOKEN))
    agent = serve(lambda conn: agent_type(conn), transport, shutdown_event=shutdown)
    task = asyncio.create_task(agent)
    endpoint = f"http://127.0.0.1:{port}/acp"
    async with httpx.AsyncClient() as http:
        for _ in range(100):
            try:
                await http.get(endpoint)
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.05)
    yield endpoint
    shutdown.set()
    await asyncio.wait_for(task, TIMEOUT)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("scheme", ["http", "ws"])
async def test_client_full_flow_and_reattach(url: str, scheme: str) -> None:
    url = url.replace("http://", f"{scheme}://")
    recorder = RecordingClient()
    async with connect_remote_transport(url, headers=AUTH) as (reader, writer):
        conn = ClientSideConnection(lambda _agent: recorder, writer, reader)
        async with asyncio.timeout(TIMEOUT):
            init = await conn.initialize(InitializeRequest(protocol_version=1))
            assert init.agent_info is not None
            assert init.agent_info.title == "Echo"
            session = await conn.new_session(NewSessionRequest(cwd="/tmp", mcp_servers=[]))
            prompt = [TextContentBlock(text="hi")]
            result = await conn.prompt(PromptRequest(session_id=session.session_id, prompt=prompt))
        assert result.stop_reason == "end_turn"
        assert recorder.texts == ["thinking", "permission: selected"]
        await conn.close()

    # A new connection reattaches the session; the replay arrives on its session stream
    recorder = RecordingClient()
    async with connect_remote_transport(url, headers=AUTH) as (reader, writer):
        conn = ClientSideConnection(lambda _agent: recorder, writer, reader)
        async with asyncio.timeout(TIMEOUT):
            await conn.initialize(InitializeRequest(protocol_version=1))
            load = LoadSessionRequest(session_id=session.session_id, cwd="/tmp", mcp_servers=[])
            await conn.load_session(load)
        assert recorder.texts == ["replayed"]
        await conn.close()


async def test_websocket(url: str) -> None:
    import websockets

    ws_url = url.replace("http://", "ws://")
    async with websockets.connect(ws_url, additional_headers=AUTH) as ws:
        assert ws.response is not None
        assert ws.response.headers["acp-connection-id"]

        async def call(message_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            await ws.send(
                anyenv.dump_json({
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "method": method,
                    "params": params,
                })
            )
            while True:
                message = anyenv.load_json(await ws.recv(), return_type=dict)
                if message.get("id") == message_id and "method" not in message:
                    return message
                if message.get("method") == "session/request_permission":
                    outcome = {"outcome": {"outcome": "selected", "optionId": "allow"}}
                    await ws.send(
                        anyenv.dump_json({"jsonrpc": "2.0", "id": message["id"], "result": outcome})
                    )

        async with asyncio.timeout(TIMEOUT):
            init = await call(1, "initialize", {"protocolVersion": 1})
            assert init["result"]["agentInfo"]["title"] == "Echo"
            session = await call(2, "session/new", {"cwd": "/tmp", "mcpServers": []})
            session_id = session["result"]["sessionId"]
            prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": "hi"}]}
            result = await call(3, "session/prompt", prompt)
        assert result["result"]["stopReason"] == "end_turn"
        await ws.send("[1, 2]")
        batch_error = anyenv.load_json(await ws.recv(), return_type=dict)
        assert batch_error["error"]["code"] == -32600


async def test_protocol_errors_and_auth(url: str) -> None:
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
    async with httpx.AsyncClient(timeout=TIMEOUT) as http:
        assert (await http.post(url, json=init)).status_code == 401
        response = await http.post(url, json=init, headers=AUTH)
        assert response.status_code == 200
        connection_id = response.headers["acp-connection-id"]
        conn_headers = {**AUTH, "Acp-Connection-Id": connection_id}
        # Batches are rejected, as are session-scoped methods without the session header
        assert (await http.post(url, json=[init], headers=conn_headers)).status_code == 400
        prompt = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "s"},
        }
        missing = await http.post(url, json=prompt, headers=conn_headers)
        assert (missing.status_code, missing.text) == (400, "Missing Acp-Session-Id")
        bad_type = await http.post(url, content="{}", headers=conn_headers)
        assert bad_type.status_code == 415
        # One receiver per stream; unknown connections are 404
        get_headers = {**conn_headers, "Accept": "text/event-stream"}
        async with http.stream("GET", url, headers=get_headers) as stream:
            assert stream.status_code == 200
            second = await http.get(url, headers=get_headers)
            assert second.status_code == 409  # One receiver per stream
        unknown = {**AUTH, "Acp-Connection-Id": "nope", "Accept": "text/event-stream"}
        assert (await http.get(url, headers=unknown)).status_code == 404
        assert (await http.delete(url, headers=conn_headers)).status_code == 202
        assert (await http.delete(url, headers=conn_headers)).status_code == 404


STDIO_AGENT = """
import asyncio
from aclientp.agent.protocol import Agent
from aclientp.schema import (
    AgentMessageChunk, InitializeResponse, NewSessionResponse, PromptResponse, SessionNotification,
)
from aclientp.transports import serve


class Echo(Agent):
    def __init__(self, conn):
        self.conn = conn

    async def initialize(self, params):
        return InitializeResponse.create(
            title="stdio echo", name="echo", version="1", protocol_version=1
        )

    async def new_session(self, params):
        return NewSessionResponse(session_id="sess-stdio")

    async def prompt(self, params):
        update = AgentMessageChunk.text("from stdio")
        notification = SessionNotification(session_id=params.session_id, update=update)
        await self.conn.session_update(notification)
        return PromptResponse(stop_reason="end_turn")


asyncio.run(serve(lambda conn: Echo(conn)))
"""


async def test_bridge_relays_a_stdio_agent(tmp_path: Path) -> None:
    from aclientp.bridge import ACPBridge, BridgeSettings

    script = tmp_path / "echo_agent.py"
    script.write_text(STDIO_AGENT)
    port = _free_port()
    settings = BridgeSettings(port=port, log_level="WARNING")
    bridge = ACPBridge(command=sys.executable, args=[str(script)], settings=settings)
    task = asyncio.create_task(bridge.run())
    url = f"http://127.0.0.1:{port}/acp"
    try:
        async with httpx.AsyncClient() as http:
            for _ in range(100):
                try:
                    status = await http.get(f"http://127.0.0.1:{port}/status")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.05)
            assert status.json()["connections"] == 0
        recorder = RecordingClient()
        async with connect_http_transport(url) as (reader, writer):
            conn = ClientSideConnection(lambda _agent: recorder, writer, reader)
            async with asyncio.timeout(TIMEOUT):
                init = await conn.initialize(InitializeRequest(protocol_version=1))
                assert init.agent_info is not None
                assert init.agent_info.title == "stdio echo"
                session = await conn.new_session(NewSessionRequest(cwd="/tmp", mcp_servers=[]))
                prompt = [TextContentBlock(text="hi")]
                await conn.prompt(PromptRequest(session_id=session.session_id, prompt=prompt))
            await conn.close()
        assert recorder.texts == ["from stdio"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_websocket_client_rejected_without_token(url: str) -> None:
    ws_url = url.replace("http://", "ws://")
    with pytest.raises(WebSocketTransportError, match="rejected"):
        async with connect_remote_transport(ws_url):
            pass


async def test_unsupported_scheme() -> None:
    with pytest.raises(ValueError, match="Unsupported ACP URL"):
        async with connect_remote_transport("ftp://agent.test/acp"):
            pass
