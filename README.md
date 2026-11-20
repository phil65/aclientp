# AClientP

[![PyPI License](https://img.shields.io/pypi/l/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Package status](https://img.shields.io/pypi/status/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Monthly downloads](https://img.shields.io/pypi/dm/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Distribution format](https://img.shields.io/pypi/format/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Wheel availability](https://img.shields.io/pypi/wheel/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Python version](https://img.shields.io/pypi/pyversions/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Implementation](https://img.shields.io/pypi/implementation/aclientp.svg)](https://pypi.org/project/aclientp/)
[![Releases](https://img.shields.io/github/downloads/phil65/aclientp/total.svg)](https://github.com/phil65/aclientp/releases)
[![Github Contributors](https://img.shields.io/github/contributors/phil65/aclientp)](https://github.com/phil65/aclientp/graphs/contributors)
[![Github Discussions](https://img.shields.io/github/discussions/phil65/aclientp)](https://github.com/phil65/aclientp/discussions)
[![Github Forks](https://img.shields.io/github/forks/phil65/aclientp)](https://github.com/phil65/aclientp/forks)
[![Github Issues](https://img.shields.io/github/issues/phil65/aclientp)](https://github.com/phil65/aclientp/issues)
[![Github Issues](https://img.shields.io/github/issues-pr/phil65/aclientp)](https://github.com/phil65/aclientp/pulls)
[![Github Watchers](https://img.shields.io/github/watchers/phil65/aclientp)](https://github.com/phil65/aclientp/watchers)
[![Github Stars](https://img.shields.io/github/stars/phil65/aclientp)](https://github.com/phil65/aclientp/stars)
[![Github Repository size](https://img.shields.io/github/repo-size/phil65/aclientp)](https://github.com/phil65/aclientp)
[![Github last commit](https://img.shields.io/github/last-commit/phil65/aclientp)](https://github.com/phil65/aclientp/commits)
[![Github release date](https://img.shields.io/github/release-date/phil65/aclientp)](https://github.com/phil65/aclientp/releases)
[![Github language count](https://img.shields.io/github/languages/count/phil65/aclientp)](https://github.com/phil65/aclientp)
[![Github commits this month](https://img.shields.io/github/commit-activity/m/phil65/aclientp)](https://github.com/phil65/aclientp)
[![Package status](https://codecov.io/gh/phil65/aclientp/branch/main/graph/badge.svg)](https://codecov.io/gh/phil65/aclientp/)
[![PyUp](https://pyup.io/repos/github/phil65/aclientp/shield.svg)](https://pyup.io/repos/github/phil65/aclientp/)

[Read the documentation!](https://phil65.github.io/aclientp/)

**AClientP** implements the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP, the protocol between code editors and AI coding agents) in typed Python: both
the agent and the client side, for the released **v1** protocol and the **draft v2**.

The name sets it apart from the other "ACP"s (Agent Communication Protocol,
Agent Connect Protocol, Agentic Commerce Protocol): **a**gent **client** **p**rotocol.

## Why another ACP library?

- **Checked against upstream.** The models are hand-written, but every release pins
  an upstream schema release (`schema/v1`, `schema/v2`) and a conformance check
  compares the models with it: fields, required/optional, nullability, limits, enums,
  union variants and methods. Known differences are documented with a reason.
- **v1 and v2.** v2 lives next to v1 (`aclientp.schema.v2`, `aclientp.v2`), and
  `RoutedAgentConnection` serves a client with the v1 or v2 agent, chosen by its
  `initialize` request, like the official TypeScript SDK's router.
- **Typed throughout.** Pydantic models, `mypy --strict`, discriminated unions with
  fallback variants that keep unknown fields (so custom and future messages survive a
  round trip).
- **Small core.** pydantic, anyio, anyenv, httpx, structlog and upathtools; web
  servers, WebSockets and tracing are extras.

## Installation

```bash
uv add aclientp
```

| Extra | Adds |
| --- | --- |
| `websocket` | WebSocket transport |
| `http2` | HTTP/2 for the Streamable HTTP client |
| `bridge` | HTTP server transport, `aclientp-bridge`: expose a stdio agent over HTTP / WebSocket |
| `debug` | `aclientp-debug-server`: a mock agent with a web UI |
| `tracing` | logfire spans for handled requests |

## Quick start

### A v1 agent over stdio

```python
import anyio

from aclientp import Agent, run_agent
from aclientp.schema import (
    InitializeRequest,
    InitializeResponse,
    NewSessionRequest,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
)


class EchoAgent(Agent):
    async def initialize(self, params: InitializeRequest) -> InitializeResponse:
        return InitializeResponse(protocol_version=1)

    async def new_session(self, params: NewSessionRequest) -> NewSessionResponse:
        return NewSessionResponse(session_id="session-1")

    async def prompt(self, params: PromptRequest) -> PromptResponse:
        return PromptResponse(stop_reason="end_turn")


anyio.run(run_agent, EchoAgent())
```

### Serving v1 and v2

```python
from aclientp.router import RoutedAgentConnection
from aclientp.stdio import stdio_streams


async def main() -> None:
    stdin, stdout = await stdio_streams()
    conn = RoutedAgentConnection(stdout, stdin, v1=MyV1Agent(), v2=MyV2Agent)
    await conn.wait_closed()
```

A v2 agent subclasses `aclientp.v2.agent.Agent` and overrides the methods it supports;
the rest answer `-32601`. v2 models follow the v2 patch rules: optional fields default
to `MISSING` and are left out, `None` is sent as `null`. Serialize with `to_wire` and
test fields with `is_set` (`MISSING` is truthy).

### Remote agents over Streamable HTTP (experimental)

`connect_http_transport` is the client side of ACP's Streamable HTTP transport (the
"Streamable HTTP & WebSocket Transport" RFD), compatible with the TypeScript SDK's
experimental server. It yields byte streams like a stdio agent's:

```python
from aclientp import connect_http_transport
from aclientp.client.connection import ClientSideConnection


async with connect_http_transport(url, headers={"X-API-Key": key}) as (reader, writer):
    conn = ClientSideConnection(lambda agent: MyClient(), writer, reader)
    await conn.initialize(InitializeRequest(protocol_version=1))
```

Serving an agent the same way (Streamable HTTP and WebSocket on one endpoint, compatible
with the TypeScript SDK's experimental clients):

```python
from aclientp import HttpTransport, serve
from aclientp.http_server import bearer_token_auth


auth = bearer_token_auth(token)
await serve(lambda conn: MyAgent(conn), HttpTransport(host="0.0.0.0", port=8765, authenticate=auth))
```

`aclientp-bridge -- my-agent --flag` serves a stdio agent this way, one subprocess per
connection.

For clients that only launch local agents, `aclientp-connect` relays stdio to a remote
agent. Header values may reference environment variables:

```json
{
  "command": "uvx",
  "args": ["--from", "aclientp[http2]", "aclientp-connect", "https://example.com/api/acp",
           "-H", "X-API-Key: ${MY_API_KEY}"],
  "env": { "MY_API_KEY": "..." }
}
```

## Staying in sync with upstream

```bash
uv run python -m aclientp.schema_reference latest           # pinned vs newest tag
uv run python -m aclientp.schema_reference sync --dry-run   # changelog of the newest tag
uv run python -m aclientp.schema_reference sync             # pin it
uv run python -m aclientp.schema_reference check            # models vs pinned schema
uv run python -m aclientp.schema_reference refs             # changes in reference implementations
```

Use `--protocol 2` for v2. `schema/<version>/conformance.toml` lists renames and known
differences; the test suite runs the same check, so undocumented drift fails CI.
`schema/references.toml` pins reference implementations (the official TypeScript SDK
and claude-agent-acp) by commit; `refs` lists the upstream commits since each pin with
a compare link.

## Status

v1 follows `schema-v1.33.0`, v2 follows the draft `schema-v2.0.0-alpha.17`. The v2
API is **unstable** and follows the draft as it changes.
