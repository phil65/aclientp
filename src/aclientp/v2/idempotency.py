"""**UNSTABLE**: Deduplicating v2 ``session/prompt`` retries by ``idempotencyKey``.

The Client sends the same ``idempotencyKey`` on every attempt of one submission. An
Agent that advertises ``capabilities.session`` MUST insert at most one user message per
key and session, and answer a retry with the ``messageId`` of the message it already
inserted, without starting another turn. It MUST recognize the key for as long as it
retains the message, across connections and restarts, so the record belongs in the
same storage as the session.

Invalid keys (empty, longer than 255 characters, not a string) already fail validation
of :class:`~aclientp.schema.v2.PromptRequest`, which the connection answers with
``-32602``.

:class:`IdempotencyStore` maps ``(session_id, idempotency_key)`` to the ``messageId``
and serializes concurrent attempts with the same key. Records live in an
:class:`IdempotencyBackend`: :class:`InMemoryIdempotencyBackend` by default, or one
backed by the agent's session storage::

    store = IdempotencyStore(MySessionStorageBackend(storage))


    async def prompt(self, params: PromptRequest) -> PromptResponse:
        async with store.claim(params.session_id, params.idempotency_key) as claim:
            if claim.message_id is not None:  # a retry: already inserted
                return PromptResponse(message_id=claim.message_id)
            message_id = insert_user_message(params)  # may raise: nothing is recorded
            await claim.record(message_id)
        start_turn(params.session_id, message_id)
        return PromptResponse(message_id=message_id)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anyio
from pydantic import MISSING


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@runtime_checkable
class IdempotencyBackend(Protocol):
    """Where :class:`IdempotencyStore` keeps its records.

    Back it with the agent's session storage so keys survive a restart, and drop a
    session's records together with the session.
    """

    async def get(self, session_id: str, key: str) -> str | None:
        """The ``messageId`` recorded for ``key`` in the session, or ``None``."""
        ...

    async def put(self, session_id: str, key: str, message_id: str) -> None:
        """Record ``message_id`` as the message inserted for ``key`` in the session."""
        ...


class InMemoryIdempotencyBackend:
    """Records kept in memory: enough for live-only sessions, lost on restart."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, str]] = {}

    async def get(self, session_id: str, key: str) -> str | None:
        return self._records.get(session_id, {}).get(key)

    async def put(self, session_id: str, key: str, message_id: str) -> None:
        self._records.setdefault(session_id, {})[key] = message_id

    def forget_session(self, session_id: str) -> None:
        """Drop the records of a session (e.g. when it is deleted)."""
        self._records.pop(session_id, None)


@dataclass
class PromptClaim:
    """One attempt of a ``session/prompt`` submission, held by :meth:`IdempotencyStore.claim`."""

    session_id: str
    key: str | None
    """The ``idempotencyKey``; ``None`` when the request had none (no deduplication)."""

    message_id: str | None
    """The message already inserted for this key (the attempt is a retry), else ``None``."""

    _store: IdempotencyStore = field(repr=False)

    @property
    def is_retry(self) -> bool:
        """Whether a message was already inserted for this key."""
        return self.message_id is not None

    async def record(self, message_id: str) -> None:
        """Record the inserted message; call it before answering the request.

        Without a key there is nothing to record. Raises ``RuntimeError`` for a retry,
        which must not insert a second message.
        """
        if self.message_id is not None:
            msg = f"idempotency key already recorded for message {self.message_id!r}"
            raise RuntimeError(msg)
        if self.key is not None:
            await self._store.backend.put(self.session_id, self.key, message_id)
        self.message_id = message_id


class IdempotencyStore:
    """Maps ``(session_id, idempotency_key)`` to the ``messageId`` inserted for it."""

    def __init__(self, backend: IdempotencyBackend | None = None) -> None:
        self.backend: IdempotencyBackend = backend or InMemoryIdempotencyBackend()
        self._locks: dict[tuple[str, str], tuple[anyio.Lock, int]] = {}

    async def lookup(self, session_id: str, key: str) -> str | None:
        """The ``messageId`` recorded for ``key`` in the session, or ``None``."""
        return await self.backend.get(session_id, key)

    @asynccontextmanager
    async def claim(
        self, session_id: str, key: str | MISSING | None
    ) -> AsyncGenerator[PromptClaim]:
        """Hold ``key`` for one attempt; ``claim.message_id`` is set for a retry.

        Concurrent attempts with the same key and session wait for each other, so a
        retry that arrives while the first attempt is still inserting gets its
        ``messageId`` instead of inserting again. If the body raises before
        :meth:`PromptClaim.record`, nothing is recorded and the next attempt is a new
        submission. ``MISSING`` and ``None`` mean no key: the claim never deduplicates.
        """
        if key is MISSING or key is None:
            yield PromptClaim(session_id, None, None, self)
            return
        slot = (session_id, key)
        lock, users = self._locks.get(slot, (anyio.Lock(), 0))
        self._locks[slot] = (lock, users + 1)
        try:
            async with lock:
                recorded = await self.backend.get(session_id, key)
                yield PromptClaim(session_id, key, recorded, self)
        finally:
            lock, users = self._locks[slot]
            if users == 1:
                del self._locks[slot]
            else:
                self._locks[slot] = (lock, users - 1)
