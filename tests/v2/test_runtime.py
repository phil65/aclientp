"""Tests for the v2 agent/client runtime and the version router."""

import asyncio
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError
import pytest

from aclientp.agent.implementations.testing import TestAgent as V1TestAgent
from aclientp.exceptions import InvalidResponseError, ProtocolVersionError, RequestError
from aclientp.router import RoutedAgentConnection
from aclientp.schema.v2 import (
    AgentMessageChunk,
    CancelSessionNotification,
    IdleStateSessionUpdate,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    JsonValue,
    ListProvidersRequest,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PromptRequest,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    SelectedPermissionOutcome,
    TextContent,
    UpdateSessionNotification,
)
from aclientp.v2.agent import Agent, AgentSideConnection
from aclientp.v2.client import Client, ClientSideConnection
from tests.streams import TIMEOUT, MemoryReader, MemoryWriter, Peer, make_streams


if TYPE_CHECKING:
    from collections.abc import AsyncIterator


INFO = Implementation(name="test", version="1")


class EchoAgent(Agent):
    """A v2 agent that echoes prompts after asking for permission."""

    def __init__(self, conn: AgentSideConnection) -> None:
        self.conn = conn

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        return InitializeResponse(protocol_version=2, info=INFO)

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        return NewSessionResponse(session_id="s1")

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        permission = await self.conn.request_permission(
            RequestPermissionRequest(
                session_id=params.session_id,
                title="Echo?",
                options=[PermissionOption(option_id="ok", name="OK", kind="allow_once")],
            )
        )
        assert isinstance(permission.outcome, SelectedPermissionOutcome)
        block = params.prompt[0]
        assert isinstance(block, TextContent)
        chunk = AgentMessageChunk(message_id="a1", content=TextContent(text=block.text))
        await self.conn.session_update(
            UpdateSessionNotification(session_id=params.session_id, update=chunk)
        )
        idle = IdleStateSessionUpdate(stop_reason="end_turn")
        await self.conn.session_update(
            UpdateSessionNotification(session_id=params.session_id, update=idle)
        )
        return PromptResponse(message_id="u1")


class RecordingClient(Client):
    """A v2 client that grants every permission and records updates."""

    def __init__(self) -> None:
        self.updates: list[UpdateSessionNotification] = []

    async def request_permission(
        self, params: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        option = params.options[0].option_id
        return RequestPermissionResponse(outcome=SelectedPermissionOutcome(option_id=option))

    async def session_update(self, params: UpdateSessionNotification) -> None:
        self.updates.append(params)


def _pipe() -> tuple[MemoryWriter, MemoryReader]:
    send, receive = anyio.create_memory_object_stream[bytes](100)
    return MemoryWriter(send), MemoryReader(receive)


@pytest.fixture
async def connected() -> AsyncIterator[tuple[ClientSideConnection, RecordingClient]]:
    to_agent_writer, to_agent_reader = _pipe()
    to_client_writer, to_client_reader = _pipe()
    agent_conn = AgentSideConnection(EchoAgent, to_client_writer, to_agent_reader)
    client = RecordingClient()
    client_conn = ClientSideConnection(client, to_agent_writer, to_client_reader)
    yield client_conn, client
    await client_conn.close()
    await agent_conn.close()


async def test_client_and_agent_talk_v2(
    connected: tuple[ClientSideConnection, RecordingClient],
) -> None:
    conn, client = connected

    init = await conn.initialize(InitializeRequest(protocol_version=2, info=INFO))
    session = await conn.new_session(NewSessionRequest(cwd="/work"))
    prompt = PromptRequest(session_id=session.session_id, prompt=[TextContent(text="hi")])
    response = await asyncio.wait_for(conn.prompt(prompt), TIMEOUT)
    await asyncio.wait_for(conn.connection.wait_for_notifications(), TIMEOUT)

    assert init.protocol_version == 2
    assert response.message_id == "u1"
    assert [type(u.update) for u in client.updates] == [AgentMessageChunk, IdleStateSessionUpdate]


async def test_unimplemented_agent_method_answers_method_not_found(
    connected: tuple[ClientSideConnection, RecordingClient],
) -> None:
    conn, _ = connected
    await conn.initialize(InitializeRequest(protocol_version=2, info=INFO))

    with pytest.raises(RequestError) as exc:
        await conn.list_providers(ListProvidersRequest())

    assert exc.value.code == -32601


# --- v2 rules, driven with raw frames ------------------------------------------------


@pytest.fixture
async def agent_peer() -> AsyncIterator[Peer]:
    writer, reader, peer = make_streams()
    conn = AgentSideConnection(EchoAgent, writer, reader)
    yield peer
    await conn.close()


def _init(request_id: Any, version: int = 2) -> dict[str, Any]:
    params = {"protocolVersion": version, "info": {"name": "c", "version": "1"}}
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": params}


async def test_requests_before_initialize_are_rejected(agent_peer: Peer) -> None:
    new_session = {"method": "session/new", "params": {"cwd": "/w"}}
    await agent_peer.send({"jsonrpc": "2.0", "id": 1, **new_session})

    reply = await agent_peer.receive()

    assert reply["id"] == 1
    assert reply["error"]["code"] == -32600


async def test_initialize_below_v2_is_unsupported(agent_peer: Peer) -> None:
    await agent_peer.send(_init(1, version=1))

    reply = await agent_peer.receive()

    assert reply["error"]["code"] == -32022
    assert reply["error"]["data"] == {"requested": 1, "supported": [2]}


async def test_newer_version_is_answered_as_v2(agent_peer: Peer) -> None:
    await agent_peer.send({**_init(1, version=7), "params": {"protocolVersion": 7}})

    reply = await agent_peer.receive()

    assert reply["result"]["protocolVersion"] == 2


async def test_initialize_cannot_be_repeated_after_other_requests(agent_peer: Peer) -> None:
    await agent_peer.send(_init(1))
    await agent_peer.receive()
    new_session = {"method": "session/new", "params": {"cwd": "/w"}}
    await agent_peer.send({"jsonrpc": "2.0", "id": 2, **new_session})
    await agent_peer.receive()
    await agent_peer.send(_init(3))

    reply = await agent_peer.receive()

    assert reply["id"] == 3
    assert reply["error"]["code"] == -32600


async def test_null_request_ids_and_batches_are_rejected(agent_peer: Peer) -> None:
    await agent_peer.send(_init(None))
    null_id = await agent_peer.receive()
    await agent_peer.send_raw(b'[{"jsonrpc": "2.0", "id": 1, "method": "initialize"}]\n')
    batch = await agent_peer.receive()

    assert null_id["id"] is None
    assert null_id["error"]["code"] == -32600
    assert batch["id"] is None
    assert batch["error"]["code"] == -32600


# --- Router -------------------------------------------------------------------------


@pytest.fixture
async def routed() -> AsyncIterator[tuple[RoutedAgentConnection, Peer]]:
    writer, reader, peer = make_streams()
    conn = RoutedAgentConnection(writer, reader, v1=V1TestAgent(), v2=EchoAgent)
    yield conn, peer
    await conn.close()


async def test_router_selects_v2(routed: tuple[RoutedAgentConnection, Peer]) -> None:
    conn, peer = routed
    await peer.send(_init(1))

    reply = await peer.receive()

    assert reply["result"]["protocolVersion"] == 2
    assert conn.protocol_version == 2
    assert conn.v2_side is not None


async def test_router_falls_back_to_v1(routed: tuple[RoutedAgentConnection, Peer]) -> None:
    conn, peer = routed
    params = {"protocolVersion": 1, "clientCapabilities": {}}
    await peer.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})

    reply = await peer.receive()

    assert reply["result"]["protocolVersion"] == 1
    assert conn.protocol_version == 1
    assert conn.v1_side is not None


@pytest.mark.parametrize("version", ["2024-11-05", "0.1.0", "1"])
async def test_router_reads_string_protocol_versions_like_v1(
    routed: tuple[RoutedAgentConnection, Peer], version: str
) -> None:
    conn, peer = routed
    params = {"protocolVersion": version, "clientCapabilities": {}}
    await peer.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})

    reply = await peer.receive()

    assert reply["result"]["protocolVersion"] == 1  # v1 answers, the client decides
    assert conn.protocol_version == 1


async def test_router_rejects_non_string_non_integer_versions(
    routed: tuple[RoutedAgentConnection, Peer],
) -> None:
    _, peer = routed
    params = {"protocolVersion": [1], "clientCapabilities": {}}
    await peer.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})

    reply = await peer.receive()

    assert reply["error"]["code"] == -32602


async def test_router_rejects_requests_before_initialize(
    routed: tuple[RoutedAgentConnection, Peer],
) -> None:
    _, peer = routed
    await peer.send({"jsonrpc": "2.0", "id": 1, "method": "session/list", "params": {}})

    reply = await peer.receive()

    assert reply["error"]["code"] == -32600


async def test_v2_only_router_rejects_v1() -> None:
    writer, reader, peer = make_streams()
    conn = RoutedAgentConnection(writer, reader, v2=EchoAgent)
    await peer.send(_init(1, version=1))

    reply = await peer.receive()

    assert reply["error"]["code"] == -32022
    await conn.close()


async def test_client_reports_a_v1_agent_as_protocol_version_error() -> None:
    writer, reader, peer = make_streams()
    conn = ClientSideConnection(RecordingClient(), writer, reader)
    init = asyncio.create_task(conn.initialize(InitializeRequest(protocol_version=2, info=INFO)))
    request = await peer.receive()
    # A v1 agent answers with its own version and a v1-shaped result (no `info`).
    v1_result = {"protocolVersion": 1, "agentCapabilities": {}, "agentInfo": {"name": "old"}}
    await peer.send({"jsonrpc": "2.0", "id": request["id"], "result": v1_result})

    with pytest.raises(ProtocolVersionError) as exc_info:
        await asyncio.wait_for(init, TIMEOUT)

    assert exc_info.value.received == 1
    assert exc_info.value.expected == 2
    assert exc_info.value.result == v1_result
    assert conn.initialize_response is None
    await conn.close()


async def test_invalid_responses_raise_invalid_response_error() -> None:
    writer, reader, peer = make_streams()
    conn = ClientSideConnection(RecordingClient(), writer, reader)
    init = asyncio.create_task(conn.initialize(InitializeRequest(protocol_version=2, info=INFO)))
    request = await peer.receive()
    await peer.send({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": 2}})

    with pytest.raises(InvalidResponseError) as exc_info:
        await asyncio.wait_for(init, TIMEOUT)

    assert isinstance(exc_info.value, RequestError)
    assert exc_info.value.method == "initialize"
    assert exc_info.value.code == -32603
    assert isinstance(exc_info.value.__cause__, ValidationError)
    assert any(error["loc"] == ("info",) for error in exc_info.value.errors)
    await conn.close()


class ExtAgent(EchoAgent):
    """Answers custom requests with JSON."""

    def __init__(self, conn: AgentSideConnection) -> None:
        super().__init__(conn)
        self.notes: list[JsonValue] = []

    async def ext_method(self, method: str, params: JsonValue) -> JsonValue:
        return {"method": method, "echo": params}

    async def ext_notification(self, method: str, params: JsonValue) -> None:
        self.notes.append(params)


async def test_extension_methods_carry_json() -> None:
    to_agent_writer, to_agent_reader = _pipe()
    to_client_writer, to_client_reader = _pipe()
    agents: list[ExtAgent] = []

    def make_agent(conn: AgentSideConnection) -> ExtAgent:
        agents.append(ExtAgent(conn))
        return agents[-1]

    agent_conn = AgentSideConnection(make_agent, to_client_writer, to_agent_reader)
    client_conn = ClientSideConnection(RecordingClient(), to_agent_writer, to_client_reader)
    await client_conn.initialize(InitializeRequest(protocol_version=2, info=INFO))

    result = await asyncio.wait_for(
        client_conn.ext_method("_x/hello", {"n": [1, 2.5, None]}), TIMEOUT
    )
    await client_conn.ext_notification("_x/note", ["a"])
    await client_conn.ext_notification("_x/empty")
    await asyncio.sleep(0.05)

    assert result == {"method": "_x/hello", "echo": {"n": [1, 2.5, None]}}
    assert agents[0].notes == [["a"], None]
    await client_conn.close()
    await agent_conn.close()


class _EarlyUpdatesAgent(Agent):
    """Sends a session's first update before answering ``session/new``; slow on cancel."""

    def __init__(self, conn: AgentSideConnection) -> None:
        self.conn = conn
        self.cancelled: list[str] = []

    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        return InitializeResponse(protocol_version=2, info=INFO)

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        chunk = AgentMessageChunk(message_id="a0", content=TextContent(text="welcome"))
        await self.conn.session_update(UpdateSessionNotification(session_id="s1", update=chunk))
        return NewSessionResponse(session_id="s1")

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        await self.conn.request_permission(
            RequestPermissionRequest(
                session_id=params.session_id,
                title="Go?",
                options=[PermissionOption(option_id="ok", name="OK", kind="allow_once")],
            )
        )
        # The client cancelled before answering: its cancel is handled by now
        assert self.cancelled == [params.session_id]
        return PromptResponse(message_id="u1")

    async def cancel(self, params: CancelSessionNotification) -> None:
        await asyncio.sleep(0.1)
        self.cancelled.append(params.session_id)


class _SlowClient(RecordingClient):
    conn: ClientSideConnection

    async def request_permission(
        self, params: RequestPermissionRequest
    ) -> RequestPermissionResponse:
        await self.conn.cancel(CancelSessionNotification(session_id=params.session_id))
        return await super().request_permission(params)

    async def session_update(self, params: UpdateSessionNotification) -> None:
        await asyncio.sleep(0.1)
        await super().session_update(params)


async def test_both_sides_handle_notifications_before_the_response_after_them() -> None:
    to_agent_writer, to_agent_reader = _pipe()
    to_client_writer, to_client_reader = _pipe()
    agents: list[_EarlyUpdatesAgent] = []

    def make_agent(conn: AgentSideConnection) -> _EarlyUpdatesAgent:
        agents.append(_EarlyUpdatesAgent(conn))
        return agents[0]

    agent_conn = AgentSideConnection(make_agent, to_client_writer, to_agent_reader)
    client = _SlowClient()
    conn = ClientSideConnection(client, to_agent_writer, to_client_reader)
    client.conn = conn
    await conn.initialize(InitializeRequest(protocol_version=2, info=INFO))

    session = await asyncio.wait_for(conn.new_session(NewSessionRequest(cwd="/work")), TIMEOUT)
    assert [type(u.update) for u in client.updates] == [AgentMessageChunk]  # Client side

    prompt = PromptRequest(session_id=session.session_id, prompt=[TextContent(text="hi")])
    response = await asyncio.wait_for(conn.prompt(prompt), TIMEOUT)  # Agent side asserts
    assert response.message_id == "u1"
    await conn.close()
    await agent_conn.close()
