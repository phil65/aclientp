"""Tests for the pinned upstream schema references and the conformance check."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, ForwardRef, Literal

from pydantic import MISSING, BaseModel, Field, StringConstraints, model_validator
import pytest
from upathtools import UPath

from aclientp.schema.lenient import DefaultOnError
from aclientp.schema_reference import (
    REFERENCE_ROOT,
    Finding,
    OurSchema,
    Reference,
    UpstreamSchema,
    changelog,
    check,
    load_references,
    main,
    run_check,
    split_known,
    tag_sort_key,
    update_reference,
    wire_fields,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("protocol_version", [1, 2])
def test_models_conform_to_pinned_reference(protocol_version: int) -> None:
    report = run_check(protocol_version)
    new = "\n".join(f"{f.id}: {f.detail}" for f in report.new)
    stale = "\n".join(report.stale)
    assert report.ok, f"undocumented differences:\n{new}\nstale known differences:\n{stale}"


# --- Synthetic schema exercising the checker -------------------------------------

META: dict[str, Any] = {"agentMethods": {"session_prompt": "session/prompt"}}


def _schema(**defs: Any) -> dict[str, Any]:
    return {"$defs": defs}


CHUNK: dict[str, Any] = {
    "type": "object",
    "properties": {"content": {"type": "string"}, "messageId": {"type": "string"}},
    "required": ["content"],
}
UPDATE: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"sessionUpdate": {"type": "string", "const": "agent_message_chunk"}},
            "required": ["sessionUpdate"],
            "allOf": [{"$ref": "#/$defs/ContentChunk"}],
        }
    ]
}
KIND: dict[str, Any] = {
    "anyOf": [{"type": "string", "const": "read"}, {"title": "other", "type": "string"}]
}
# Open enum, the way aclientp.schema models them: known values plus any string.
OpenKind = Literal["read"] | str  # noqa: PYI051


class AgentMessageChunk(BaseModel):
    """Model matching the ``agent_message_chunk`` variant exactly."""

    session_update: Literal["agent_message_chunk"] = Field(
        default="agent_message_chunk", alias="sessionUpdate"
    )
    content: str
    message_id: str | None = Field(default=None, alias="messageId")


def _ours(model: type[BaseModel], kind: object) -> OurSchema:
    return OurSchema(
        models={model.__name__: model},
        aliases={
            "SessionUpdate": model,
            "ToolKind": kind,
            "AgentMethod": Literal["session/prompt"],
        },
    )


def _ids(findings: list[Finding]) -> set[str]:
    return {f.id for f in findings}


def test_variants_match_through_union_discriminator() -> None:
    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=CHUNK, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    findings = check(upstream, _ours(AgentMessageChunk, OpenKind), {})

    # ContentChunk is only an inlined base of the variant, so it needs no model.
    assert findings == []


def test_field_differences_are_reported() -> None:
    class Chunk(BaseModel):
        session_update: Literal["agent_message_chunk"] = Field(
            default="agent_message_chunk", alias="sessionUpdate"
        )
        content: str | None = None  # required upstream, may be left out
        extra: int = 0  # not upstream

    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=CHUNK, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    ids = _ids(check(upstream, _ours(Chunk, OpenKind), {}))

    variant = "SessionUpdate[sessionUpdate=agent_message_chunk]"
    assert ids == {
        f"missing-field:{variant}.messageId",
        f"extra-field:{variant}.extra",
        f"omittable-field:{variant}.content",
    }


def test_optional_upstream_field_required_here_is_reported() -> None:
    class Chunk(BaseModel):
        session_update: Literal["agent_message_chunk"] = Field(
            default="agent_message_chunk", alias="sessionUpdate"
        )
        content: str
        message_id: str = Field(alias="messageId")

    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=CHUNK, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    ids = _ids(check(upstream, _ours(Chunk, OpenKind), {}))

    assert ids == {"required-field:SessionUpdate[sessionUpdate=agent_message_chunk].messageId"}


def test_enum_and_method_differences_are_reported() -> None:
    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=CHUNK, SessionUpdate=UPDATE, ToolKind=KIND),
        {"agentMethods": {"session_prompt": "session/prompt", "session_new": "session/new"}},
    )

    ids = _ids(check(upstream, _ours(AgentMessageChunk, Literal["read", "edit"]), {}))

    assert ids == {
        "extra-enum-value:ToolKind.edit",
        "closed-enum:ToolKind",
        "missing-method:AgentMethod.session/new",
    }


def test_missing_variant_and_renames() -> None:
    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=CHUNK, SessionUpdate=UPDATE, ToolKind=KIND), META
    )
    ours = OurSchema(
        models={},
        aliases={"Kind": OpenKind, "AgentMethod": Literal["session/prompt"]},
    )

    ids = _ids(check(upstream, ours, {"ToolKind": "Kind"}))

    assert ids == {"missing-shape:SessionUpdate[sessionUpdate=agent_message_chunk]"}


def test_split_known_reports_new_and_stale() -> None:
    findings = [Finding("missing-field:A.b", ""), Finding("missing-field:A.c", "")]

    report = split_known(findings, {"missing-field:A.b": "known", "missing-field:A.gone": "fixed"})

    assert [f.id for f in report.new] == ["missing-field:A.c"]
    assert report.stale == {"missing-field:A.gone": "fixed"}
    assert not report.ok


def test_changelog_lists_changes() -> None:
    old = UpstreamSchema.parse(_schema(ContentChunk=CHUNK, ToolKind=KIND), META)
    chunk = {**CHUNK, "properties": {**CHUNK["properties"], "role": {"type": "string"}}}
    kind = {"anyOf": [*KIND["anyOf"], {"type": "string", "const": "edit"}]}
    new = UpstreamSchema.parse(
        _schema(ContentChunk=chunk, ToolKind=kind),
        {"agentMethods": {**META["agentMethods"], "session_new": "session/new"}},
    )

    assert changelog(old, new) == [
        "+ field ContentChunk.role",
        "+ enum value ToolKind.edit",
        "+ method agentMethods: session/new",
    ]


def test_tags_sort_by_version() -> None:
    tags = ["schema-v2.0.0", "schema-v2.0.0-alpha.17", "schema-v2.0.0-alpha.5", "schema-v1.33.0"]

    assert sorted(tags, key=tag_sort_key) == [
        "schema-v1.33.0",
        "schema-v2.0.0-alpha.5",
        "schema-v2.0.0-alpha.17",
        "schema-v2.0.0",
    ]


def test_nullability_differences_are_reported() -> None:
    nullable_chunk = {
        **CHUNK,
        "properties": {**CHUNK["properties"], "messageId": {"type": ["string", "null"]}},
    }

    class Chunk(BaseModel):
        session_update: Literal["agent_message_chunk"] = Field(
            default="agent_message_chunk", alias="sessionUpdate"
        )
        content: str | None  # upstream does not allow null
        message_id: str | MISSING = Field(default=MISSING, alias="messageId")  # rejects null

    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=nullable_chunk, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    ids = _ids(check(upstream, _ours(Chunk, OpenKind), {}, check_nullability=True))

    variant = "SessionUpdate[sessionUpdate=agent_message_chunk]"
    assert ids == {f"sends-null:{variant}.content", f"rejects-null:{variant}.messageId"}


def test_constraint_differences_are_reported() -> None:
    limited_chunk = {
        **CHUNK,
        "properties": {**CHUNK["properties"], "content": {"type": "string", "minLength": 1}},
    }

    class Chunk(BaseModel):
        session_update: Literal["agent_message_chunk"] = Field(
            default="agent_message_chunk", alias="sessionUpdate"
        )
        content: str  # lacks min_length=1
        message_id: str | None = Field(default=None, alias="messageId", max_length=5)

    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=limited_chunk, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    ids = _ids(check(upstream, _ours(Chunk, OpenKind), {}, check_constraints=True))

    variant = "SessionUpdate[sessionUpdate=agent_message_chunk]"
    assert ids == {f"constraint:{variant}.content", f"constraint:{variant}.messageId"}


def test_deserialize_annotation_differences_are_reported() -> None:
    lenient_chunk = {
        **CHUNK,
        "properties": {
            "content": {"type": "string"},
            "messageId": {"type": "string", "x-deserialize-default-on-error": True},
        },
    }

    class Chunk(BaseModel):
        session_update: Literal["agent_message_chunk"] = Field(
            default="agent_message_chunk", alias="sessionUpdate"
        )
        content: DefaultOnError[str]  # not lenient upstream
        message_id: str | None = Field(default=None, alias="messageId")  # lacks the marker

    class Marked(Chunk):
        content: str
        message_id: DefaultOnError[str | None] = Field(default=None, alias="messageId")

    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=lenient_chunk, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    variant = "SessionUpdate[sessionUpdate=agent_message_chunk]"
    ids = _ids(check(upstream, _ours(Chunk, OpenKind), {}))
    assert ids == {f"deserialize:{variant}.content", f"deserialize:{variant}.messageId"}
    assert check(upstream, _ours(Marked, OpenKind), {}) == []


def test_update_reference_moves_only_the_named_pin(tmp_path: Path) -> None:
    path = UPath(tmp_path / "references.toml")
    path.write_text(
        '[a]\nrepo = "x/a"\ncommit = "111"\npaths = ["src"]\n\n'
        '[b]\nrepo = "x/b"\ncommit = "222"\npaths = ["lib"]\n',
        encoding="utf-8",
    )

    update_reference("b", "333", path)

    pins = {r.name: r.commit for r in load_references(path)}
    assert pins == {"a": "111", "b": "333"}


def test_pinned_reference_implementations_are_valid() -> None:
    references = load_references()

    assert {r.name for r in references} >= {"typescript-sdk", "claude-agent-acp"}
    assert all(len(r.commit) == 40 and r.paths for r in references)


def test_parse_nullability_reports_only_rejected_nulls() -> None:
    nullable_chunk = {
        **CHUNK,
        "properties": {**CHUNK["properties"], "messageId": {"type": ["string", "null"]}},
    }

    class Chunk(BaseModel):
        session_update: Literal["agent_message_chunk"] = Field(
            default="agent_message_chunk", alias="sessionUpdate"
        )
        content: str | None  # would be sends-null in full mode
        message_id: str = Field(default="", alias="messageId")  # rejects null

    upstream = UpstreamSchema.parse(
        _schema(ContentChunk=nullable_chunk, SessionUpdate=UPDATE, ToolKind=KIND), META
    )

    ids = _ids(check(upstream, _ours(Chunk, OpenKind), {}, check_parse_nullability=True))

    assert ids == {"rejects-null:SessionUpdate[sessionUpdate=agent_message_chunk].messageId"}


def test_forward_references_are_resolved() -> None:
    class Holder(BaseModel):
        items: list[DefinedLater] | None = None  # type: ignore[name-defined]  # noqa: F821

    assert isinstance(Holder.model_fields["items"].annotation, ForwardRef)  # unresolved yet
    globals()["DefinedLater"] = int

    fields = wire_fields(Holder)

    assert fields["items"].accepts_none


# --- `not` and `if` / `then` constraints -------------------------------------------

MCP_MESSAGE: dict[str, Any] = {  # upstream #2988
    "type": "object",
    "properties": {
        "serverId": {"type": "string"},
        "sessionId": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "toolCallId": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["serverId"],
    "if": {"properties": {"toolCallId": {"type": "string"}}, "required": ["toolCallId"]},
    "then": {"properties": {"sessionId": {"type": "string"}}, "required": ["sessionId"]},
}
SET_CONFIG: dict[str, Any] = {  # upstream #2981
    "type": "object",
    "properties": {"configId": {"type": "string"}},
    "required": ["configId"],
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "boolean"},
                "value": {"type": "boolean"},
            },
            "required": ["type", "value"],
        },
        {
            "title": "value_id",
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "not": {"properties": {"type": {"const": "boolean"}}, "required": ["type"]},
        },
    ],
}


class McpMessage(BaseModel):
    server_id: str = Field(alias="serverId")
    session_id: str | None = Field(default=None, alias="sessionId")
    tool_call_id: str | None = Field(default=None, alias="toolCallId")


class StrictMcpMessage(McpMessage):
    @model_validator(mode="after")
    def _needs_session(self) -> StrictMcpMessage:
        if self.tool_call_id is not None and self.session_id is None:
            raise ValueError("toolCallId requires sessionId")
        return self


class SetConfig(BaseModel):
    config_id: str = Field(alias="configId")
    type: str | None = None
    value: str | bool


class StrictSetConfig(SetConfig):
    @model_validator(mode="after")
    def _boolean_needs_bool(self) -> StrictSetConfig:
        if self.type == "boolean" and not isinstance(self.value, bool):
            raise ValueError("boolean needs a bool")
        return self


def _probe_ids(model: type[BaseModel], name: str, definition: dict[str, Any]) -> set[str]:
    upstream = UpstreamSchema.parse(_schema(**{name: definition}), {})
    return _ids(check(upstream, OurSchema({name: model}, {}), {}))


def test_if_then_constraints_are_probed() -> None:
    upstream = UpstreamSchema.parse(_schema(MessageMcpRequest=MCP_MESSAGE), {})
    [probe] = upstream.probes
    assert probe.payload == {"serverId": "x", "toolCallId": "x"}
    assert probe.control == {"serverId": "x", "toolCallId": "x", "sessionId": "x"}

    loose = _probe_ids(McpMessage, "MessageMcpRequest", MCP_MESSAGE)
    strict = _probe_ids(StrictMcpMessage, "MessageMcpRequest", MCP_MESSAGE)

    assert loose == {"conditional:MessageMcpRequest.toolCallId->sessionId"}
    assert strict == set()


def test_not_constraints_are_probed() -> None:
    upstream = UpstreamSchema.parse(_schema(SetSessionConfigOptionRequest=SET_CONFIG), {})
    [probe] = upstream.probes
    assert probe.payload == {"configId": "x", "value": "x", "type": "boolean"}

    loose = _probe_ids(SetConfig, "SetSessionConfigOptionRequest", SET_CONFIG)
    strict = _probe_ids(StrictSetConfig, "SetSessionConfigOptionRequest", SET_CONFIG)

    assert loose == {"accepts-excluded:SetSessionConfigOptionRequest[type=boolean]"}
    assert strict == set()


def test_not_probe_is_skipped_when_another_branch_may_accept_it() -> None:
    # {"type": "start"} is excluded from the fallback, but the `start` variant accepts it.
    replay = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"type": {"type": "string", "const": "start"}},
                "required": ["type"],
            },
            {
                "type": "object",
                "properties": {"type": {"type": "string"}},
                "required": ["type"],
                "not": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"type": {"type": "string", "const": "start"}},
                            "required": ["type"],
                        }
                    ]
                },
            },
        ]
    }

    assert UpstreamSchema.parse(_schema(ReplayFrom=replay), {}).probes == ()


def test_control_rejected_by_the_models_is_reported() -> None:
    class NoNulls(BaseModel):
        server_id: str = Field(alias="serverId")
        tool_call_id: str | None = Field(default=None, alias="toolCallId")
        # rejects every sessionId, so the probe proves nothing
        session_id: None = Field(default=None, alias="sessionId")

    ids = _probe_ids(NoNulls, "MessageMcpRequest", MCP_MESSAGE)

    assert ids == {"unprobed:MessageMcpRequest.toolCallId->sessionId"}


@pytest.mark.parametrize("protocol_version", [1, 2])
def test_pinned_schemas_probe_2981_and_2988(protocol_version: int) -> None:
    ids = {probe.id for probe in Reference.load(protocol_version).parsed.probes}

    assert {
        "conditional:MessageMcpRequest.toolCallId->sessionId",
        "accepts-excluded:SetSessionConfigOptionRequest[type=boolean]",
        "accepts-excluded:InitialSessionConfigOption[type=boolean]",
    } <= ids


# --- CLI ------------------------------------------------------------------------


def test_check_reports_the_tag_of_the_checked_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = UPath(tmp_path)
    for name in ("schema.unstable.json", "meta.unstable.json", "conformance.toml"):
        target = root / "v1" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((REFERENCE_ROOT / "v1" / name).read_text("utf-8"), encoding="utf-8")
    (root / "v1" / "VERSION").write_text("schema-v1.99.0\n", encoding="utf-8")

    assert run_check(1, root).tag == "schema-v1.99.0"
    assert main(["check", "--root", str(tmp_path)]) == 0
    assert "ACP v1 models vs schema-v1.99.0" in capsys.readouterr().out


def test_constraints_behind_refs_and_in_optional_members_are_compared() -> None:
    key_def = {"type": "string", "minLength": 1, "maxLength": 255}
    prompt = {
        "type": "object",
        "properties": {"key": {"anyOf": [{"$ref": "#/$defs/Key"}, {"type": "null"}]}},
    }
    upstream = UpstreamSchema.parse(_schema(Key=key_def, Prompt=prompt), {})
    limited = Annotated[str, StringConstraints(min_length=1, max_length=255)]

    class Loose(BaseModel):
        key: str | MISSING | None = MISSING

    class Strict(BaseModel):
        key: limited | MISSING | None = MISSING  # type: ignore[valid-type]

    def ids(model: type[BaseModel]) -> set[str]:
        ours = OurSchema({"Prompt": model}, {})
        return _ids(check(upstream, ours, {}, check_constraints=True))

    assert ids(Loose) == {"constraint:Prompt.key"}
    assert ids(Strict) == set()
