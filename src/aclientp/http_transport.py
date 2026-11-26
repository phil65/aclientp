"""Client side of ACP's Streamable HTTP transport (experimental).

Implements the client of the "Streamable HTTP & WebSocket Transport" RFD
(``docs/rfds/streamable-http-websocket-transport.mdx`` in the ACP repository), behaving
like the TypeScript SDK's ``createHttpStream`` (``@agentclientprotocol/sdk/experimental``):

- ``POST /acp`` sends each client message. ``initialize`` is answered directly (200, with
  the ``Acp-Connection-Id`` header); everything else is answered ``202`` and the response
  arrives on an SSE stream.
- ``GET /acp`` opens the SSE streams: one for the connection (responses to ``session/new``
  and ``session/load``, connection-level messages), and one per session
  (``Acp-Session-Id``), opened as soon as a response names the session.
- ``DELETE /acp`` ends the connection on close. Cookies are kept for the connection.

POSTs are sent one at a time, each after the previous one was answered (``_PostWriter``
awaits each in the connection's writer task). That is deliberate: the server sees messages
in the order the client wrote them (e.g. a ``session/cancel`` after the prompt it cancels,
a permission answer before a later request). The cost is that a slow POST delays the
messages behind it, for at most the client's timeout (30 s by default).

The transport is exposed as a pair of byte streams carrying newline-delimited JSON-RPC,
like stdio, so any connection (or a stdio relay) can use it unchanged.

Failure handling follows the TypeScript SDK: HTTP 401, 403, 404 and 410, network errors
and failed POSTs of notifications or responses close the transport. Any other HTTP error
fails only the request, with a JSON-RPC error whose ``data`` is ``{status, body}``. HTTP 409
marks a single session as lost: its requests fail until ``session/load`` or
``session/resume`` reattaches it. ACP v1 does not replay messages missed while a stream
was down.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import importlib.util
import logging
from typing import TYPE_CHECKING, Any

import anyenv
import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream
import httpx2

from aclientp._json import decode_object


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from anyio.abc import TaskGroup
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


logger = logging.getLogger(__name__)

HEADER_CONNECTION_ID = "Acp-Connection-Id"
HEADER_SESSION_ID = "Acp-Session-Id"
JSON_MIME_TYPE = "application/json"
EVENT_STREAM_MIME_TYPE = "text/event-stream"

CONNECTION_FAILURE_STATUSES = frozenset({401, 403, 404, 410})
"""POST statuses meaning the whole connection is unusable or gone."""
SESSION_CONFLICT_STATUS = 409
"""Status meaning a session is no longer attached to this connection."""
SESSION_REATTACH_METHODS = frozenset({"session/load", "session/resume"})
MAX_ERROR_BODY_CHARS = 8 * 1024

INTERNAL_ERROR = -32603
INVALID_REQUEST = -32600

type JsonMessage = dict[str, Any]


class HttpTransportError(ConnectionError):
    """The ACP server rejected the connection or became unreachable."""


def _id_key(message_id: object) -> str | None:
    match message_id:
        case bool():
            return None
        case str():
            return f"string:{message_id}"
        case int() | float():
            return f"number:{message_id}"
        case None:
            return "null"
        case _:
            return None


def _params_session_id(message: JsonMessage) -> str | None:
    params = message.get("params")
    if isinstance(params, dict) and isinstance(session_id := params.get("sessionId"), str):
        return session_id
    return None


def _result_session_id(message: JsonMessage) -> str | None:
    result = message.get("result")
    if isinstance(result, dict) and isinstance(session_id := result.get("sessionId"), str):
        return session_id
    return None


def _is_request(message: JsonMessage) -> bool:
    return "method" in message and "id" in message


def _is_response(message: JsonMessage) -> bool:
    return "method" not in message and "id" in message


@dataclass
class _Session:
    """State of one session-scoped SSE stream."""

    ready: anyio.Event = field(default_factory=anyio.Event)
    open: bool = False
    error: str | None = None
    """Set when the stream could not be opened or the server dropped the session (409)."""
    pending: set[str] = field(default_factory=set)
    """Keys of requests whose responses are expected on this stream."""
    cancel: anyio.CancelScope = field(default_factory=anyio.CancelScope)


class _HttpConnection:
    """One ACP connection over Streamable HTTP."""

    def __init__(
        self,
        url: str,
        client: httpx2.AsyncClient,
        task_group: TaskGroup,
        inbox: MemoryObjectSendStream[bytes],
    ) -> None:
        self.url = url
        self.client = client
        self.task_group = task_group
        self.inbox = inbox
        self.connection_id: str | None = None
        self.sessions: dict[str, _Session] = {}
        self.server_request_sessions: dict[str, str] = {}
        """Server-to-client request keys -> session, so responses carry the session header."""
        self.closed = False

    # --- inbound ------------------------------------------------------------------------

    async def _deliver(self, message: JsonMessage) -> None:
        if (session_id := _result_session_id(message)) is not None:
            await self._open_session(session_id)
        key = _id_key(message.get("id"))
        session_id = _params_session_id(message)
        if _is_request(message) and key is not None and session_id is not None:
            self.server_request_sessions[key] = session_id
        if _is_response(message) and key is not None:
            for session in self.sessions.values():
                session.pending.discard(key)
        await self._deliver_raw(anyenv.dump_json(message))

    async def _deliver_raw(self, text: str) -> None:
        if not self.closed:
            await self.inbox.send(text.encode() + b"\n")

    async def _fail_request(
        self, message: JsonMessage, text: str, *, data: object = None, code: int = INTERNAL_ERROR
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": text}
        if data is not None:
            error["data"] = data
        await self._deliver({"jsonrpc": "2.0", "id": message.get("id"), "error": error})

    # --- SSE streams ----------------------------------------------------------------------

    async def _read_sse(self, response: httpx2.Response) -> None:
        """Deliver every ``data:`` event of an SSE response (comments are keep-alives)."""
        data: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if data:
                    payload = "\n".join(data)
                    data = []
                    if (message := decode_object(payload)) is None:
                        logger.warning("Dropping malformed ACP SSE event: %.200s", payload)
                        continue
                    await self._deliver(message)
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            # "event:", "id:", "retry:" and ":" comments carry nothing for ACP v1

    def _sse_headers(self, session_id: str | None = None) -> dict[str, str]:
        assert self.connection_id is not None
        headers = {"Accept": EVENT_STREAM_MIME_TYPE, HEADER_CONNECTION_ID: self.connection_id}
        if session_id is not None:
            headers[HEADER_SESSION_ID] = session_id
        return headers

    def _sse_timeout(self) -> httpx2.Timeout:
        """The client's timeouts, without a read timeout: SSE streams idle between events."""
        default = self.client.timeout
        return httpx2.Timeout(
            connect=default.connect, read=None, write=default.write, pool=default.pool
        )

    async def _connection_stream(self, *, task_status: anyio.abc.TaskStatus[None]) -> None:
        started = False
        try:
            headers = self._sse_headers()
            async with self.client.stream(
                "GET", self.url, headers=headers, timeout=self._sse_timeout()
            ) as resp:
                if resp.status_code != httpx2.codes.OK:
                    body = (await resp.aread()).decode(errors="replace")
                    msg = f"ACP connection stream failed ({resp.status_code}): {body[:200]}"
                    raise HttpTransportError(msg)
                started = True
                task_status.started()
                await self._read_sse(resp)
        except httpx2.HTTPError as exc:
            if not started:
                raise HttpTransportError(f"ACP connection stream failed: {exc}") from exc
            logger.warning("ACP connection stream failed: %s", exc)
        if not self.closed:
            logger.info("ACP connection stream ended, closing the transport")
            await self.close_inbox()

    async def _open_session(self, session_id: str) -> None:
        """Open a session's SSE stream (once); waits until the server accepted it."""
        if self.connection_id is None:
            return
        session = self.sessions.get(session_id)
        if session is not None and (session.open or session.error is not None):
            return
        if session is None:
            session = _Session()
            self.sessions[session_id] = session
            self.task_group.start_soon(self._session_stream, session_id, session)
        await session.ready.wait()

    async def _session_stream(self, session_id: str, session: _Session) -> None:
        with session.cancel:
            try:
                headers = self._sse_headers(session_id)
                async with self.client.stream(
                    "GET", self.url, headers=headers, timeout=self._sse_timeout()
                ) as resp:
                    if resp.status_code != httpx2.codes.OK:
                        body = (await resp.aread()).decode(errors="replace")
                        session.error = (
                            f"Session stream rejected ({resp.status_code}): {body[:200]}"
                        )
                        session.ready.set()
                        if resp.status_code in CONNECTION_FAILURE_STATUSES - {404}:
                            await self.close_inbox()
                        return
                    session.open = True
                    session.ready.set()
                    await self._read_sse(resp)
            except httpx2.HTTPError as exc:
                session.error = f"Session stream failed: {exc}"
            finally:
                session.ready.set()
        # Stream ended: fail what was waiting on it; the next message reopens it.
        pending, session.pending = session.pending, set()
        if self.sessions.get(session_id) is session and session.error is None:
            del self.sessions[session_id]
        for key in pending:
            await self._deliver_raw(
                anyenv.dump_json({
                    "jsonrpc": "2.0",
                    "id": _key_to_id(key),
                    "error": {"code": INTERNAL_ERROR, "message": "ACP session stream closed"},
                })
            )

    # --- outbound -----------------------------------------------------------------------

    def _session_for(self, message: JsonMessage) -> str | None:
        if (session_id := _params_session_id(message)) is not None:
            return session_id
        if _is_response(message) and (key := _id_key(message.get("id"))) is not None:
            return self.server_request_sessions.pop(key, None)
        return None

    async def send(self, message: JsonMessage) -> None:
        """POST one client message."""
        if self.connection_id is None:
            if message.get("method") == "initialize" and "id" in message:
                await self._initialize(message)
            else:
                await self._fail_or_close(message, "ACP connection is not initialized")
            return
        session_id = self._session_for(message)
        if session_id is not None and not await self._attach(session_id, message):
            return
        await self._post(message, self.connection_id, session_id)

    async def _attach(self, session_id: str, message: JsonMessage) -> bool:
        """Make sure the session's stream is open before posting (False: message failed)."""
        reattach = message.get("method") in SESSION_REATTACH_METHODS
        session = self.sessions.get(session_id)
        if session is not None and session.error is not None:
            if not reattach:
                await self._fail_or_close(message, session.error)
                return False
            del self.sessions[session_id]
        await self._open_session(session_id)
        session = self.sessions.get(session_id)
        if session is None:
            return True
        if session.error is not None and not reattach:
            await self._fail_or_close(message, session.error)
            return False
        key = _id_key(message.get("id"))
        if _is_request(message) and key is not None:
            session.pending.add(key)
        return True

    async def _post(self, message: JsonMessage, connection_id: str, session_id: str | None) -> None:
        headers = {
            "Content-Type": JSON_MIME_TYPE,
            "Accept": f"{JSON_MIME_TYPE}, {EVENT_STREAM_MIME_TYPE}",
            HEADER_CONNECTION_ID: connection_id,
        }
        if session_id is not None:
            headers[HEADER_SESSION_ID] = session_id
        try:
            response = await self.client.post(
                self.url, content=anyenv.dump_json(message), headers=headers
            )
        except httpx2.HTTPError as exc:
            logger.warning("ACP POST failed: %s", exc)
            await self.close_inbox()
            return
        if response.is_success:
            return
        body = response.text[:MAX_ERROR_BODY_CHARS]
        status = response.status_code
        if status in CONNECTION_FAILURE_STATUSES or not _is_request(message):
            logger.warning("ACP POST rejected (%s): %.200s", status, body)
            await self.close_inbox()
            return
        session = self.sessions.get(session_id) if session_id is not None else None
        if status == SESSION_CONFLICT_STATUS and session is not None:
            session.error = f"Session is no longer attached ({status}): {body[:200]}"
            session.cancel.cancel()
        data = {"status": status, "body": body}
        await self._fail_request(message, f"ACP request failed with HTTP {status}", data=data)

    async def _initialize(self, message: JsonMessage) -> None:
        """POST ``initialize`` and open the connection stream.

        A failure answers ``initialize`` with a JSON-RPC error (so clients can show why) and
        closes the transport.
        """
        headers = {
            "Content-Type": JSON_MIME_TYPE,
            "Accept": f"{JSON_MIME_TYPE}, {EVENT_STREAM_MIME_TYPE}",
        }
        try:
            response = await self.client.post(
                self.url, content=anyenv.dump_json(message), headers=headers
            )
        except httpx2.HTTPError as exc:
            await self._fail_connection(message, f"ACP server unreachable: {exc}")
            return
        if not response.is_success:
            status, body = response.status_code, response.text[:MAX_ERROR_BODY_CHARS]
            text = f"ACP initialize failed with HTTP {status}: {body[:200]}"
            await self._fail_connection(message, text, data={"status": status, "body": body})
            return
        connection_id = response.headers.get(HEADER_CONNECTION_ID)
        if not connection_id:
            await self._fail_connection(
                message, "ACP initialize response missing Acp-Connection-Id"
            )
            return
        self.connection_id = connection_id
        try:
            await self.task_group.start(self._connection_stream)
        except HttpTransportError as exc:
            await self._fail_connection(message, str(exc))
            return
        await self._deliver(response.json())

    async def _fail_connection(
        self, message: JsonMessage, text: str, *, data: object = None
    ) -> None:
        logger.warning("%s", text)
        await self._fail_request(message, text, data=data)
        await self.close_inbox()

    async def _fail_or_close(self, message: JsonMessage, text: str) -> None:
        if _is_request(message):
            await self._fail_request(message, text)
        else:
            logger.warning("Dropping ACP message: %s", text)

    # --- lifecycle ----------------------------------------------------------------------

    async def close_inbox(self) -> None:
        if not self.closed:
            self.closed = True
            await self.inbox.aclose()

    async def terminate(self) -> None:
        """End the connection on the server (DELETE)."""
        if self.connection_id is None:
            return
        headers = {HEADER_CONNECTION_ID: self.connection_id}
        with anyio.move_on_after(5):
            try:
                await self.client.delete(self.url, headers=headers)
            except httpx2.HTTPError as exc:
                logger.debug("ACP DELETE failed: %s", exc)


def _key_to_id(key: str) -> object:
    kind, _, value = key.partition(":")
    match kind:
        case "string":
            return value
        case "number":
            number = float(value)
            return int(number) if number.is_integer() else number
        case _:
            return None


class _InboxReader(ByteReceiveStream):
    def __init__(self, inbox: MemoryObjectReceiveStream[bytes]) -> None:
        self._inbox = inbox

    async def receive(self, max_bytes: int = 65536) -> bytes:
        try:
            return await self._inbox.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError) as exc:
            raise anyio.EndOfStream from exc

    async def aclose(self) -> None:
        await self._inbox.aclose()


class _PostWriter(ByteSendStream):
    """Splits the newline-delimited JSON written by a connection into POSTs.

    One POST at a time, in write order (see the module docstring): not parallelized on
    purpose, so the server receives messages in the order they were sent.
    """

    def __init__(self, connection: _HttpConnection) -> None:
        self._connection = connection
        self._buffer = b""

    async def send(self, item: bytes) -> None:
        if self._connection.closed:
            raise anyio.BrokenResourceError
        self._buffer += item
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            if (message := decode_object(line)) is None:
                logger.warning("Dropping non-JSON-object ACP message: %.200r", line)
                continue
            await self._connection.send(message)

    async def aclose(self) -> None:
        pass


def _http2_available() -> bool:
    return importlib.util.find_spec("h2") is not None


@asynccontextmanager
async def connect_http_transport(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    client: httpx2.AsyncClient | None = None,
    timeout: float = 30.0,
) -> AsyncGenerator[tuple[ByteReceiveStream, ByteSendStream]]:
    """Connect to an ACP server's Streamable HTTP endpoint.

    The streams carry newline-delimited JSON-RPC, like a stdio agent's. The first message
    written must be ``initialize``.

    Args:
        url: The ``/acp`` endpoint.
        headers: Extra headers for every request (e.g. authentication).
        client: HTTP client to use (its cookies and settings apply). By default a client
            is created, using HTTP/2 when the ``h2`` package is installed.
        timeout: Timeout for connecting and for POSTs (SSE streams have no read timeout).

    Yields:
        (reader, writer) byte streams.
    """
    owned = client is None
    if client is None:
        client = httpx2.AsyncClient(
            http2=_http2_available(),
            headers=dict(headers or {}),
            # Finite for POSTs: one the server never answers would otherwise block
            # every later message (they are sent one at a time).
            timeout=httpx2.Timeout(timeout),
        )
    elif headers:
        client.headers.update(headers)
    send_inbox, receive_inbox = anyio.create_memory_object_stream[bytes](max_buffer_size=1024)
    try:
        async with anyio.create_task_group() as tg:
            connection = _HttpConnection(url, client, tg, send_inbox)
            try:
                yield _InboxReader(receive_inbox), _PostWriter(connection)
            finally:
                await connection.close_inbox()
                await connection.terminate()
                tg.cancel_scope.cancel()
    finally:
        if owned:
            await client.aclose()
