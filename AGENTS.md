# AGENTS.md

`aclientp` implements the [Agent Client Protocol](https://agentclientprotocol.com) (ACP) in typed
Python, agent and client side, for v1 and the draft v2. Architecture, usage and the upstream sync
workflow: `README.md` ("Staying in sync with upstream") and the docs in `docs/`.

## Specs

| Spec | URL | Targets | Code | Update check |
|---|---|---|---|---|
| ACP v1 | https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v1.35.0 | `schema-v1.35.0` (`schema/v1/VERSION`, vendored `schema/v1/*.json`) | `src/aclientp/schema/`, `src/aclientp/agent/`, `src/aclientp/client/` | `uv run python -m aclientp.schema_reference latest` |
| ACP v2 (draft) | https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v2.0.0-beta.4 | `schema-v2.0.0-beta.4` (`schema/v2/VERSION`, vendored `schema/v2/*.json`) | `src/aclientp/schema/v2/`, `src/aclientp/v2/`, `src/aclientp/router.py` | `uv run python -m aclientp.schema_reference --protocol 2 latest` |
| ACP reference implementations | https://github.com/agentclientprotocol/typescript-sdk | pinned by commit in `schema/references.toml` | `src/aclientp/router.py`, `src/aclientp/v2/` | `uv run python -m aclientp.schema_reference refs` |
| ACP Streamable HTTP & WebSocket transport (RFD, experimental) | https://agentclientprotocol.com/rfds/streamable-http-websocket-transport | unpinned (tracks latest) | `src/aclientp/http_transport.py`, `src/aclientp/http_server.py`, `src/aclientp/ws_transport.py` | https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/rfds/streamable-http-websocket-transport.mdx |
| ACP registry | https://github.com/agentclientprotocol/registry | registry `v1` (`REGISTRY_URL` in `src/aclientp/registry/fetch.py`) | `src/aclientp/registry/` | https://github.com/agentclientprotocol/registry |
