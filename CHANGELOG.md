# Changelog

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
