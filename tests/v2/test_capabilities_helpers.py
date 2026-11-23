"""``supports()``: capability markers by dotted wire path."""

from __future__ import annotations

from aclientp.schema.v2 import AgentCapabilities, InitializeResponse, supports


def _caps(data: dict[str, object]) -> AgentCapabilities:
    response = InitializeResponse.model_validate({
        "protocolVersion": 2,
        "info": {"name": "a", "version": "1"},
        "capabilities": data,
    })
    assert isinstance(response.capabilities, AgentCapabilities)
    return response.capabilities


def test_supports_nested_markers() -> None:
    caps = _caps({
        "session": {"systemPrompt": {"mode": {"override": {}}}, "fork": {}},
        "providers": {},
    })

    assert supports(caps, "session.systemPrompt.mode.override")
    assert supports(caps, "session.fork")
    assert supports(caps, "providers")
    assert supports(caps, "session")


def test_missing_null_and_unknown_mean_unsupported() -> None:
    caps = _caps({"session": {"fork": None, "systemPrompt": {}}, "auth": None})

    assert not supports(caps, "session.fork")  # null
    assert not supports(caps, "session.delete")  # omitted
    assert not supports(caps, "session.systemPrompt.mode.override")  # omitted on the way
    assert not supports(caps, "auth.status")  # null on the way
    assert not supports(caps, "session.noSuchThing")
    assert not supports(None, "session")
