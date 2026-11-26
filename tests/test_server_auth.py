"""Authentication and Origin checks of the WebSocket and Streamable HTTP servers.

Agents run tools on the serving machine, so: a token is required off loopback, a wrong
or missing token is refused at the handshake, and browsers (which send ``Origin`` and
apply no CORS to WebSockets) are refused unless their origin is allowed.
"""

import asyncio
import secrets
import socket
from typing import TYPE_CHECKING

import httpx2
import pytest

from aclientp.agent.protocol import Agent
from aclientp.client.connection import ClientSideConnection
from aclientp.client.implementations import NoOpClient
from aclientp.schema import InitializeRequest, InitializeResponse
from aclientp.server_auth import bearer_token_auth, is_loopback_host, origin_allowed
from aclientp.transports import HttpTransport, WebSocketTransport, serve
from aclientp.ws_transport import WebSocketTransportError, connect_remote_transport


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from aclientp.agent.connection import AgentSideConnection


TIMEOUT = 10
TOKEN = secrets.token_urlsafe(16)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ALLOWED_ORIGIN = "https://app.example"


class HelloAgent(Agent):
    def __init__(self, conn: AgentSideConnection) -> None:
        self.conn = conn

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        return InitializeResponse.create(
            title="Hello", name="hello", version="1", protocol_version=1
        )


def _agent(conn: AgentSideConnection) -> Agent:
    return HelloAgent(conn)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _wait_listening(port: int) -> None:
    for _ in range(200):
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.025)
        else:
            writer.close()
            return
    pytest.fail(f"server on port {port} did not start")


async def _initialize(url: str, headers: Mapping[str, str] | None = None) -> str | None:
    async with connect_remote_transport(url, headers=headers) as (reader, writer):
        conn = ClientSideConnection(lambda _agent: NoOpClient(), writer, reader)
        async with asyncio.timeout(TIMEOUT):
            init = await conn.initialize(InitializeRequest(protocol_version=1))
        await conn.close()
    return init.agent_info.title if init.agent_info else None


@pytest.fixture(params=["websocket", "http"])
async def ws_url(request: pytest.FixtureRequest) -> AsyncIterator[str]:
    """A loopback server requiring TOKEN and allowing ALLOWED_ORIGIN, as a ws:// URL.

    ``websocket`` is the plain WebSocket transport, ``http`` the WebSocket upgrade of the
    Streamable HTTP transport.
    """
    port = _free_port()
    auth = bearer_token_auth(TOKEN)
    if request.param == "websocket":
        transport: WebSocketTransport | HttpTransport = WebSocketTransport(
            host="127.0.0.1", port=port, authenticate=auth, allow_origins=[ALLOWED_ORIGIN]
        )
        url = f"ws://127.0.0.1:{port}"
    else:
        transport = HttpTransport(
            host="127.0.0.1", port=port, authenticate=auth, allow_origins=[ALLOWED_ORIGIN]
        )
        url = f"ws://127.0.0.1:{port}/acp"
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(_agent, transport, shutdown_event=shutdown))
    await _wait_listening(port)
    yield url
    shutdown.set()
    await asyncio.wait_for(task, TIMEOUT)


@pytest.fixture
async def open_ws_url() -> AsyncIterator[str]:
    """The plain WebSocket transport with its defaults (loopback, no token)."""
    port = _free_port()
    shutdown = asyncio.Event()
    transport = WebSocketTransport(host="127.0.0.1", port=port)
    task = asyncio.create_task(serve(_agent, transport, shutdown_event=shutdown))
    await _wait_listening(port)
    yield f"ws://127.0.0.1:{port}"
    shutdown.set()
    await asyncio.wait_for(task, TIMEOUT)


# --- helpers --------------------------------------------------------------------------


def test_loopback_hosts() -> None:
    for host in ("localhost", "127.0.0.1", "127.0.0.2", "::1", "[::1]", "LOCALHOST"):
        assert is_loopback_host(host), host
    for host in ("0.0.0.0", "", "::", "192.168.1.2", "example.com", "localhost.evil.test"):
        assert not is_loopback_host(host), host


def test_origin_allowed() -> None:
    assert origin_allowed(None, [])
    assert not origin_allowed("https://evil.example", [])
    assert not origin_allowed("null", [])
    assert origin_allowed("https://app.example", ["https://app.example/"])
    assert origin_allowed("HTTPS://App.Example", ["https://app.example"])
    assert not origin_allowed("https://app.example:8443", ["https://app.example"])
    assert origin_allowed("https://evil.example", ["*"])


# --- refusing to start ----------------------------------------------------------------


@pytest.mark.parametrize("transport_type", [WebSocketTransport, HttpTransport])
async def test_non_loopback_without_token_refused(
    transport_type: type[WebSocketTransport | HttpTransport],
) -> None:
    transport = transport_type(host="0.0.0.0", port=_free_port())
    with pytest.raises(ValueError, match="requires authentication"):
        await asyncio.wait_for(serve(_agent, transport), TIMEOUT)


async def test_non_loopback_unauthenticated_opt_out() -> None:
    port = _free_port()
    transport = WebSocketTransport(host="0.0.0.0", port=port, allow_unauthenticated=True)
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(_agent, transport, shutdown_event=shutdown))
    await _wait_listening(port)
    assert await _initialize(f"ws://127.0.0.1:{port}") == "Hello"
    shutdown.set()
    await asyncio.wait_for(task, TIMEOUT)


# --- the handshake --------------------------------------------------------------------


async def test_correct_token_accepted(ws_url: str) -> None:
    assert await _initialize(ws_url, AUTH) == "Hello"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}],
    ids=["missing", "wrong", "not-bearer"],
)
async def test_bad_token_rejected_at_handshake(ws_url: str, headers: dict[str, str]) -> None:
    with pytest.raises(WebSocketTransportError, match=r"\(401\)"):
        await _initialize(ws_url, headers)


async def test_token_in_query_not_accepted(ws_url: str) -> None:
    with pytest.raises(WebSocketTransportError, match=r"\(401\)"):
        await _initialize(f"{ws_url}?token={TOKEN}")


async def test_foreign_origin_rejected(ws_url: str) -> None:
    headers = {**AUTH, "Origin": "https://evil.example"}
    with pytest.raises(WebSocketTransportError, match=r"\(403\)"):
        await _initialize(ws_url, headers)


async def test_allowed_origin_accepted(ws_url: str) -> None:
    assert await _initialize(ws_url, {**AUTH, "Origin": ALLOWED_ORIGIN}) == "Hello"


async def test_foreign_origin_rejected_without_token_too(open_ws_url: str) -> None:
    """Cross-site WebSocket hijacking: any web page could reach a localhost agent."""
    with pytest.raises(WebSocketTransportError, match=r"\(403\)"):
        await _initialize(open_ws_url, {"Origin": "https://evil.example"})
    with pytest.raises(WebSocketTransportError, match=r"\(403\)"):
        await _initialize(open_ws_url, {"Origin": "null"})


async def test_no_origin_accepted_without_token_on_loopback(open_ws_url: str) -> None:
    assert await _initialize(open_ws_url) == "Hello"


# --- Streamable HTTP requests ---------------------------------------------------------


@pytest.mark.parametrize("ws_url", ["http"], indirect=True)
async def test_http_foreign_origin_rejected(ws_url: str) -> None:
    url = ws_url.replace("ws://", "http://")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}}
    async with httpx2.AsyncClient(timeout=TIMEOUT) as http:
        foreign = {**AUTH, "Origin": "https://evil.example"}
        assert (await http.post(url, json=init, headers=foreign)).status_code == 403
        allowed = {**AUTH, "Origin": ALLOWED_ORIGIN}
        response = await http.post(url, json=init, headers=allowed)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert (await http.post(url, json=init, headers=AUTH)).status_code == 200


# --- bridges --------------------------------------------------------------------------


async def test_bridges_refuse_non_loopback_without_token() -> None:
    from aclientp.bridge import ACPBridge, BridgeSettings
    from aclientp.bridge.ws_server import ACPWebSocketServer

    ws_bridge = ACPWebSocketServer(command="true", host="0.0.0.0")
    with pytest.raises(ValueError, match="requires authentication"):
        await ws_bridge.serve()
    http_bridge = ACPBridge(command="true", settings=BridgeSettings(host="0.0.0.0"))
    with pytest.raises(ValueError, match="requires authentication"):
        await http_bridge.run()


def test_bridge_cli_refuses_non_loopback_without_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from aclientp.bridge.__main__ import main

    monkeypatch.delenv("ACP_BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["acp-bridge", "-t", "ws", "-H", "0.0.0.0", "true"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
    assert "requires authentication" in capsys.readouterr().err
