"""AClientP: main package.

Alternative ACP protocol implementation.
"""

from __future__ import annotations

from importlib.metadata import version

__version__ = version("aclientp")
__title__ = "AClientP"

__author__ = "Philipp Temminghoff"
__author_email__ = "philipptemminghoff@googlemail.com"
__copyright__ = "Copyright (c) 2025 Philipp Temminghoff"
__license__ = "MIT"
__url__ = "https://github.com/phil65/aclientp"

__all__ = [
    "__version__",
]


from aclientp.agent import AgentSideConnection
from aclientp.agent.protocol import Agent
from aclientp.bridge import ACPBridge, BridgeSettings
from aclientp.client import (
    ClientSideConnection,
    DefaultACPClient,
    HeadlessACPClient,
    NoOpClient,
)
from aclientp.client.protocol import Client
from aclientp.debug_server import cli
from aclientp.exceptions import RequestError
from aclientp.filesystem import ACPFileSystem, ACPPath
from aclientp.schema import (
    PROTOCOL_VERSION,
    AgentMessageChunk,
    AgentMethod,
    AllowedOutcome,
    Annotations,
    AuthenticateRequest,
    AuthenticateResponse,
    AvailableCommand,
    CancelNotification,
    ClientCapabilities,
    ClientMethod,
    CreateTerminalRequest,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapability,
    HttpMcpServer,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    KillTerminalCommandRequest,
    KillTerminalCommandResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    ModelInfo,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PlanEntry,
    PlanEntryPriority,
    PlanEntryStatus,
    PromptRequest,
    PromptResponse,
    ReadTextFileRequest,
    ReadTextFileResponse,
    ReleaseTerminalRequest,
    ReleaseTerminalResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    SessionMode,
    SessionModelState,
    SessionModeState,
    SessionNotification,
    SetSessionModelRequest,
    SetSessionModelResponse,
    SetSessionModeRequest,
    SetSessionModeResponse,
    SseMcpServer,
    StdioMcpServer,
    TerminalOutputRequest,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCall,
    ToolCallContent,
    ToolCallKind,
    ToolCallLocation,
    ToolCallStatus,
    UserMessageChunk,
    WaitForTerminalExitRequest,
    WaitForTerminalExitResponse,
    WriteTextFileRequest,
    WriteTextFileResponse,
)
from aclientp.stdio import connect_to_agent, run_agent, stdio_streams
from aclientp.terminal_handle import TerminalHandle


__all__ = [  # noqa: RUF022
    # bridge
    "ACPBridge",
    "BridgeSettings",
    # client implementations
    "DefaultACPClient",
    "HeadlessACPClient",
    "NoOpClient",
    # constants
    "PROTOCOL_VERSION",
    # literal types
    "AgentMethod",
    "ClientMethod",
    # types
    "Annotations",
    "AvailableCommand",
    "PlanEntry",
    "AllowedOutcome",
    "ToolCall",
    "DeniedOutcome",
    "ToolCallContent",
    "PermissionOption",
    "ToolCallKind",
    "PlanEntryPriority",
    "PlanEntryStatus",
    "ToolCallStatus",
    "InitializeRequest",
    "ToolCallLocation",
    "InitializeResponse",
    "EnvVariable",
    "HttpMcpServer",
    "StdioMcpServer",
    "SseMcpServer",
    "NewSessionRequest",
    "AgentMessageChunk",
    "UserMessageChunk",
    "TextContentBlock",
    "NewSessionResponse",
    "LoadSessionRequest",
    "LoadSessionResponse",
    "AuthenticateRequest",
    "AuthenticateResponse",
    "PromptRequest",
    "ClientCapabilities",
    "SessionModeState",
    "SessionMode",
    "Implementation",
    "PromptResponse",
    "WriteTextFileRequest",
    "WriteTextFileResponse",
    "ReadTextFileRequest",
    "ReadTextFileResponse",
    "RequestPermissionRequest",
    "RequestPermissionResponse",
    "CancelNotification",
    "SessionNotification",
    "SetSessionModeRequest",
    "SetSessionModeResponse",
    # model types
    "ModelInfo",
    "SessionModelState",
    "SetSessionModelRequest",
    "SetSessionModelResponse",
    # terminal types
    "CreateTerminalRequest",
    "CreateTerminalResponse",
    "TerminalOutputRequest",
    "TerminalOutputResponse",
    "WaitForTerminalExitRequest",
    "WaitForTerminalExitResponse",
    "KillTerminalCommandRequest",
    "KillTerminalCommandResponse",
    "ReleaseTerminalRequest",
    "ReleaseTerminalResponse",
    # core
    "AgentSideConnection",
    "ClientSideConnection",
    "RequestError",
    "Agent",
    "Client",
    "TerminalHandle",
    # connection helpers (recommended)
    "run_agent",
    "connect_to_agent",
    # split protocols
    "FileSystemCapability",
    # stdio helper
    "stdio_streams",
    # filesystem
    "ACPFileSystem",
    "ACPPath",
    # cli
    "cli",
]
