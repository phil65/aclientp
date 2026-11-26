"""Transport abstractions for ACP agents.

This module provides transport configuration classes and utilities for running
ACP agents over different transports (stdio, WebSocket, etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any, Literal, assert_never

import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream

from aclientp._json import single_line


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
    from pathlib import Path

    from anyio.abc import Process
    from websockets.asyncio.server import ServerConnection
    from websockets.http11 import Request, Response

    from aclientp.agent.connection import AgentSideConnection
    from aclientp.agent.protocol import Agent
    from aclientp.connection import StreamObserver
    from aclientp.http_server import Authenticate
    from aclientp.router import RoutedAgentConnection
    from aclientp.server_auth import HandshakeAuthenticate
    from aclientp.v2.agent import Agent as V2Agent, AgentSideConnection as V2AgentSideConnection

logger = logging.getLogger(__name__)


# =============================================================================
# Transport Configuration Classes
# =============================================================================


@dataclass
class StdioTransport:
    """Configuration for stdio transport.

    This is the default transport for ACP agents, communicating over
    stdin/stdout streams.
    """


@dataclass
class WebSocketTransport:
    """Configuration for WebSocket server transport.

    Runs an ACP agent as a WebSocket server that accepts client connections.
    Each client connection gets its own agent instance. The handshake is checked before
    any ACP message (see :mod:`aclientp.server_auth`): browser origins not in
    ``allow_origins`` get 403, callers ``authenticate`` rejects 401.

    Attributes:
        host: Host to bind the WebSocket server to.
        port: Port for the WebSocket server.
        authenticate: Identifies callers from the upgrade request's headers (None:
            rejected with 401), e.g. :func:`~aclientp.server_auth.bearer_token_auth`.
            Required off loopback.
        allow_origins: Browser origins allowed to connect (``*``: any). Requests with
            another ``Origin`` header are refused; requests without one are allowed.
        allow_unauthenticated: Serve without ``authenticate`` off loopback anyway (e.g.
            behind a proxy that authenticates).
    """

    host: str = "localhost"
    port: int = 8765
    authenticate: HandshakeAuthenticate | None = None
    allow_origins: list[str] = field(default_factory=list)
    allow_unauthenticated: bool = False


@dataclass
class HttpTransport:
    """Configuration for the Streamable HTTP & WebSocket server transport (experimental).

    Serves ACP on one endpoint per the transport RFD: Streamable HTTP (POST/GET/DELETE)
    and WebSocket upgrade. Each client connection gets its own agent instance. Requires
    the ``bridge`` extra (starlette, uvicorn).

    Attributes:
        host: Host to bind to.
        port: Port to listen on.
        path: Endpoint path.
        authenticate: Identifies callers (None rejects with 401); connections belong to
            their creator. None accepts every caller, for local use only: required off
            loopback.
        allow_origins: CORS origins for browser clients (``*``: any). Requests with
            another ``Origin`` header are refused (403), WebSocket upgrades included.
        allow_unauthenticated: Serve without ``authenticate`` off loopback anyway (e.g.
            behind a proxy that authenticates).
        max_buffered_bytes: Output a connection may hold for its client before the agent's
            writes wait.
        max_output_stall: Seconds a client may take no output while over that limit
            before its connection is closed.
    """

    host: str = "localhost"
    port: int = 8765
    path: str = "/acp"
    authenticate: Authenticate | None = None
    allow_origins: list[str] = field(default_factory=list)
    max_buffered_bytes: int = 64 * 1024 * 1024
    max_output_stall: float = 60.0
    allow_unauthenticated: bool = False


@dataclass
class StreamTransport:
    """Configuration for custom stream transport.

    Allows passing pre-created streams for the agent to use.

    Attributes:
        reader: Stream to read incoming messages from.
        writer: Stream to write outgoing messages to.
    """

    reader: ByteReceiveStream
    writer: ByteSendStream


# Type alias for all supported transports
Transport = (
    StdioTransport
    | WebSocketTransport
    | HttpTransport
    | StreamTransport
    | Literal["stdio", "websocket", "http"]
)


# =============================================================================
# Transport Runner
# =============================================================================


type V1AgentSource = Agent | Callable[[AgentSideConnection], Agent]
"""A v1 agent, or a factory receiving its connection."""

type V2AgentSource = V2Agent | Callable[[V2AgentSideConnection], V2Agent]
"""A v2 agent, or a factory receiving its connection."""


@dataclass(frozen=True, slots=True)
class _AgentSpec:
    """What every transport needs to serve one client connection."""

    v1: V1AgentSource | None
    v2: V2AgentSource | None
    debug_file: str | None
    observers: list[StreamObserver] | None

    def connect(
        self, writer: ByteSendStream, reader: ByteReceiveStream
    ) -> AgentSideConnection | RoutedAgentConnection:
        """Serve a client: plain v1, or routed by ``initialize`` once a v2 agent is given."""
        from aclientp.agent.connection import AgentSideConnection
        from aclientp.router import RoutedAgentConnection

        if self.v2 is None:
            assert self.v1 is not None  # Checked by serve()
            return AgentSideConnection(
                _ensure_factory(self.v1),
                writer,
                reader,
                observers=self.observers,
                debug_file=self.debug_file,
            )
        return RoutedAgentConnection(
            writer,
            reader,
            v1=None if self.v1 is None else _ensure_factory(self.v1),
            v2=self.v2,
            observers=self.observers,
            debug_file=self.debug_file,
        )


async def serve(
    agent: V1AgentSource | None = None,
    transport: Transport = "stdio",
    *,
    v2_agent: V2AgentSource | None = None,
    shutdown_event: asyncio.Event | None = None,
    debug_file: str | None = None,
    observers: list[StreamObserver] | None = None,
) -> None:
    """Run an ACP agent with the specified transport.

    This is the main entry point for running ACP agents. It handles transport
    setup and lifecycle management automatically.

    With only ``agent``, every client is served by that v1 agent. With ``v2_agent``,
    each client connection is routed on ``initialize``
    (:class:`~aclientp.router.RoutedAgentConnection`): clients asking for v2 or later
    get the v2 agent, others the v1 agent (if given).

    Args:
        agent: The v1 agent, or a factory that takes an AgentSideConnection and
            returns one.
        transport: Transport configuration. Can be:
            - "stdio" or StdioTransport(): Use stdin/stdout
            - "websocket" or WebSocketTransport(...): Run WebSocket server
            - StreamTransport(...): Use custom streams
        v2_agent: The v2 agent, or a factory that takes a v2 AgentSideConnection and
            returns one.
        shutdown_event: Optional event to signal shutdown. If not provided,
            runs until cancelled.
        debug_file: Optional file path for debug message logging.
        observers: Observers of the raw messages of each connection.

    Example:
        ```python
        # Stdio (default)
        await serve(MyAgent())

        # WebSocket server
        await serve(MyAgent(), WebSocketTransport(host="0.0.0.0", port=9000))

        # v1 and v2 clients on one endpoint
        await serve(MyAgent(), v2_agent=MyV2Agent)

        # With shutdown control
        shutdown = asyncio.Event()
        task = asyncio.create_task(serve(MyAgent(), shutdown_event=shutdown))
        # ... later ...
        shutdown.set()
        await task
        ```
    """
    if agent is None and v2_agent is None:
        msg = "serve() needs a v1 agent, a v2 agent, or both"
        raise ValueError(msg)
    spec = _AgentSpec(v1=agent, v2=v2_agent, debug_file=debug_file, observers=observers)
    # Normalize string shortcuts to config objects
    match transport:
        case "stdio":
            transport = StdioTransport()
        case "websocket":
            transport = WebSocketTransport()
        case "http":
            transport = HttpTransport()

    # Dispatch to appropriate runner
    match transport:
        case StdioTransport():
            await _serve_stdio(spec, shutdown_event)
        case WebSocketTransport():
            await _serve_websocket(spec, transport, shutdown_event)
        case HttpTransport():
            await _serve_http(spec, transport, shutdown_event)
        case StreamTransport(reader=reader, writer=writer):
            await _serve_streams(spec, reader, writer, shutdown_event)
        case _ as unreachable:
            assert_never(unreachable)


async def _run_until_closed(
    conn: AgentSideConnection | RoutedAgentConnection, shutdown_event: asyncio.Event | None
) -> None:
    """Keep the connection open until the peer disconnects, shutdown is signalled or cancelled."""
    waiters: set[asyncio.Task[Any]] = {asyncio.create_task(conn.wait_closed())}
    if shutdown_event is not None:
        waiters.add(asyncio.create_task(shutdown_event.wait()))
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        await conn.close()


async def _serve_stdio(spec: _AgentSpec, shutdown_event: asyncio.Event | None) -> None:
    """Run agent over stdio."""
    from aclientp.stdio import stdio_streams

    reader, writer = await stdio_streams()
    await _run_until_closed(spec.connect(writer, reader), shutdown_event)


async def _serve_streams(
    spec: _AgentSpec,
    reader: ByteReceiveStream,
    writer: ByteSendStream,
    shutdown_event: asyncio.Event | None,
) -> None:
    """Run agent over custom streams."""
    await _run_until_closed(spec.connect(writer, reader), shutdown_event)


def websocket_handshake_guard(
    authenticate: HandshakeAuthenticate | None, allow_origins: list[str]
) -> Callable[[ServerConnection, Request], Awaitable[Response | None]]:
    """A ``process_request`` hook for ``websockets.serve`` checking Origin and auth.

    Foreign browser origins get 403 and callers ``authenticate`` rejects 401, before the
    upgrade (see :mod:`aclientp.server_auth`).
    """
    from http import HTTPStatus

    from aclientp.server_auth import origin_allowed

    async def process_request(connection: ServerConnection, request: Request) -> Response | None:
        origins = request.headers.get_all("Origin")
        origin = origins[0] if len(origins) == 1 else None
        if len(origins) > 1 or not origin_allowed(origin, allow_origins):
            logger.warning("Refused WebSocket client from origin %r", origin or origins)
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden origin\n")
        if authenticate is not None:
            if len(request.headers.get_all("Authorization")) > 1:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
            if await authenticate(request) is None:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        return None

    return process_request


async def _serve_websocket(
    spec: _AgentSpec,
    transport: WebSocketTransport,
    shutdown_event: asyncio.Event | None,
) -> None:
    """Run agent as WebSocket server."""
    import websockets

    from aclientp.server_auth import check_exposure

    host, port = transport.host, transport.port
    check_exposure(
        host,
        authenticated=transport.authenticate is not None,
        allow_unauthenticated=transport.allow_unauthenticated,
        what="ACP over WebSocket",
    )
    guard = websocket_handshake_guard(transport.authenticate, transport.allow_origins)

    shutdown = shutdown_event or asyncio.Event()
    connections: list[AgentSideConnection | RoutedAgentConnection] = []

    async def handle_client(websocket: ServerConnection) -> None:
        """Handle a single WebSocket client connection."""
        logger.info("WebSocket client connected")

        # Create stream adapters for WebSocket
        ws_reader = _WebSocketReadStream(websocket)
        ws_writer = _WebSocketWriteStream(websocket)

        conn = spec.connect(ws_writer, ws_reader)
        connections.append(conn)
        try:
            # The reader turns a closed socket into EOF, which closes the connection.
            await _run_until_closed(conn, shutdown)
        finally:
            connections.remove(conn)
            logger.info("WebSocket client disconnected")

    logger.info("Starting WebSocket server on ws://%s:%d", host, port)
    async with websockets.serve(handle_client, host, port, process_request=guard):
        logger.info("WebSocket server running on ws://%s:%d", host, port)
        await shutdown.wait()

    # Clean up remaining connections
    for conn in connections:
        await conn.close()


async def _serve_http(
    spec: _AgentSpec,
    transport: HttpTransport,
    shutdown_event: asyncio.Event | None,
) -> None:
    """Run agent as a Streamable HTTP & WebSocket server."""
    import uvicorn

    from aclientp.http_server import AcpHttpServer
    from aclientp.server_auth import check_exposure

    check_exposure(
        transport.host,
        authenticated=transport.authenticate is not None,
        allow_unauthenticated=transport.allow_unauthenticated,
        what="ACP over HTTP",
    )

    async def serve_connection(reader: ByteReceiveStream, writer: ByteSendStream) -> None:
        await _run_until_closed(spec.connect(writer, reader), None)

    server = AcpHttpServer(
        serve_connection,
        path=transport.path,
        authenticate=transport.authenticate,
        allow_origins=transport.allow_origins,
        max_buffered_bytes=transport.max_buffered_bytes,
        max_output_stall=transport.max_output_stall,
    )
    config = uvicorn.Config(
        server.app(), host=transport.host, port=transport.port, log_level="warning"
    )
    uvicorn_server = uvicorn.Server(config)

    async def stop_on_shutdown() -> None:
        if shutdown_event is not None:
            await shutdown_event.wait()
            uvicorn_server.should_exit = True

    stopper = asyncio.create_task(stop_on_shutdown())
    url = f"http://{transport.host}:{transport.port}{transport.path}"
    logger.info("ACP Streamable HTTP / WebSocket server on %s", url)
    try:
        await uvicorn_server.serve()
    finally:
        stopper.cancel()
        await server.close()


class _WebSocketReadStream(ByteReceiveStream):
    """Adapter to read from WebSocket as a ByteReceiveStream."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._buffer = b""

    async def receive(self, max_bytes: int = 65536) -> bytes:
        # If we have buffered data, return it
        if self._buffer:
            data = self._buffer[:max_bytes]
            self._buffer = self._buffer[max_bytes:]
            return data

        # Read from WebSocket
        try:
            message: str | bytes = await self._websocket.recv()
            if isinstance(message, bytes):
                message = message.decode(errors="replace")
            # One frame is one message: one line of the JSON-RPC line protocol
            message = single_line(message).encode() + b"\n"
            self._buffer = message[max_bytes:]
            return message[:max_bytes]
        except Exception as e:
            raise anyio.EndOfStream from e

    async def aclose(self) -> None:
        pass


class _WebSocketWriteStream(ByteSendStream):
    """Adapter to write to WebSocket as a ByteSendStream."""

    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket

    async def send(self, item: bytes) -> None:
        # WebSocket sends complete messages, strip newline if present
        message = item.decode().strip()
        if message:
            await self._websocket.send(message)

    async def aclose(self) -> None:
        pass


def _ensure_factory(
    agent: Agent | Callable[[AgentSideConnection], Agent],
) -> Callable[[AgentSideConnection], Agent]:
    """Ensure agent is wrapped in a factory function."""
    if callable(agent) and not hasattr(agent, "initialize"):
        return agent  # Already a factory

    # Wrap instance in factory
    def factory(connection: AgentSideConnection) -> Agent:
        return agent  # type: ignore[return-value]  # ty:ignore[invalid-explicit-return]

    return factory


# =============================================================================
# Subprocess Transport Utilities (for spawning agents)
# =============================================================================


DEFAULT_INHERITED_ENV_VARS = (
    [
        "APPDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "USERNAME",
        "USERPROFILE",
    ]
    if os.name == "nt"
    else ["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"]
)


def default_environment() -> dict[str, str]:
    """Return a trimmed environment based on MCP best practices."""
    env: dict[str, str] = {}
    for key in DEFAULT_INHERITED_ENV_VARS:
        value = os.environ.get(key)
        if value is None:
            continue
        # Skip function-style env vars on some shells (see MCP reference)
        if value.startswith("()"):
            continue
        env[key] = value
    return env


async def _drain_stderr_to_log(
    process: Process,
    command: str,
    log_level: int = logging.WARNING,
    *,
    log: bool = True,
    on_stderr: Callable[[str], object] | None = None,
) -> None:
    """Read stderr from a process line by line; log each line and/or pass it on.

    Args:
        process: The subprocess to read stderr from.
        command: Command name for log messages.
        log_level: Log level for stderr output (default WARNING).
        log: Whether to log the lines.
        on_stderr: Called with each line (without line ending).
    """
    if process.stderr is None:
        return

    buffer = b""

    def emit(raw: bytes) -> None:
        line = raw.decode(errors="replace").rstrip("\r\n")
        if log and line.strip():
            logger.log(log_level, "[%s stderr] %s", command, line)
        if on_stderr is not None:
            try:
                on_stderr(line)
            except Exception:
                logger.exception("stderr callback failed for %s", command)

    try:
        # Chunks are not lines: split them, and keep a partial last line for later.
        async for chunk in process.stderr:
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                emit(raw)
    except anyio.EndOfStream:
        pass
    except Exception:
        logger.debug("Error reading stderr from %s", command, exc_info=True)
    if buffer:
        emit(buffer)


@asynccontextmanager
async def spawn_stdio_transport(
    command: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    stderr: int | None = subprocess.PIPE,
    shutdown_timeout: float = 2.0,
    log_stderr: bool = False,
    stderr_log_level: int = logging.WARNING,
    on_stderr: Callable[[str], object] | None = None,
) -> AsyncGenerator[tuple[ByteReceiveStream, ByteSendStream, Process]]:
    """Launch a subprocess and expose its stdio streams as anyio streams.

    This mirrors the defensive shutdown behaviour used by the MCP Python SDK:
    close stdin first, wait for graceful exit, then escalate to terminate/kill.

    Args:
        command: The command to execute.
        *args: Arguments for the command.
        env: Environment variables (merged with defaults).
        cwd: Working directory for the subprocess.
        stderr: How to handle stderr (default: subprocess.PIPE).
        shutdown_timeout: Timeout for graceful shutdown.
        log_stderr: If True, read stderr in background and log each line.
        stderr_log_level: Log level for stderr output (default WARNING).
        on_stderr: Called with each stderr line (read in background), e.g. to show
            it in a UI.

    With ``stderr=PIPE`` (the default), stderr is always read in the background, so
    an agent writing a lot to it can never block on a full pipe: lines go to
    ``on_stderr`` and / or the log, and are discarded otherwise. To read
    ``process.stderr`` yourself, pass another ``stderr`` (e.g. a file) instead.
    """
    merged_env = default_environment()
    if env:
        merged_env.update(env)

    process = await anyio.open_process(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        env=merged_env,
        cwd=str(cwd) if cwd is not None else None,
    )

    if process.stdout is None or process.stdin is None:
        process.kill()
        await process.wait()
        raise RuntimeError("spawn_stdio_transport requires stdout/stdin pipes")

    stderr_task: asyncio.Task[None] | None = None
    if process.stderr is not None and stderr == subprocess.PIPE:
        stderr_task = asyncio.create_task(
            _drain_stderr_to_log(
                process, command, stderr_log_level, log=log_stderr, on_stderr=on_stderr
            )
        )

    try:
        yield process.stdout, process.stdin, process
    finally:
        # Shielded: in a cancelled anyio scope every await would be cancelled at once
        # (level-triggered) and the child would keep running. Each await is bounded
        # by shutdown_timeout.
        with anyio.CancelScope(shield=True):
            await _shutdown_process(process, shutdown_timeout)
            # The process is gone: read what is left of stderr (e.g. a crash message)
            # before stopping the reader.
            if stderr_task is not None:
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(stderr_task), shutdown_timeout)
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task


async def _shutdown_process(process: Process, shutdown_timeout: float) -> None:
    """Close stdin, wait for the process to exit, then terminate / kill it."""
    # Attempt graceful stdin shutdown first. Bounded: closing waits for buffered
    # data to drain, which never happens if the child stopped reading.
    if process.stdin is not None:
        with contextlib.suppress(Exception), anyio.move_on_after(shutdown_timeout):
            await process.stdin.aclose()

    try:
        with anyio.fail_after(shutdown_timeout):
            await process.wait()
    except TimeoutError:
        process.terminate()
        try:
            with anyio.fail_after(shutdown_timeout):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
