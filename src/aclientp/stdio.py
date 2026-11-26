from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
import os
import queue
import sys
import threading
from typing import TYPE_CHECKING, Any

import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream

from aclientp.agent.connection import AgentSideConnection
from aclientp.client.connection import ClientSideConnection
from aclientp.connection import Connection
from aclientp.transports import spawn_stdio_transport


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping
    from pathlib import Path

    from anyio.abc import Process

    from aclientp.agent.protocol import Agent
    from aclientp.client.protocol import Client
    from aclientp.connection import MethodHandler, StreamObserver


class StdinStream(ByteReceiveStream):
    """Stdin as a ByteReceiveStream, read by a daemon thread.

    A blocking read cannot be interrupted. In the default executor it would keep
    ``asyncio.run`` from returning until the client sends data or closes the pipe, so an
    agent could not exit on its own (on a fatal error or a signal) while the client
    keeps stdin open. A daemon thread does not hold the interpreter. It reads the file
    descriptor (``os.read``), not ``sys.stdin.buffer``: a daemon thread blocked inside
    the buffered reader would hold its lock at interpreter shutdown, which is fatal.
    """

    def __init__(self, fd: int | None = None) -> None:
        """Read from file descriptor ``fd`` (default: stdin's)."""
        self._fd = fd
        self._requests: queue.SimpleQueue[tuple[asyncio.Future[bytes], int]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._unclaimed = b""  # Read for a receive() that was cancelled meanwhile

    async def receive(self, max_bytes: int = 65536) -> bytes:
        """Read up to ``max_bytes``, returning as soon as any data is available."""
        if self._unclaimed:
            data, self._unclaimed = self._unclaimed[:max_bytes], self._unclaimed[max_bytes:]
            return data
        if self._thread is None:
            fd = sys.stdin.fileno() if self._fd is None else self._fd
            self._thread = threading.Thread(
                target=self._read_loop, args=(fd,), name="acp-stdin", daemon=True
            )
            self._thread.start()
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._requests.put((future, max_bytes))
        data = await future
        if not data:
            raise anyio.EndOfStream
        return data

    def _read_loop(self, fd: int) -> None:
        while True:
            future, max_bytes = self._requests.get()
            try:
                # Returns as soon as any data is available (up to max_bytes)
                data = os.read(fd, max_bytes)
            except Exception as exc:  # noqa: BLE001
                self._deliver(future, None, exc)
                continue
            self._deliver(future, data, None)
            if not data:
                return

    def _deliver(
        self, future: asyncio.Future[bytes], data: bytes | None, exc: Exception | None
    ) -> None:
        def settle() -> None:
            if future.cancelled():
                self._unclaimed += data or b""
            elif exc is not None:
                future.set_exception(exc)
            else:
                future.set_result(data or b"")

        with contextlib.suppress(RuntimeError):  # The event loop is closed
            future.get_loop().call_soon_threadsafe(settle)

    async def aclose(self) -> None:
        """Close is a no-op for stdin."""


class StdoutStream(ByteSendStream):
    """Wrapper for stdout that implements ByteSendStream interface."""

    async def send(self, item: bytes) -> None:
        """Write bytes to stdout."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write, item)

    def _write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def aclose(self) -> None:
        """Close is a no-op for stdout."""


async def stdio_streams() -> tuple[StdinStream, StdoutStream]:
    """Create stdio anyio-compatible streams (cross-platform).

    Returns (stdin, stdout) as byte stream wrappers compatible with
    ByteReceiveStream and ByteSendStream interfaces.
    """
    return StdinStream(), StdoutStream()


@asynccontextmanager
async def spawn_stdio_connection(
    handler: MethodHandler,
    command: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    observers: list[StreamObserver] | None = None,
    log_stderr: bool = False,
) -> AsyncGenerator[tuple[Connection, Process]]:
    """Spawn a subprocess and bind its stdio to a low-level Connection.

    Args:
        handler: Method handler for the connection.
        command: The command to execute.
        *args: Arguments for the command.
        env: Environment variables for the subprocess.
        cwd: Working directory for the subprocess.
        observers: Optional stream observers.
        log_stderr: If True, log stderr output from the subprocess.
    """
    async with spawn_stdio_transport(command, *args, env=env, cwd=cwd, log_stderr=log_stderr) as (
        reader,
        writer,
        process,
    ):
        conn = Connection(handler, writer, reader, observers=observers)
        try:
            yield conn, process
        finally:
            with anyio.CancelScope(shield=True):  # see spawn_stdio_transport
                await conn.close()


@asynccontextmanager
async def spawn_agent_process(
    to_client: Callable[[Agent], Client],
    command: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    log_stderr: bool = False,
    **connection_kwargs: Any,
) -> AsyncGenerator[tuple[ClientSideConnection, Process]]:
    """Spawn an ACP agent subprocess and return a ClientSideConnection to it.

    Args:
        to_client: Factory function that creates a Client from an Agent.
        command: The command to execute.
        *args: Arguments for the command.
        env: Environment variables for the subprocess.
        cwd: Working directory for the subprocess.
        log_stderr: If True, log stderr output from the subprocess.
        **connection_kwargs: Additional arguments for ClientSideConnection.
    """
    async with spawn_stdio_transport(
        command,
        *args,
        env=env,
        cwd=cwd,
        log_stderr=log_stderr,
    ) as (reader, writer, process):
        conn = ClientSideConnection(to_client, writer, reader, **connection_kwargs)
        try:
            yield conn, process
        finally:
            with anyio.CancelScope(shield=True):  # see spawn_stdio_transport
                await conn.close()


@asynccontextmanager
async def spawn_client_process(
    to_agent: Callable[[AgentSideConnection], Agent],
    command: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    log_stderr: bool = False,
    **connection_kwargs: Any,
) -> AsyncGenerator[tuple[AgentSideConnection, Process]]:
    """Spawn an ACP client subprocess and return an AgentSideConnection to it.

    Args:
        to_agent: Factory function that creates an Agent from an AgentSideConnection.
        command: The command to execute.
        *args: Arguments for the command.
        env: Environment variables for the subprocess.
        cwd: Working directory for the subprocess.
        log_stderr: If True, log stderr output from the subprocess.
        **connection_kwargs: Additional arguments for AgentSideConnection.
    """
    async with spawn_stdio_transport(
        command,
        *args,
        env=env,
        cwd=cwd,
        log_stderr=log_stderr,
    ) as (reader, writer, process):
        conn = AgentSideConnection(to_agent, writer, reader, **connection_kwargs)
        try:
            yield conn, process
        finally:
            with anyio.CancelScope(shield=True):  # see spawn_stdio_transport
                await conn.close()


async def run_agent(
    agent: Agent | Callable[[AgentSideConnection], Agent],
    input_stream: ByteSendStream | None = None,
    output_stream: ByteReceiveStream | None = None,
    **connection_kwargs: Any,
) -> None:
    """Run an ACP agent over stdio or provided streams.

    This is the recommended entry point for running agents. It handles stream
    setup and connection lifecycle automatically, and returns once the client
    disconnects (or raises when cancelled).

    Args:
        agent: An Agent implementation or a factory callable that takes
            an AgentSideConnection and returns an Agent. Using a factory allows
            the agent to access the connection for client communication.
        input_stream: ByteSendStream the agent writes to (defaults to stdout).
        output_stream: ByteReceiveStream the agent reads from (defaults to stdin).
        **connection_kwargs: Additional keyword arguments for AgentSideConnection.

    Example with direct agent:
        ```python
        class MyAgent(Agent):
            async def initialize(self, params: InitializeRequest) -> InitializeResponse:
                return InitializeResponse(protocol_version=params.protocol_version)
            # ... implement protocol methods ...

        await run_agent(MyAgent())
        ```

    Example with factory:
        ```python
        class MyAgent(Agent):
            def __init__(self, connection: AgentSideConnection):
                self.connection = connection
            # ... implement protocol methods ...

        def create_agent(conn: AgentSideConnection) -> MyAgent:
            return MyAgent(conn)

        await run_agent(create_agent)
        ```
    """
    if (input_stream is None) != (output_stream is None):
        msg = "Pass both input_stream and output_stream, or neither to use stdio"
        raise ValueError(msg)
    if input_stream is None or output_stream is None:
        output_stream, input_stream = await stdio_streams()

    conn = AgentSideConnection(agent, input_stream, output_stream, **connection_kwargs)
    try:
        await conn.wait_closed()
    finally:
        await conn.close()


def connect_to_agent(
    client: Client,
    input_stream: ByteSendStream,
    output_stream: ByteReceiveStream,
    **connection_kwargs: Any,
) -> ClientSideConnection:
    """Create a ClientSideConnection to an ACP agent.

    This is the recommended entry point for client-side connections.

    Args:
        client: The client implementation.
        input_stream: ByteSendStream for sending to the agent.
        output_stream: ByteReceiveStream for receiving from the agent.
        **connection_kwargs: Additional keyword arguments for ClientSideConnection.

    Returns:
        A ClientSideConnection connected to the agent.
    """

    # Create a factory that ignores the connection parameter
    def client_factory(connection: Agent) -> Client:
        return client

    return ClientSideConnection(client_factory, input_stream, output_stream, **connection_kwargs)
