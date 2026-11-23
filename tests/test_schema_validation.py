"""Validation rules of the v1 models that go beyond the field types."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError
import pytest

from aclientp.schema import (
    AcpMcpServer,
    ForkSessionRequest,
    HttpMcpServer,
    InitialSessionConfigOption,
    LoadSessionRequest,
    McpServer,
    MessageMcpRequest,
    NewSessionRequest,
    ResumeSessionRequest,
    SessionOptionsRequest,
    SetSessionConfigOptionRequest,
    SseMcpServer,
    StdioMcpServer,
)


MCP = TypeAdapter(McpServer)
STDIO: dict[str, Any] = {"name": "fs", "command": "/bin/mcp", "args": [], "env": []}


# --- mcp/message attribution (upstream #2988) ------------------------------------


def test_mcp_message_carries_session_and_tool_call() -> None:
    wire = {"serverId": "s", "requestId": "r", "method": "tools/call"}
    request = MessageMcpRequest.model_validate({**wire, "sessionId": "a", "toolCallId": "t"})
    assert (request.session_id, request.tool_call_id) == ("a", "t")
    assert MessageMcpRequest.model_validate({**wire, "sessionId": "a"}).tool_call_id is None
    assert MessageMcpRequest.model_validate({**wire, "toolCallId": None}).session_id is None


def test_mcp_message_tool_call_requires_session() -> None:
    with pytest.raises(ValidationError, match="toolCallId requires sessionId"):
        MessageMcpRequest.model_validate({
            "serverId": "s",
            "requestId": "r",
            "method": "tools/call",
            "toolCallId": "t",
        })


# --- boolean config setters (upstream #2981) ------------------------------------


@pytest.mark.parametrize("value", ["false", "true", "x"])
def test_boolean_config_setter_rejects_string_values(value: str) -> None:
    with pytest.raises(ValidationError, match="requires a boolean value"):
        SetSessionConfigOptionRequest.model_validate({
            "sessionId": "s",
            "configId": "fast",
            "type": "boolean",
            "value": value,
        })
    with pytest.raises(ValidationError, match="requires a boolean value"):
        InitialSessionConfigOption.model_validate({
            "configId": "fast",
            "type": "boolean",
            "value": value,
        })


def test_config_setters_keep_their_loose_shape() -> None:
    boolean = {"sessionId": "s", "configId": "fast", "type": "boolean", "value": False}
    assert SetSessionConfigOptionRequest.model_validate(boolean).value is False
    value_id = {"sessionId": "s", "configId": "model", "value": "opus"}
    assert SetSessionConfigOptionRequest.model_validate(value_id).value == "opus"
    custom = {"sessionId": "s", "configId": "x", "type": "_custom", "value": "a"}
    assert SetSessionConfigOptionRequest.model_validate(custom).type == "_custom"
    assert InitialSessionConfigOption.model_validate({"configId": "m", "value": "a"}).type is None


# --- MCP transports --------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "model"),
    [
        (STDIO, StdioMcpServer),
        ({**STDIO, "type": "stdio"}, StdioMcpServer),
        ({"type": "http", "name": "h", "url": "https://x.test/mcp", "headers": []}, HttpMcpServer),
        ({"type": "sse", "name": "s", "url": "https://x.test/sse", "headers": []}, SseMcpServer),
        ({"type": "acp", "name": "a", "serverId": "id"}, AcpMcpServer),
    ],
)
def test_mcp_server_selects_its_transport(payload: dict[str, Any], model: type) -> None:
    assert type(MCP.validate_python(payload)) is model
    assert type(MCP.validate_json(MCP.dump_json(MCP.validate_python(payload)))) is model


@pytest.mark.parametrize(
    "payload",
    [
        {**STDIO, "type": "http", "url": 42},  # malformed http: never falls back to stdio
        {**STDIO, "type": "http"},  # http without url
        {**STDIO, "type": "websocket"},  # unknown transport
        {**STDIO, "type": None},
    ],
)
def test_mcp_server_rejects_unknown_and_malformed_transports(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MCP.validate_python(payload)
    with pytest.raises(ValidationError):
        NewSessionRequest.model_validate({"cwd": "/w", "mcpServers": [payload]})


def test_mcp_server_instances_validate() -> None:
    stdio = StdioMcpServer(name="fs", command="/bin/mcp", args=[], env=[])
    assert MCP.validate_python(stdio) is stdio
    http = HttpMcpServer(name="h", url="https://x.test/mcp")  # type: ignore[arg-type]
    assert MCP.validate_python(http) is http


# --- null lists on session setup (upstream #2976) --------------------------------


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        (NewSessionRequest, {}),
        (LoadSessionRequest, {"sessionId": "s"}),
        (ResumeSessionRequest, {"sessionId": "s"}),
        (ForkSessionRequest, {"sessionId": "s"}),
    ],
)
def test_null_initial_config_options_mean_none(model: type, extra: dict[str, Any]) -> None:
    request = model.model_validate({"cwd": "/w", "configOptions": None, **extra})
    assert list(request.config_options) == []


def test_null_additional_directories_on_session_options() -> None:
    request = SessionOptionsRequest.model_validate({"cwd": "/w", "additionalDirectories": None})
    assert list(request.additional_directories) == []
