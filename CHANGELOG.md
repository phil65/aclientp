# Changelog

## 0.5.0

Follows upstream `schema-v1.35.0` and `schema-v2.0.0-beta.4`.

- **Behaviour change (stricter validation):**
  - v1 `McpServer` is selected by `type`: only a missing `type` or `"stdio"` selects
    `StdioMcpServer`. Unknown transports and malformed known ones (e.g.
    `{"type": "http", "url": 42, "command": ...}`) fail validation (`-32602` on
    `session/new` and friends) instead of falling back to stdio, like the Rust SDK.
  - v1 `SetSessionConfigOptionRequest` and `InitialSessionConfigOption` reject
    `type: "boolean"` with a non-boolean `value` (upstream #2981). The models keep
    their loose shape.
  - `MessageMcpRequest` (v1 and v2): `toolCallId` requires `sessionId` (upstream #2988).
  - v2 `IdempotencyKey` must be 1 to 255 characters; other keys fail validation of
    `PromptRequest` (`-32602`), never treated as omitted.
- `MessageMcpRequest` (v1 and v2) gains optional `session_id` / `tool_call_id`, naming
  the session and tool call an `mcp/message` request was sent for (upstream #2988).
- `null` is accepted, and read as `[]`, for the initial `configOptions` of session setup
  (v1 new/load/resume/fork, v2 new/resume/fork) and for `additionalDirectories` on
  `session/options` (v1 and v2; upcoming upstream #2976).
- Requests and notifications without params leave out `params` instead of sending
  `"params": null`, which JSON-RPC does not allow (upstream #2920).
- New `aclientp.v2.idempotency`: `IdempotencyStore` with a pluggable backend
  (`InMemoryIdempotencyBackend` by default) for deduplicating `session/prompt` retries
  by `idempotencyKey`, as agents advertising `capabilities.session` must (upstream #2847).
  See "Prompt retries" in the README.
- Docs: dropped the **UNSTABLE** markers of the now stable v1 subagent, plan operation
  and state types (upstream #2939, #2898); removed the outdated "a malformed value counts
  as no controls" notes (#2891, #2946); v2 notes which `description` fields may contain
  Markdown (#2974, #2978).
- Conformance check: probes `not` and `if`/`then` constraints with payloads upstream
  rejects, compares limits behind `$ref`s and in optional constrained types, `check
  --root DIR` checks another reference directory and the report names its tag (it named
  the pinned one). The probes found a few long-standing differences, now listed in
  `conformance.toml` (v1 elicitation requests are one loose model, v1 elicitation
  property schemas have no fallback variant, the v2 `Error` model does not require
  `data` for `-32022`).
- `schema/references.toml`: the TypeScript SDK and claude-agent-acp pins moved to their
  current heads.

## 0.4.0

- **Behaviour change:** a request now returns (or raises the peer's error) only once
  the notifications received before its response have been handled, on the client and
  the agent side, for every transport. Before, the response could be delivered while
  earlier notifications (e.g. a replay's or a new session's first updates) were still
  being handled; callers had to call `wait_for_notifications()` themselves. See
  "Message ordering" in the README. A notification handler that waits for something
  only done after a later request returns now deadlocks.
- The v1 `ClientSideConnection.prompt` no longer waits separately for the turn's
  updates (the guarantee covers it).
