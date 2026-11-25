"""Client side of ACP's WebSocket transport (experimental).

Implements the WebSocket mode of the "Streamable HTTP & WebSocket Transport" RFD: a
``GET`` with ``Upgrade: websocket`` on the agent's ``/acp`` endpoint (the server answers
with ``Acp-Connection-Id``), then one JSON-RPC message per text frame in both directions.
Binary frames are ignored. The first message must still be ``initialize``.

Like :func:`aclientp.http_transport.connect_http_transport`, the transport is exposed as
byte streams carrying newline-delimited JSON-RPC. :func:`connect_remote_transport` picks
the transport from the URL scheme.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import TYPE_CHECKING

import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream

from aclientp._json import single_line


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from websockets.asyncio.client import ClientConnection


logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
"""Largest message accepted from the server (file contents and images can be large)."""
HEADER_CONNECTION_ID = "acp-connection-id"


class WebSocketTransportError(ConnectionError):
    """The WebSocket connection to the ACP server could not be opened."""


class _FrameReader(ByteReceiveStream):
    """Text frames as newline-delimited JSON; a closed socket reads as EOF."""

    def __init__(self, websocket: ClientConnection) -> None:
        self._websocket = websocket

    async def receive(self, max_bytes: int = 65536) -> bytes:
        from websockets.exceptions import ConnectionClosed

        while True:
            try:
                frame = await self._websocket.recv()
            except ConnectionClosed as exc:
                raise anyio.EndOfStream from exc
            if isinstance(frame, str):  # Binary frames are ignored (RFD)
                return single_line(frame).encode() + b"\n"

    async def aclose(self) -> None:
        pass


class _FrameWriter(ByteSendStream):
    """Sends each newline-delimited JSON message as one text frame."""

    def __init__(self, websocket: ClientConnection) -> None:
        self._websocket = websocket
        self._buffer = b""

    async def send(self, item: bytes) -> None:
        from websockets.exceptions import ConnectionClosed

        self._buffer += item
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            try:
                await self._websocket.send(line.decode())
            except ConnectionClosed as exc:
                raise anyio.BrokenResourceError from exc

    async def aclose(self) -> None:
        pass


@asynccontextmanager
async def connect_websocket_transport(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    open_timeout: float = 30.0,
) -> AsyncGenerator[tuple[ByteReceiveStream, ByteSendStream]]:
    """Connect to an ACP server's WebSocket endpoint (``ws://`` or ``wss://``).

    Args:
        url: The ``/acp`` endpoint.
        headers: Extra headers for the upgrade request (e.g. authentication).
        open_timeout: Timeout for the opening handshake.

    Yields:
        (reader, writer) byte streams.

    Raises:
        WebSocketTransportError: The server rejected the upgrade or is unreachable.
    """
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidHandshake, InvalidStatus

    try:
        websocket = await connect(
            url,
            additional_headers=dict(headers or {}),
            open_timeout=open_timeout,
            max_size=MAX_MESSAGE_BYTES,
        )
    except InvalidStatus as exc:
        response = exc.response
        body = response.body.decode(errors="replace")[:200] if response.body else ""
        msg = f"ACP WebSocket upgrade rejected ({response.status_code}): {body}".rstrip(": ")
        raise WebSocketTransportError(msg) from exc
    except (InvalidHandshake, OSError, TimeoutError) as exc:
        raise WebSocketTransportError(f"ACP WebSocket connection failed: {exc}") from exc
    if websocket.response is not None:
        connection_id = websocket.response.headers.get(HEADER_CONNECTION_ID)
        logger.debug("ACP WebSocket connected (%s)", connection_id or "no connection ID")
    try:
        yield _FrameReader(websocket), _FrameWriter(websocket)
    finally:
        await websocket.close()


@asynccontextmanager
async def connect_remote_transport(
    url: str, *, headers: Mapping[str, str] | None = None
) -> AsyncGenerator[tuple[ByteReceiveStream, ByteSendStream]]:
    """Connect to a remote agent, choosing the transport by URL scheme.

    ``ws://`` / ``wss://`` use the WebSocket transport, ``http://`` / ``https://`` the
    Streamable HTTP transport. Both yield newline-delimited JSON-RPC byte streams.

    Raises:
        ValueError: The URL has another scheme.
    """
    from aclientp.http_transport import connect_http_transport

    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    match scheme:
        case "ws" | "wss":
            async with connect_websocket_transport(url, headers=headers) as streams:
                yield streams
        case "http" | "https":
            async with connect_http_transport(url, headers=headers) as streams:
                yield streams
        case _:
            msg = f"Unsupported ACP URL {url!r}: use http(s):// or ws(s)://"
            raise ValueError(msg)
