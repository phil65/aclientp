"""Lenient deserialization (``x-deserialize-*``): newer peers' messages still parse."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import MISSING, ValidationError
import pytest

from aclientp.client.connection import ClientSideConnection
from aclientp.client.implementations.noop_client import NoOpClient
from aclientp.schema import (
    InitializeRequest,
    InitializeResponse,
    NewSessionRequest,
    NewSessionResponse,
    SessionNotification,
    StdioMcpServer,
    TextContentBlock,
    ToolCallStart,
)
from aclientp.schema.v2 import UpdateSessionNotification
from aclientp.schema.v2.sessions import NewSessionRequest as V2NewSessionRequest
from tests.streams import TIMEOUT, make_streams


TOOL_CALL: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "t1",
    "title": "Read file",
    "kind": "brand_new_kind",
    "status": "paused",
    "_meta": "oops",
    "content": [
        {"type": "content", "content": {"type": "text", "text": "kept"}},
        {"type": "hologram", "frames": 3},
    ],
    "locations": [{"path": "/a.py", "line": 3}, {"line": "nope"}],
}


def test_v1_tool_call_from_a_newer_agent_parses() -> None:
    notification = SessionNotification.model_validate({"sessionId": "s", "update": TOOL_CALL})
    update = notification.update
    assert isinstance(update, ToolCallStart)
    assert update.kind is None or update.kind == "other"
    assert update.status is None or update.status == "pending"
    assert update.field_meta is None
    assert len(update.content or []) == 1
    assert [location.path for location in update.locations or []] == ["/a.py"]


def test_v1_bad_session_meta_is_dropped() -> None:
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    params = {"sessionId": "s", "update": update, "_meta": "oops"}
    assert SessionNotification.model_validate(params).field_meta is None


def test_v1_unknown_config_option_type_does_not_fail_session_new() -> None:
    select = {
        "id": "model",
        "name": "Model",
        "type": "select",
        "currentValue": "a",
        "options": [{"value": "a", "name": "A"}],
    }
    slider = {"id": "temp", "name": "Temperature", "type": "slider", "min": 0, "max": 1}
    response = NewSessionResponse.model_validate({
        "sessionId": "s",
        "configOptions": [select, slider],
    })
    assert [option.id for option in response.config_options or []] == ["model"]


def test_v1_unknown_auth_method_does_not_fail_initialize() -> None:
    response = InitializeResponse.model_validate({
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": "maybe", "promptCapabilities": {"image": True}},
        "authMethods": [
            {"id": "login", "name": "Log in"},
            {"id": "x", "name": "Future", "type": "future_auth"},
        ],
    })
    assert [method.id for method in response.auth_methods] == ["login"]
    assert response.agent_capabilities is not None
    assert response.agent_capabilities.load_session is False
    assert response.agent_capabilities.prompt_capabilities.image is True


def test_v1_null_lists_read_as_empty() -> None:
    request = NewSessionRequest.model_validate({"cwd": "/", "mcpServers": None})
    assert list(request.mcp_servers) == []
    server = StdioMcpServer.model_validate({
        "name": "fs",
        "command": "x",
        "args": None,
        "env": None,
    })
    assert (list(server.args), list(server.env)) == ([], [])


def test_v2_bad_meta_and_content_items() -> None:
    notification = UpdateSessionNotification.model_validate({
        "sessionId": "s",
        "_meta": "oops",
        "update": {
            "sessionUpdate": "agent_message",
            "messageId": "m",
            "content": [{"type": "text", "text": "kept"}, {"type": "text"}],
        },
    })
    assert notification.field_meta is MISSING
    assert len(notification.update.content) == 1  # type: ignore[union-attr]


def test_v2_null_lists_read_as_empty() -> None:
    request = V2NewSessionRequest.model_validate({"cwd": "/", "configOptions": None})
    assert list(request.config_options) == []  # type: ignore[arg-type]


def test_invalid_local_model_instances_raise() -> None:
    # A model instance was built by local code: a wrong one is a bug, not peer data
    with pytest.raises(ValidationError):
        ToolCallStart(tool_call_id="t", title="x", content=[TextContentBlock(text="x")])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        InitializeResponse(protocol_version=1, agent_capabilities=TextContentBlock(text="x"))  # type: ignore[arg-type]


async def test_client_receives_tool_call_from_a_newer_agent() -> None:
    received: list[SessionNotification] = []

    class Recorder(NoOpClient):
        async def session_update(self, params: SessionNotification) -> None:
            received.append(params)

    writer, reader, peer = make_streams()
    conn = ClientSideConnection(lambda _agent: Recorder(), writer, reader)
    try:
        params = {"sessionId": "s", "update": TOOL_CALL}
        await peer.send({"jsonrpc": "2.0", "method": "session/update", "params": params})
        async with asyncio.timeout(TIMEOUT):
            while not received:
                await asyncio.sleep(0.01)
    finally:
        await conn.close()
    assert isinstance(received[0].update, ToolCallStart)


@pytest.mark.parametrize(("wire", "expected"), [(1, 1), ("1", 1), ("2024-11-05", 0), ("0.1.0", 0)])
def test_v1_string_protocol_version(wire: Any, expected: int) -> None:
    request = InitializeRequest.model_validate({"protocolVersion": wire})
    assert request.protocol_version == expected
