"""Minimal v1 + v2 ACP agent fixture for ``acp-tck`` conformance tests.

Exercises ``aclientp``'s version router, v1/v2 agent runtimes, idempotency store,
and stdio / Streamable HTTP transports against the official ACP Test Compatibility Kit.
"""

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any
import uuid

from pydantic import MISSING

from aclientp.agent.protocol import Agent as V1Agent
from aclientp.exceptions import RequestError
from aclientp.schema import (
    PROTOCOL_VERSION as V1_PROTOCOL_VERSION,
    AgentCapabilities as V1AgentCapabilities,
    AgentMessageChunk as V1AgentMessageChunk,
    CancelNotification as V1CancelNotification,
    Implementation as V1Implementation,
    InitializeRequest as V1InitializeRequest,
    InitializeResponse as V1InitializeResponse,
    NewSessionRequest as V1NewSessionRequest,
    NewSessionResponse as V1NewSessionResponse,
    PromptRequest as V1PromptRequest,
    PromptResponse as V1PromptResponse,
    ResourceContentBlock as V1ResourceLink,
    SessionNotification as V1SessionNotification,
    TextContentBlock as V1TextContentBlock,
)
from aclientp.schema.v2 import (
    AgentCapabilities as V2AgentCapabilities,
    AgentMessageChunk as V2AgentMessageChunk,
    CancelSessionNotification as V2CancelNotification,
    CloseSessionRequest as V2CloseSessionRequest,
    CloseSessionResponse as V2CloseSessionResponse,
    IdleStateSessionUpdate,
    Implementation as V2Implementation,
    InitializeRequest as V2InitializeRequest,
    InitializeResponse as V2InitializeResponse,
    JsonValue,
    ListSessionsRequest as V2ListSessionsRequest,
    ListSessionsResponse as V2ListSessionsResponse,
    NewSessionRequest as V2NewSessionRequest,
    NewSessionResponse as V2NewSessionResponse,
    PromptRequest as V2PromptRequest,
    PromptResponse as V2PromptResponse,
    ReplayComplete,
    ReplayFromStart,
    ResourceLink as V2ResourceLink,
    ResumeSessionRequest as V2ResumeSessionRequest,
    ResumeSessionResponse as V2ResumeSessionResponse,
    RunningStateSessionUpdate,
    SessionCapabilities as V2SessionCapabilities,
    SessionInfo as V2SessionInfo,
    SessionInfoUpdate as V2SessionInfoUpdate,
    TextContent as V2TextContent,
    UpdateSessionNotification as V2UpdateNotification,
    UserMessage as V2UserMessage,
)
from aclientp.transports import serve
from aclientp.v2.agent import Agent as V2Agent
from aclientp.v2.idempotency import IdempotencyStore


if TYPE_CHECKING:
    from aclientp.agent.connection import AgentSideConnection as V1AgentSideConnection
    from aclientp.v2.agent import AgentSideConnection as V2AgentSideConnection


CANCEL_MARKER = "TCK_CANCEL"


def _assert_absolute_cwd(cwd: str) -> None:
    if not (PurePosixPath(cwd).is_absolute() or PureWindowsPath(cwd).is_absolute()):
        raise RequestError.invalid_params({"cwd": cwd})


class V1TckAgent(V1Agent):
    """Minimal conformant ACP v1 agent built on ``aclientp``."""

    def __init__(self, conn: V1AgentSideConnection) -> None:
        self.conn = conn
        self._sessions: set[str] = set()
        self._cancels: dict[str, asyncio.Event] = {}

    async def initialize(self, params: V1InitializeRequest) -> V1InitializeResponse:
        return V1InitializeResponse(
            protocol_version=min(params.protocol_version, V1_PROTOCOL_VERSION),
            agent_capabilities=V1AgentCapabilities(),
            agent_info=V1Implementation(name="aclientp-tck", version="1.0"),
        )

    async def new_session(self, params: V1NewSessionRequest) -> V1NewSessionResponse:
        _assert_absolute_cwd(params.cwd)
        session_id = f"v1-{uuid.uuid4().hex[:12]}"
        self._sessions.add(session_id)
        return V1NewSessionResponse(session_id=session_id)

    async def prompt(self, params: V1PromptRequest) -> V1PromptResponse:
        if params.session_id not in self._sessions:
            raise RequestError.session_not_found(params.session_id)
        for block in params.prompt:
            if not isinstance(block, (V1TextContentBlock, V1ResourceLink)):
                raise RequestError.invalid_params({"block": getattr(block, "type", None)})
        text = " ".join(b.text for b in params.prompt if isinstance(b, V1TextContentBlock))
        cancel_event = asyncio.Event()
        self._cancels[params.session_id] = cancel_event
        try:
            if CANCEL_MARKER in text:
                await self.conn.session_update(
                    V1SessionNotification(
                        session_id=params.session_id,
                        update=V1AgentMessageChunk.text("Working..."),
                    )
                )
                await cancel_event.wait()
                return V1PromptResponse(stop_reason="cancelled")
            await self.conn.session_update(
                V1SessionNotification(
                    session_id=params.session_id,
                    update=V1AgentMessageChunk.text("Hello from aclientp."),
                )
            )
            return V1PromptResponse(stop_reason="end_turn")
        finally:
            if self._cancels.get(params.session_id) is cancel_event:
                del self._cancels[params.session_id]

    async def cancel(self, params: V1CancelNotification) -> None:
        if event := self._cancels.get(params.session_id):
            event.set()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)


@dataclass
class _V2Session:
    session_id: str
    cwd: str
    title: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    cancel_events: list[asyncio.Event] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class V2TckAgent(V2Agent):
    """Minimal conformant ACP v2 agent built on ``aclientp``."""

    def __init__(self, conn: V2AgentSideConnection) -> None:
        self.conn = conn
        self._sessions: dict[str, _V2Session] = {}
        self._idempotency = IdempotencyStore()

    async def initialize(self, params: V2InitializeRequest) -> V2InitializeResponse:
        return V2InitializeResponse(
            protocol_version=2,
            info=V2Implementation(name="aclientp-tck", version="1.0"),
            capabilities=V2AgentCapabilities(session=V2SessionCapabilities()),
        )

    async def new_session(self, params: V2NewSessionRequest) -> V2NewSessionResponse:
        _assert_absolute_cwd(params.cwd)
        if params.mcp_servers not in (None, MISSING) and params.mcp_servers:
            raise RequestError.invalid_params({"mcpServers": "unsupported transport"})
        session_id = f"v2-{uuid.uuid4().hex[:12]}"
        self._sessions[session_id] = _V2Session(session_id=session_id, cwd=params.cwd)
        return V2NewSessionResponse(session_id=session_id)

    async def resume_session(self, params: V2ResumeSessionRequest) -> V2ResumeSessionResponse:
        _assert_absolute_cwd(params.cwd)
        if params.mcp_servers not in (None, MISSING) and params.mcp_servers:
            raise RequestError.invalid_params({"mcpServers": "unsupported transport"})
        session = self._sessions.get(params.session_id)
        if session is None:
            raise RequestError.session_not_found(params.session_id)
        if params.replay_from not in (None, MISSING):
            if not isinstance(params.replay_from, ReplayFromStart):
                raise RequestError.invalid_params({"replayFrom": "only start is supported"})
            await self._send(params.session_id, ReplayComplete())
        running = session.task is not None and not session.task.done()
        await self._send(
            params.session_id,
            RunningStateSessionUpdate() if running else IdleStateSessionUpdate(),
        )
        await self._send(
            params.session_id,
            V2SessionInfoUpdate(title=session.title, updated_at=session.updated_at),
        )
        return V2ResumeSessionResponse()

    async def close_session(self, params: V2CloseSessionRequest) -> V2CloseSessionResponse:
        session = self._sessions.pop(params.session_id, None)
        if session is not None:
            for event in list(session.cancel_events):
                event.set()
            if session.task is not None and not session.task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await session.task
        return V2CloseSessionResponse()

    async def list_sessions(self, params: V2ListSessionsRequest) -> V2ListSessionsResponse:
        cwd = params.cwd if isinstance(params.cwd, str) else None
        if cwd is not None:
            _assert_absolute_cwd(cwd)
        if params.cursor not in (None, MISSING):
            raise RequestError.invalid_params({"cursor": params.cursor})
        items = [
            V2SessionInfo(
                session_id=s.session_id,
                cwd=s.cwd,
                title=s.title,
                updated_at=s.updated_at,
            )
            for s in self._sessions.values()
            if cwd is None or s.cwd == cwd
        ]
        return V2ListSessionsResponse(sessions=items)

    async def prompt(self, params: V2PromptRequest) -> V2PromptResponse:
        session = self._sessions.get(params.session_id)
        if session is None:
            raise RequestError.session_not_found(params.session_id)
        for block in params.prompt:
            if not isinstance(block, (V2TextContent, V2ResourceLink)):
                raise RequestError.invalid_params({"block": getattr(block, "type", None)})
        async with self._idempotency.claim(params.session_id, params.idempotency_key) as claim:
            if claim.message_id is not None:
                return V2PromptResponse(message_id=claim.message_id)
            message_id = f"u-{uuid.uuid4().hex[:12]}"
            await claim.record(message_id)

        text = " ".join(b.text for b in params.prompt if isinstance(b, V2TextContent))
        if session.title is None and text:
            session.title = text[:60]
        session.updated_at = datetime.now(UTC).isoformat()

        previous = session.task
        was_running = previous is not None and not previous.done()
        cancel_event = asyncio.Event()
        session.cancel_events.append(cancel_event)

        async def _commit_and_start() -> None:
            await self._send(
                params.session_id,
                V2UserMessage(message_id=message_id, content=params.prompt),
            )
            if not was_running:
                await self._send(params.session_id, RunningStateSessionUpdate())
            session.task = asyncio.create_task(
                self._run_turn(session, text, cancel_event, previous, was_running)
            )

        # Once the prompt is claimed & emitted, $/cancel_request must not turn the reply into -32800
        commit_task = asyncio.create_task(_commit_and_start())
        while not commit_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(commit_task)
        return V2PromptResponse(message_id=message_id)

    async def _run_turn(
        self,
        session: _V2Session,
        text: str,
        cancel_event: asyncio.Event,
        previous: asyncio.Task[None] | None,
        was_running: bool,
    ) -> None:
        if previous is not None and not previous.done():
            with contextlib.suppress(asyncio.CancelledError):
                await previous
        reply_id = f"a-{uuid.uuid4().hex[:12]}"
        try:
            if was_running:
                await self._send(session.session_id, RunningStateSessionUpdate())
            if cancel_event.is_set():
                await self._send(
                    session.session_id, IdleStateSessionUpdate(stop_reason="cancelled")
                )
                return
            if CANCEL_MARKER in text:
                await self._send(
                    session.session_id,
                    V2AgentMessageChunk(
                        message_id=reply_id, content=V2TextContent(text="Working...")
                    ),
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(cancel_event.wait(), 2.0)
                if cancel_event.is_set():
                    await self._send(
                        session.session_id, IdleStateSessionUpdate(stop_reason="cancelled")
                    )
                    return
            await self._send(
                session.session_id,
                V2AgentMessageChunk(
                    message_id=reply_id, content=V2TextContent(text="Hello from aclientp.")
                ),
            )
            await self._send(session.session_id, IdleStateSessionUpdate(stop_reason="end_turn"))
        finally:
            if cancel_event in session.cancel_events:
                session.cancel_events.remove(cancel_event)

    async def cancel(self, params: V2CancelNotification) -> None:
        session = self._sessions.get(params.session_id)
        if session is not None:
            for event in list(session.cancel_events):
                event.set()

    async def ext_method(self, method: str, params: JsonValue) -> JsonValue:
        raise RequestError.method_not_found(method)

    async def _send(self, session_id: str, update: Any) -> None:
        await self.conn.session_update(V2UpdateNotification(session_id=session_id, update=update))


def main() -> None:
    import logging
    import os
    import sys

    import structlog

    os.environ.setdefault("LOGFIRE_IGNORE_NO_CONFIG", "1")
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    asyncio.run(serve(lambda conn: V1TckAgent(conn), v2_agent=lambda conn: V2TckAgent(conn)))


if __name__ == "__main__":
    main()
