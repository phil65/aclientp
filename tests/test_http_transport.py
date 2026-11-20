"""Streamable HTTP client transport, against an in-process fake of the RFD's server side."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import anyenv
import httpx
import pytest

from aclientp.client.connection import ClientSideConnection
from aclientp.client.implementations import NoOpClient
from aclientp.exceptions import RequestError
from aclientp.http_relay import parse_header
from aclientp.http_transport import connect_http_transport
from aclientp.schema import (
    InitializeRequest,
    NewSessionRequest,
    PromptRequest,
    SessionNotification,
    TextContentBlock,
)


URL = "http://agent.test/acp"
CONNECTION = "conn-1"
SESSION = "sess-1"
TIMEOUT = 5


@dataclass
class FakeServer:
    """The server side of the RFD's Streamable HTTP mode, as far as these tests need it."""

    prompt_status: int = 202
    conn_queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    session_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = field(default_factory=dict)
    posts: list[tuple[dict[str, str], dict[str, Any]]] = field(default_factory=list)
    streams: list[str | None] = field(default_factory=list)
    deleted: bool = False
    pending_prompt: object = None

    def _session_queue(self, session_id: str) -> asyncio.Queue[dict[str, Any] | None]:
        return self.session_queues.setdefault(session_id, asyncio.Queue())

    async def handle(self, request: httpx.Request) -> httpx.Response:
        match request.method:
            case "POST":
                return await self._post(request)
            case "GET":
                return self._get(request)
            case "DELETE":
                self.deleted = True
                return httpx.Response(202)
            case _:
                return httpx.Response(405)

    async def _post(self, request: httpx.Request) -> httpx.Response:
        message = anyenv.load_json(request.content, return_type=dict)
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.posts.append((headers, message))
        if message.get("method") == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
            body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            return httpx.Response(200, json=body, headers={"Acp-Connection-Id": CONNECTION})
        if headers.get("acp-connection-id") != CONNECTION:
            return httpx.Response(404, text="Unknown Acp-Connection-Id")
        match message.get("method"):
            case "session/new":
                response = {"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": SESSION}}
                await self.conn_queue.put(response)
            case "session/prompt":
                if self.prompt_status != 202:
                    return httpx.Response(self.prompt_status, text="busy")
                self.pending_prompt = message["id"]
                queue = self._session_queue(headers["acp-session-id"])
                chunk = {"content": {"type": "text", "text": "hi"}}
                update = {"sessionUpdate": "agent_message_chunk", **chunk}
                await queue.put({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": SESSION, "update": update},
                })
                await queue.put({
                    "jsonrpc": "2.0",
                    "id": "srv-1",
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": SESSION,
                        "toolCall": {"toolCallId": "t1"},
                        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
                    },
                })
            case None if message.get("id") == "srv-1":  # The client's permission answer
                queue = self._session_queue(headers["acp-session-id"])
                result = {"stopReason": "end_turn"}
                await queue.put({"jsonrpc": "2.0", "id": self.pending_prompt, "result": result})
            case _:
                pass
        return httpx.Response(202)

    def _get(self, request: httpx.Request) -> httpx.Response:
        if "text/event-stream" not in request.headers.get("accept", ""):
            return httpx.Response(406)
        if request.headers.get("acp-connection-id") != CONNECTION:
            return httpx.Response(404)
        session_id = request.headers.get("acp-session-id")
        self.streams.append(session_id)
        queue = self.conn_queue if session_id is None else self._session_queue(session_id)

        async def events() -> Any:
            yield b":\n\n"  # keep-alive comment
            while (message := await queue.get()) is not None:
                yield f"data: {anyenv.dump_json(message)}\n\n".encode()

        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=events())


class RecordingClient(NoOpClient):
    def __init__(self) -> None:
        self.updates: list[SessionNotification] = []

    async def session_update(self, params: SessionNotification) -> None:
        self.updates.append(params)


def _client(server: FakeServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server.handle))


async def test_full_flow_over_streamable_http() -> None:
    server = FakeServer()
    recorder = RecordingClient()
    async with (
        _client(server) as http,
        connect_http_transport(URL, client=http, headers={"X-API-Key": "k"}) as (reader, writer),
    ):
        conn = ClientSideConnection(lambda _agent: recorder, writer, reader)
        async with asyncio.timeout(TIMEOUT):
            init = await conn.initialize(InitializeRequest(protocol_version=1))
            assert init.agent_capabilities is not None
            assert init.agent_capabilities.load_session
            session = await conn.new_session(NewSessionRequest(cwd="/tmp", mcp_servers=[]))
            assert session.session_id == SESSION
            prompt = PromptRequest(session_id=SESSION, prompt=[TextContentBlock(text="hello")])
            result = await conn.prompt(prompt)
        assert result.stop_reason == "end_turn"
        await conn.close()
    assert server.streams == [None, SESSION]  # Connection stream, then the session's
    assert server.deleted
    assert [u.update.session_update for u in recorder.updates] == ["agent_message_chunk"]
    # Session-scoped POSTs (the prompt and the permission answer) carry both headers
    by_method = {message.get("method") or message.get("id"): h for h, message in server.posts}
    for key in ("session/prompt", "srv-1"):
        assert by_method[key]["acp-connection-id"] == CONNECTION
        assert by_method[key]["acp-session-id"] == SESSION
    assert "acp-session-id" not in by_method["session/new"]
    assert all(h["x-api-key"] == "k" for h, _ in server.posts)


async def test_http_error_fails_only_the_request() -> None:
    server = FakeServer(prompt_status=500)
    async with (
        _client(server) as http,
        connect_http_transport(URL, client=http) as (reader, writer),
    ):
        conn = ClientSideConnection(lambda _agent: NoOpClient(), writer, reader)
        async with asyncio.timeout(TIMEOUT):
            await conn.initialize(InitializeRequest(protocol_version=1))
            await conn.new_session(NewSessionRequest(cwd="/tmp", mcp_servers=[]))
            prompt = PromptRequest(session_id=SESSION, prompt=[TextContentBlock(text="x")])
            with pytest.raises(RequestError) as exc_info:
                await conn.prompt(prompt)
            assert exc_info.value.data == {"status": 500, "body": "busy"}
            # The connection is still usable
            session = await conn.new_session(NewSessionRequest(cwd="/tmp", mcp_servers=[]))
        assert session.session_id == SESSION
        await conn.close()


async def test_rejected_initialize_is_a_json_rpc_error() -> None:
    async def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Invalid API key")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http,
        connect_http_transport(URL, client=http) as (reader, writer),
    ):
        conn = ClientSideConnection(lambda _agent: NoOpClient(), writer, reader)
        async with asyncio.timeout(TIMEOUT):
            with pytest.raises(RequestError) as exc_info:
                await conn.initialize(InitializeRequest(protocol_version=1))
        assert exc_info.value.data == {"status": 401, "body": "Invalid API key"}
        await conn.close()


def test_parse_header_expands_environment_variables() -> None:
    env = {"KEY": "secret"}
    assert parse_header("X-API-Key: ${KEY}", env) == ("X-API-Key", "secret")
    assert parse_header("Authorization: Bearer $KEY", env) == ("Authorization", "Bearer secret")
    with pytest.raises(ValueError, match="unset environment variable"):
        parse_header("X-API-Key: ${MISSING}", env)
    with pytest.raises(ValueError, match="expected 'Name: value'"):
        parse_header("no-colon", env)
