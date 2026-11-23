"""Tests for v2 session, config option, command and prompt state models."""

from __future__ import annotations

from typing import Any

from pydantic import MISSING, TypeAdapter, ValidationError
import pytest

from aclientp.schema.v2.base import to_wire
from aclientp.schema.v2.commands import Command, Skill, TextCommandInput, UnknownCommand
from aclientp.schema.v2.common import Error
from aclientp.schema.v2.config_options import (
    BooleanSessionConfigOption,
    SelectSessionConfigOption,
    SetConfigOptionBooleanRequest,
    SetSessionConfigOptionRequest,
)
from aclientp.schema.v2.mcp import McpServerStdio, MessageMcpRequest, UnknownMcpServer
from aclientp.schema.v2.prompt import (
    IdleStateUpdate,
    OtherStateUpdate,
    StateUpdate,
    UnknownStateUpdate,
)
from aclientp.schema.v2.sessions import (
    ForkSessionRequest,
    NewSessionRequest,
    NewSessionResponse,
    ReplayFromStart,
    ResumeSessionRequest,
    SessionOptionsRequest,
)


STATES: TypeAdapter[Any] = TypeAdapter(StateUpdate)


def test_new_session_response_with_options_and_commands() -> None:
    response = NewSessionResponse.model_validate({
        "sessionId": "s1",
        "configOptions": [
            {
                "type": "select",
                "configId": "model",
                "name": "Model",
                "category": "model",
                "currentValue": "fast",
                "options": [{"value": "fast", "name": "Fast"}],
            },
            {"type": "boolean", "configId": "think", "name": "Thinking", "currentValue": True},
        ],
        "availableCommands": [
            {
                "type": "command",
                "name": "plan",
                "description": "Plan",
                "input": {"type": "text", "hint": "goal"},
            },
            {"type": "skill", "name": "pdf", "description": "PDFs"},
            {"type": "_x", "name": "custom", "description": "Custom"},
        ],
    })

    options = response.config_options
    commands = response.available_commands
    assert options is not MISSING
    assert commands is not MISSING
    assert [type(o) for o in options] == [SelectSessionConfigOption, BooleanSessionConfigOption]
    assert [type(c) for c in commands] == [Command, Skill, UnknownCommand]
    command = commands[0]
    assert isinstance(command, Command)
    assert isinstance(command.input, TextCommandInput)


def test_set_config_option_request_by_value_type() -> None:
    request = TypeAdapter(SetSessionConfigOptionRequest).validate_python({
        "sessionId": "s1",
        "configId": "think",
        "type": "boolean",
        "value": False,
    })

    assert isinstance(request, SetConfigOptionBooleanRequest)
    assert request.value is False


def test_new_session_request_with_mcp_servers() -> None:
    request = NewSessionRequest.model_validate({
        "cwd": "/work",
        "mcpServers": [
            {"type": "stdio", "name": "fs", "command": "/usr/bin/mcp-fs"},
            {"type": "_ws", "name": "remote", "url": "wss://x"},
        ],
    })

    servers = request.mcp_servers
    assert servers is not MISSING
    assert [type(s) for s in servers] == [McpServerStdio, UnknownMcpServer]


def test_resume_with_replay_from_start() -> None:
    request = ResumeSessionRequest(session_id="s1", cwd="/work", replay_from=ReplayFromStart())

    assert to_wire(request) == {"sessionId": "s1", "cwd": "/work", "replayFrom": {"type": "start"}}


def test_idle_state_with_error() -> None:
    state = STATES.validate_python({
        "state": "idle",
        "stopReason": "error",
        "error": {"code": -32603, "message": "boom"},
    })

    assert isinstance(state, IdleStateUpdate)
    assert isinstance(state.error, Error)
    assert state.error.code == -32603


def test_idle_state_keeps_null_stop_reason() -> None:
    state = IdleStateUpdate(stop_reason=None)

    assert to_wire(state) == {"state": "idle", "stopReason": None}


def test_unknown_and_custom_states_are_distinct() -> None:
    assert isinstance(STATES.validate_python({"state": "unknown"}), UnknownStateUpdate)
    assert isinstance(STATES.validate_python({"state": "_paused"}), OtherStateUpdate)


# --- upstream schema-v2.0.0-beta.4 and #2976 -----------------------------------------


def test_mcp_message_tool_call_requires_session() -> None:
    wire = {"serverId": "s", "requestId": "r", "method": "tools/call"}
    request = MessageMcpRequest.model_validate({**wire, "sessionId": "a", "toolCallId": "t"})
    assert to_wire(request) == {**wire, "sessionId": "a", "toolCallId": "t"}
    assert MessageMcpRequest.model_validate({**wire, "toolCallId": None}).session_id is MISSING
    for session in ({}, {"sessionId": None}):
        with pytest.raises(ValidationError, match="toolCallId requires sessionId"):
            MessageMcpRequest.model_validate({**wire, **session, "toolCallId": "t"})


def test_null_setup_lists_read_as_empty() -> None:
    for model, extra in (
        (NewSessionRequest, {}),
        (ResumeSessionRequest, {"sessionId": "s"}),
        (ForkSessionRequest, {"sessionId": "s"}),
    ):
        request = model.model_validate({"cwd": "/w", "configOptions": None, **extra})
        assert list(request.config_options) == []
        assert model.model_validate({"cwd": "/w", **extra}).config_options is MISSING
    options = SessionOptionsRequest.model_validate({"cwd": "/w", "additionalDirectories": None})
    assert list(options.additional_directories) == []
