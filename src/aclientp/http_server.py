"""Server side of ACP's Streamable HTTP & WebSocket transport (experimental).

Implements the server of the "Streamable HTTP & WebSocket Transport" RFD
(``docs/rfds/streamable-http-websocket-transport.mdx`` in the ACP repository), routing
like the TypeScript SDK's ``AcpServer`` (``@agentclientprotocol/sdk/experimental``), on
one endpoint:

- ``POST`` sends a client message. ``initialize`` (without ``Acp-Connection-Id``) creates a
  connection and is answered directly (200, ``Acp-Connection-Id`` header); everything else
  is answered ``202`` and its response is delivered on an SSE stream.
- ``GET`` opens an SSE stream: the connection's (``Acp-Connection-Id``) or a session's
  (plus ``Acp-Session-Id``). ``GET`` with ``Upgrade: websocket`` opens a WebSocket instead,
  carrying one JSON-RPC message per text frame.
- ``DELETE`` ends the connection.

Routing: a response goes where its request came from (the session named by the request,
or the connection), except ``session/load``, which is answered on the connection stream.
Agent requests and notifications naming a session go to that session's stream, others to
the connection's; the client's answers to agent requests must name the same session.

One deviation from the RFD: messages for a session the client cannot know yet (no
response, client message or stream has named it) go to the connection stream. Otherwise
an agent request made while handling ``session/new`` (e.g. running a command in a client
terminal) would wait for a session stream the client can only open after that response,
and the two would deadlock. Clients accept messages on either stream.
Streams buffer until a receiver attaches and allow one receiver at a time (409). Missed
messages are not replayed (ACP v1).

Each connection runs its own agent connection, like the WebSocket transport.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
import hmac
import logging
from typing import TYPE_CHECKING, Any
import uuid

import anyenv
import anyio
from anyio.abc import ByteReceiveStream, ByteSendStream


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from starlette.applications import Starlette
    from starlette.requests import HTTPConnection, Request
    from starlette.responses import Response
    from starlette.websockets import WebSocket


logger = logging.getLogger(__name__)

HEADER_CONNECTION_ID = "acp-connection-id"
HEADER_SESSION_ID = "acp-session-id"
JSON_MIME_TYPE = "application/json"
EVENT_STREAM_MIME_TYPE = "text/event-stream"
KEEPALIVE_SECONDS = 15.0
INITIALIZE_TIMEOUT_SECONDS = 60.0

SESSION_SCOPED_METHODS = frozenset({
    "session/cancel",
    "session/close",
    "session/delete",
    "session/fork",
    "session/load",
    "session/prompt",
    "session/resume",
    "session/set_config_option",
    "session/set_mode",
    "session/set_model",
})
"""Client methods whose POST must carry ``Acp-Session-Id``."""

type JsonMessage = dict[str, Any]
type Route = str | None
"""A session ID, or None for the connection stream."""
type ServeConnection = Callable[[ByteReceiveStream, ByteSendStream], Awaitable[None]]
"""Serves one ACP connection over newline-delimited JSON byte streams until EOF."""
type Authenticate = Callable[[HTTPConnection], Awaitable[str | None]]
"""Identifies the caller (None: rejected with 401). Connections belong to their creator."""


def bearer_token_auth(token: str) -> Authenticate:
    """Authenticate with ``Authorization: Bearer <token>``."""
    expected = f"Bearer {token}".encode()

    async def authenticate(conn: HTTPConnection) -> str | None:
        given = conn.headers.get("authorization", "").encode()
        return "bearer" if hmac.compare_digest(given, expected) else None

    return authenticate


def _id_key(message_id: object) -> str | None:
    match message_id:
        case bool():
            return None
        case str():
            return f"string:{message_id}"
        case int() | float():
            return f"number:{message_id}"
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


def _is_response(message: JsonMessage) -> bool:
    return "method" not in message and "id" in message


def _is_request(message: JsonMessage) -> bool:
    return "method" in message and "id" in message


class _Mailbox:
    """Outbound messages of one stream, buffered until its (single) receiver takes them."""

    def __init__(self) -> None:
        self._queue: deque[str] = deque()
        self._wakeup = asyncio.Event()
        self.has_receiver = False
        self.finished = False

    def push(self, text: str) -> None:
        if not self.finished:
            self._queue.append(text)
            self._wakeup.set()

    def finish(self) -> None:
        self.finished = True
        self._wakeup.set()

    async def receive(self, timeout: float) -> str | None:
        """Next message; "" on timeout (keep-alive), None once finished and drained."""
        while not self._queue:
            if self.finished:
                return None
            self._wakeup.clear()
            try:
                async with asyncio.timeout(timeout):
                    await self._wakeup.wait()
            except TimeoutError:
                return ""
        return self._queue.popleft()


class _AgentInput(ByteReceiveStream):
    """Client messages for the agent, as newline-delimited JSON."""

    def __init__(self, stream: MemoryObjectReceiveStream[bytes]) -> None:
        self._stream = stream

    async def receive(self, max_bytes: int = 65536) -> bytes:
        try:
            return await self._stream.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError) as exc:
            raise anyio.EndOfStream from exc

    async def aclose(self) -> None:
        await self._stream.aclose()


class _AgentOutput(ByteSendStream):
    """Splits the agent's newline-delimited JSON into messages for routing."""

    def __init__(self, deliver: Callable[[JsonMessage], None]) -> None:
        self._deliver = deliver
        self._buffer = b""

    async def send(self, item: bytes) -> None:
        self._buffer += item
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if not line.strip():
                continue
            try:
                message = anyenv.load_json(line, return_type=dict)
            except anyenv.JsonLoadError:
                logger.warning("Dropping non-JSON-object agent output: %.200r", line)
                continue
            self._deliver(message)

    async def aclose(self) -> None:
        pass


@dataclass
class _Connection:
    """One client connection and the agent connection serving it."""

    connection_id: str
    owner: str
    to_agent: MemoryObjectSendStream[bytes]
    connection_stream: _Mailbox = field(default_factory=_Mailbox)
    session_streams: dict[str, _Mailbox] = field(default_factory=dict)
    pending_routes: dict[str, Route] = field(default_factory=dict)
    """Client request keys -> where the agent's response goes."""
    client_response_routes: dict[str, Route] = field(default_factory=dict)
    """Agent request keys -> the session the client must answer in."""
    known_sessions: set[str] = field(default_factory=set)
    """Sessions the client can know: named by a response, a client message or a stream."""
    initial_response: asyncio.Future[JsonMessage] | None = None
    task: asyncio.Task[None] | None = None
    closed: bool = False

    def session_stream(self, session_id: str) -> _Mailbox:
        if (stream := self.session_streams.get(session_id)) is None:
            stream = _Mailbox()
            if self.closed:
                stream.finish()
            self.session_streams[session_id] = stream
        return stream

    def deliver(self, message: JsonMessage) -> None:
        """Route one agent message."""
        key = _id_key(message.get("id"))
        if _is_response(message):
            future = self.initial_response
            if future is not None and not future.done():
                future.set_result(message)
                return
            route = self.pending_routes.pop(key, None) if key is not None else None
            if (session_id := _result_session_id(message)) is not None:
                self.known_sessions.add(session_id)
        else:
            route = _params_session_id(message)
            # A session the client can't know yet (e.g. an agent request made while it
            # handles the session/new that creates it) has no stream the client could
            # open, so its messages use the connection stream instead of waiting forever.
            if route is not None and route not in self.known_sessions:
                route = None
            if _is_request(message) and key is not None:
                self.client_response_routes[key] = route
        stream = self.connection_stream if route is None else self.session_stream(route)
        stream.push(anyenv.dump_json(message))

    async def send_to_agent(self, message: JsonMessage) -> None:
        await self.to_agent.send(anyenv.dump_json(message).encode() + b"\n")

    def finish(self) -> None:
        self.closed = True
        self.connection_stream.finish()
        for stream in self.session_streams.values():
            stream.finish()
        if self.initial_response is not None and not self.initial_response.done():
            self.initial_response.set_exception(ConnectionError("Agent closed the connection"))


def _text(body: str, status: int) -> Response:
    from starlette.responses import PlainTextResponse

    return PlainTextResponse(body, status_code=status)


@dataclass
class AcpHttpServer:
    """ASGI app serving ACP over Streamable HTTP and WebSocket on one endpoint."""

    serve_connection: ServeConnection
    """Runs the agent for one connection."""
    path: str = "/acp"
    authenticate: Authenticate | None = None
    """Caller identification; None accepts everyone (local use only)."""
    allow_origins: list[str] = field(default_factory=list)
    """CORS origins for browser clients."""
    connections: dict[str, _Connection] = field(default_factory=dict)

    def app(self) -> Starlette:
        """The Starlette application."""
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware
        from starlette.routing import Route as HttpRoute, WebSocketRoute

        routes = [
            HttpRoute(self.path, self._http, methods=["POST", "GET", "DELETE"]),
            WebSocketRoute(self.path, self._websocket),
        ]
        middleware = []
        if self.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=self.allow_origins,
                    allow_methods=["GET", "POST", "DELETE"],
                    allow_headers=["*"],
                    expose_headers=["Acp-Connection-Id"],
                )
            )

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
            try:
                yield
            finally:
                await self.close()

        return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

    async def close(self) -> None:
        """End all connections."""
        for connection in list(self.connections.values()):
            await self._remove(connection)

    # --- connections ------------------------------------------------------------------

    def _open(self, owner: str) -> tuple[_Connection, MemoryObjectReceiveStream[bytes]]:
        send, receive = anyio.create_memory_object_stream[bytes](max_buffer_size=1024)
        connection = _Connection(connection_id=str(uuid.uuid4()), owner=owner, to_agent=send)
        self.connections[connection.connection_id] = connection
        return connection, receive

    def _start(
        self, connection: _Connection, agent_input: ByteReceiveStream, output: ByteSendStream
    ) -> None:
        async def run() -> None:
            try:
                await self.serve_connection(agent_input, output)
            except Exception:
                logger.exception("ACP connection %s failed", connection.connection_id)
            finally:
                connection.finish()
                self.connections.pop(connection.connection_id, None)

        connection.task = asyncio.create_task(run(), name=f"acp-http-{connection.connection_id}")

    async def _remove(self, connection: _Connection) -> None:
        self.connections.pop(connection.connection_id, None)
        connection.finish()
        await connection.to_agent.aclose()  # EOF: the agent connection closes
        if (task := connection.task) is not None:
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(task)
            except TimeoutError:
                task.cancel()

    async def _caller(self, conn: HTTPConnection) -> str | None:
        if self.authenticate is None:
            return "anonymous"
        return await self.authenticate(conn)

    def _owned(self, connection_id: str | None, owner: str) -> _Connection | None:
        """The caller's connection (other callers' connections look unknown)."""
        if connection_id is None:
            return None
        connection = self.connections.get(connection_id)
        return connection if connection is not None and connection.owner == owner else None

    # --- HTTP -------------------------------------------------------------------------

    async def _http(self, request: Request) -> Response:
        owner = await self._caller(request)
        if owner is None:
            return _text("Unauthorized", 401)
        match request.method:
            case "POST":
                return await self._post(request, owner)
            case "GET":
                return self._get(request, owner)
            case "DELETE":
                connection = self._owned(request.headers.get(HEADER_CONNECTION_ID), owner)
                if connection is None:
                    return _text("Unknown Acp-Connection-Id", 404)
                await self._remove(connection)
                return _text("", 202)
            case _:
                return _text("Method Not Allowed", 405)

    @staticmethod
    async def _read_message(request: Request) -> JsonMessage | Response:
        """The POSTed JSON-RPC message, or the error response for an invalid body."""
        from starlette.responses import JSONResponse

        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != JSON_MIME_TYPE:
            return _text("Unsupported Media Type", 415)
        try:
            message = anyenv.load_json(await request.body())
        except anyenv.JsonLoadError:
            return _text("Invalid JSON", 400)
        if isinstance(message, list):
            error = {"code": -32600, "message": "JSON-RPC batches are not supported"}
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": error}, status_code=400)
        if not isinstance(message, dict):
            return _text("Invalid JSON-RPC message", 400)
        return message

    async def _post(self, request: Request, owner: str) -> Response:
        message = await self._read_message(request)
        if not isinstance(message, dict):
            return message
        connection_id = request.headers.get(HEADER_CONNECTION_ID)
        if message.get("method") == "initialize" and _is_request(message):
            if connection_id is not None:
                return _text("Initialize not allowed on existing connection", 400)
            return await self._initialize(message, owner)
        return await self._forward(request, message, owner, connection_id)

    async def _forward(
        self, request: Request, message: JsonMessage, owner: str, connection_id: str | None
    ) -> Response:
        """Pass a message of an existing connection to its agent (202)."""
        if connection_id is None:
            return _text("Missing Acp-Connection-Id", 400)
        connection = self._owned(connection_id, owner)
        if connection is None or connection.closed:
            return _text("Unknown Acp-Connection-Id", 404)
        header_session = request.headers.get(HEADER_SESSION_ID)
        if _is_response(message):
            error = self._route_client_response(connection, message, header_session)
        else:
            error = self._route_client_message(connection, message, header_session)
        if error is not None:
            return _text(error, 400)
        await connection.send_to_agent(message)
        return _text("", 202)

    @staticmethod
    def _route_client_message(
        connection: _Connection, message: JsonMessage, header_session: str | None
    ) -> str | None:
        params_session = _params_session_id(message)
        method = message.get("method")
        needs_header = method in SESSION_SCOPED_METHODS or params_session is not None
        if needs_header and header_session is None:
            return "Missing Acp-Session-Id"
        if (
            header_session is not None
            and params_session is not None
            and header_session != params_session
        ):
            return "Mismatched Acp-Session-Id"
        route = header_session or params_session
        if route is not None:
            connection.known_sessions.add(route)
        if _is_request(message) and (key := _id_key(message.get("id"))) is not None:
            connection.pending_routes[key] = None if method == "session/load" else route
        return None

    @staticmethod
    def _route_client_response(
        connection: _Connection, message: JsonMessage, header_session: str | None
    ) -> str | None:
        key = _id_key(message.get("id"))
        if key is None or key not in connection.client_response_routes:
            return None
        route = connection.client_response_routes[key]
        if route is not None and header_session is None:
            return "Missing Acp-Session-Id"
        if route is not None and header_session != route:
            return "Mismatched Acp-Session-Id"
        del connection.client_response_routes[key]
        return None

    async def _initialize(self, message: JsonMessage, owner: str) -> Response:
        from starlette.responses import JSONResponse

        connection, agent_input = self._open(owner)
        connection.initial_response = asyncio.get_running_loop().create_future()
        self._start(connection, _AgentInput(agent_input), _AgentOutput(connection.deliver))
        try:
            await connection.send_to_agent(message)
            async with asyncio.timeout(INITIALIZE_TIMEOUT_SECONDS):
                response = await connection.initial_response
        except (TimeoutError, ConnectionError) as exc:
            await self._remove(connection)
            error = {"code": -32603, "message": "Initialize failed", "data": str(exc)}
            body = {"jsonrpc": "2.0", "id": message.get("id"), "error": error}
            return JSONResponse(body, status_code=500)
        connection.initial_response = None
        if "error" in response:
            await self._remove(connection)
            return JSONResponse(response, status_code=200)
        headers = {"Acp-Connection-Id": connection.connection_id}
        return JSONResponse(response, status_code=200, headers=headers)

    def _get(self, request: Request, owner: str) -> Response:
        from starlette.responses import StreamingResponse

        if EVENT_STREAM_MIME_TYPE not in request.headers.get("accept", "").lower():
            return _text("Not Acceptable", 406)
        connection_id = request.headers.get(HEADER_CONNECTION_ID)
        if connection_id is None:
            return _text("Missing Acp-Connection-Id", 400)
        connection = self._owned(connection_id, owner)
        if connection is None:
            return _text("Unknown Acp-Connection-Id", 404)
        session_id = request.headers.get(HEADER_SESSION_ID)
        if session_id is not None:
            connection.known_sessions.add(session_id)
        stream = (
            connection.connection_stream
            if session_id is None
            else connection.session_stream(session_id)
        )
        if stream.has_receiver:
            return _text("Outbound stream already has an active receiver", 409)
        stream.has_receiver = True

        async def events() -> AsyncIterator[bytes]:
            try:
                yield b":\n\n"
                while (text := await stream.receive(KEEPALIVE_SECONDS)) is not None:
                    yield f"data: {text}\n\n".encode() if text else b":\n\n"
            finally:
                stream.has_receiver = False

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type=EVENT_STREAM_MIME_TYPE, headers=headers)

    # --- WebSocket --------------------------------------------------------------------

    async def _websocket(self, websocket: WebSocket) -> None:
        from starlette.websockets import WebSocketDisconnect

        owner = await self._caller(websocket)
        if owner is None:
            await websocket.close(code=1008, reason="Unauthorized")
            return
        connection, agent_input = self._open(owner)

        def deliver(message: JsonMessage) -> None:
            outbox.put_nowait(anyenv.dump_json(message))

        outbox: asyncio.Queue[str] = asyncio.Queue()
        self._start(connection, _AgentInput(agent_input), _AgentOutput(deliver))
        await websocket.accept(headers=[(b"acp-connection-id", connection.connection_id.encode())])

        async def pump_out() -> None:
            while True:
                await websocket.send_text(await outbox.get())

        sender = asyncio.create_task(pump_out())
        try:
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                text = frame.get("text")
                if text is None:
                    continue  # Binary frames are ignored
                try:
                    message = anyenv.load_json(text)
                except anyenv.JsonLoadError:
                    continue
                if isinstance(message, list):
                    error = {"code": -32600, "message": "JSON-RPC batches are not supported"}
                    await outbox.put(
                        anyenv.dump_json({"jsonrpc": "2.0", "id": None, "error": error})
                    )
                    continue
                if isinstance(message, dict):
                    await connection.send_to_agent(message)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            await self._remove(connection)
