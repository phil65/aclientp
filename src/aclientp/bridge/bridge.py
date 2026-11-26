"""ACP Bridge - serve a stdio ACP agent over Streamable HTTP and WebSocket.

Exposes a stdio agent on ``/acp`` per ACP's Streamable HTTP & WebSocket transport RFD
(see :mod:`aclientp.http_server`). Every client connection gets its own agent
subprocess; messages are relayed unchanged in both directions, so session updates and
agent requests (permissions, file access) reach the client.
"""

import contextlib
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

import anyio
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from aclientp.bridge.settings import BridgeSettings
from aclientp.http_server import AcpHttpServer
from aclientp.server_auth import bearer_token_auth, check_exposure
from aclientp.transports import spawn_stdio_transport


if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from anyio.abc import ByteReceiveStream, ByteSendStream
    from starlette.requests import Request
    from starlette.responses import Response


logger = logging.getLogger(__name__)


async def _pump(source: ByteReceiveStream, sink: ByteSendStream) -> None:
    with contextlib.suppress(
        anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError
    ):
        async for chunk in source:
            await sink.send(chunk)


@dataclass(kw_only=True)
class ACPBridge:
    """Bridge that serves a stdio ACP agent over Streamable HTTP and WebSocket."""

    command: str
    """ACP Agent shell command which should get spawned by the bridge."""

    args: list[str] = field(default_factory=list)
    """Arguments for the command."""

    env: Mapping[str, str] | None = None
    """Environment variables for the subprocess."""

    cwd: str | Path | None = None
    """Working directory for the subprocess."""

    settings: BridgeSettings = field(default_factory=BridgeSettings)
    """Bridge server settings."""

    def __post_init__(self) -> None:
        token = self.settings.token
        self._server = AcpHttpServer(
            self._serve_connection,
            authenticate=bearer_token_auth(token) if token else None,
            allow_origins=self.settings.allow_origins or [],
        )

    async def _serve_connection(self, reader: ByteReceiveStream, writer: ByteSendStream) -> None:
        """Run one agent subprocess for a client connection, relaying both ways."""
        spawn = spawn_stdio_transport(
            self.command, *self.args, env=self.env, cwd=self.cwd, log_stderr=True
        )
        async with spawn as (agent_out, agent_in, _process), anyio.create_task_group() as tg:

            async def relay(source: ByteReceiveStream, sink: ByteSendStream) -> None:
                await _pump(source, sink)
                tg.cancel_scope.cancel()  # Either side ending ends the connection

            tg.start_soon(relay, reader, agent_in)
            tg.start_soon(relay, agent_out, writer)

    async def _handle_status(self, request: Request) -> Response:
        """Health check endpoint (authenticated like ``/acp``: it shows the command)."""
        authenticate = self._server.authenticate
        if authenticate is not None and await authenticate(request) is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return JSONResponse({
            "status": "running",
            "command": self.command,
            "args": self.args,
            "connections": len(self._server.connections),
        })

    async def run(self) -> None:
        """Run the bridge server."""
        check_exposure(
            self.settings.host,
            authenticated=bool(self.settings.token),
            allow_unauthenticated=self.settings.allow_unauthenticated,
            what="the ACP bridge",
        )
        app = self._server.app()
        app.router.routes.append(Route("/status", self._handle_status, methods=["GET"]))
        config = uvicorn.Config(
            app,
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower(),
        )
        url = f"http://{self.settings.host}:{self.settings.port}"
        logger.info("ACP Bridge serving %s %s at %s/acp", self.command, self.args, url)
        await uvicorn.Server(config).serve()
