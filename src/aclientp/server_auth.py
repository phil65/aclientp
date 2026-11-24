"""Authentication and ``Origin`` checks shared by the ACP servers.

Agents usually run tools (shell, files) on the serving machine, so every server applies
the same rules (:class:`~aclientp.transports.WebSocketTransport`,
:class:`~aclientp.http_server.AcpHttpServer` and the bridges):

- Off loopback, a server needs ``authenticate`` (it refuses to start otherwise, unless
  explicitly opted out, e.g. behind an authenticating proxy). Callers it does not
  identify get 401 at the handshake, before any ACP message.
- Requests with an ``Origin`` header come from a browser page. They get 403 unless the
  origin is allowed: browsers apply no CORS to WebSockets, so without this any web page
  could connect to a local agent (cross-site WebSocket hijacking). Requests without
  ``Origin`` (non-browser clients) pass.

Tokens travel as ``Authorization: Bearer <token>``, for WebSocket too (on the upgrade
request). Query parameters are not accepted: they end up in logs and browser history.
Browsers cannot set headers on a WebSocket; browser clients use the Streamable HTTP
transport, whose ``fetch`` requests can.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
import hmac
import ipaddress
from typing import Protocol


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
"""Host names that only accept local connections (``is_loopback_host`` also takes
other loopback addresses)."""


class Handshake(Protocol):
    """What authentication sees of a request (Starlette's and websockets' requests)."""

    @property
    def headers(self) -> Mapping[str, str]: ...


type HandshakeAuthenticate = Callable[[Handshake], Awaitable[str | None]]
"""Identifies the caller from its request headers (None: rejected with 401)."""


class InsecureServerError(ValueError):
    """A server would expose agents to the network without authentication."""


def bearer_token_auth(token: str) -> HandshakeAuthenticate:
    """Authenticate with ``Authorization: Bearer <token>`` (constant-time comparison)."""
    if not token:
        msg = "The bearer token must not be empty"
        raise ValueError(msg)
    expected = f"Bearer {token}".encode()

    async def authenticate(request: Handshake) -> str | None:
        given = request.headers.get("authorization", "").encode()
        return "bearer" if hmac.compare_digest(given, expected) else None

    return authenticate


def is_loopback_host(host: str) -> bool:
    """Whether binding to ``host`` only accepts connections from this machine."""
    host = host.strip().removeprefix("[").removesuffix("]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def origin_allowed(origin: str | None, allow_origins: Sequence[str]) -> bool:
    """Whether a request with this ``Origin`` header may proceed.

    No ``Origin`` (a non-browser client) is allowed; a browser origin only if listed
    (``*`` allows every origin). ``null`` (sandboxed frames, ``file://``) is a browser
    origin like any other.
    """
    if origin is None:
        return True
    allowed = {_normalize_origin(o) for o in allow_origins}
    return "*" in allowed or _normalize_origin(origin) in allowed


def check_exposure(
    host: str, *, authenticated: bool, allow_unauthenticated: bool = False, what: str = "ACP"
) -> None:
    """Refuse to serve agents off loopback without authentication.

    Raises:
        InsecureServerError: ``host`` is not a loopback host, no authentication is
            configured, and ``allow_unauthenticated`` is not set.
    """
    if authenticated or allow_unauthenticated or is_loopback_host(host):
        return
    msg = (
        f"Serving {what} on {host!r} requires authentication (e.g. a bearer token): "
        "anyone reaching the port could drive the agents"
    )
    raise InsecureServerError(msg)
