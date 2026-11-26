"""Session update schema definitions."""

import base64
from collections.abc import Sequence  # noqa: TC003
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import Discriminator, Field, Tag

from aclientp.schema.agent_plan import PlanEntry, PlanId, PlanUpdateContent  # noqa: TC001
from aclientp.schema.base import AnnotatedObject
from aclientp.schema.capabilities import SubagentSessionCapabilities  # noqa: TC001
from aclientp.schema.common import Error  # noqa: TC001
from aclientp.schema.config_options import SessionConfigOption  # noqa: TC001
from aclientp.schema.content_blocks import (
    Annotations,
    Audience,
    AudioContentBlock,
    BlobResourceContents,
    ContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
    TextResourceContents,
)
from aclientp.schema.cursors import CursorAnchor, CursorCapabilities, CursorId  # noqa: TC001
from aclientp.schema.lenient import DefaultOnError, SkipInvalidItems  # noqa: TC001
from aclientp.schema.slash_commands import AvailableCommand  # noqa: TC001
from aclientp.schema.tool_call import (  # noqa: TC001
    ToolCallContent,
    ToolCallKind,
    ToolCallLocation,
)


if TYPE_CHECKING:
    from datetime import datetime


ToolCallStatus = Literal["pending", "in_progress", "completed", "failed"]


class BaseChunk(AnnotatedObject):
    """Base class for all session update chunks."""

    content: ContentBlock
    """A single item of content"""

    message_id: DefaultOnError[str | None] = None
    """A unique identifier for the message this chunk belongs to.

    All chunks belonging to the same message share the same ``message_id``.
    A change in ``message_id`` indicates a new message has started.
    Both clients and agents MUST use UUID format for message IDs.
    """

    @classmethod
    def text(
        cls,
        text: str,
        *,
        message_id: str | None = None,
        audience: Audience | None = None,
        last_modified: datetime | str | None = None,
        priority: float | None = None,
    ) -> Self:
        """Create a chunk containing text.

        Args:
            text: The text content.
            message_id: Unique identifier for the message this chunk belongs to.
            audience: The audience for the text.
            last_modified: The last modified date of the text.
            priority: The priority of the text.
        """
        annotations = Annotations.optionally_create(
            audience=audience,
            last_modified=last_modified,
            priority=priority,
        )
        return cls(
            content=TextContentBlock(text=text, annotations=annotations),
            message_id=message_id,
        )

    @classmethod
    def image(
        cls,
        data: str | bytes,
        mime_type: str,
        uri: str | None = None,
        *,
        message_id: str | None = None,
        audience: Audience | None = None,
        last_modified: datetime | str | None = None,
        priority: float | None = None,
    ) -> Self:
        """Create a chunk containing an embedded image resource.

        Args:
            data: The image data.
            mime_type: The MIME type of the image.
            uri: The URI of the image.
            message_id: Unique identifier for the message this chunk belongs to.
            audience: The audience for the image.
            last_modified: The last modified date of the image.
            priority: The priority of the image.
        """
        if isinstance(data, bytes):
            data = base64.b64encode(data).decode()
        annotations = Annotations.optionally_create(
            audience=audience,
            last_modified=last_modified,
            priority=priority,
        )
        image = ImageContentBlock(
            data=data,
            mime_type=mime_type,
            uri=uri,
            annotations=annotations,
        )
        return cls(content=image, message_id=message_id)

    @classmethod
    def audio(
        cls,
        data: str | bytes,
        mime_type: str,
        *,
        message_id: str | None = None,
        audience: Audience | None = None,
        last_modified: datetime | str | None = None,
        priority: float | None = None,
    ) -> Self:
        """Create a chunk containing audio content.

        Args:
            data: The audio data.
            mime_type: The MIME type of the audio data.
            message_id: Unique identifier for the message this chunk belongs to.
            audience: The audience for the audio chunk.
            last_modified: The last modified date of the audio chunk.
            priority: The priority of the audio chunk.
        """
        if isinstance(data, bytes):
            data = base64.b64encode(data).decode()
        annotations = Annotations.optionally_create(
            audience=audience,
            last_modified=last_modified,
            priority=priority,
        )
        content = AudioContentBlock(
            data=data,
            mime_type=mime_type,
            annotations=annotations,
        )
        return cls(content=content, message_id=message_id)

    @classmethod
    def resource(
        cls,
        name: str,
        uri: str,
        description: str | None = None,
        mime_type: str | None = None,
        size: int | None = None,
        title: str | None = None,
        *,
        message_id: str | None = None,
        audience: Audience | None = None,
        last_modified: datetime | str | None = None,
        priority: float | None = None,
    ) -> Self:
        """Create a chunk containing a resource content block.

        Args:
            name: The name of the resource.
            uri: The URI of the resource.
            description: The description of the resource.
            mime_type: The MIME type of the resource.
            size: The size of the resource.
            title: The title of the resource.
            message_id: Unique identifier for the message this chunk belongs to.
            audience: The audience of the resource.
            last_modified: The last modified date of the resource.
            priority: The priority of the resource.
        """
        annotations = Annotations.optionally_create(
            audience=audience,
            last_modified=last_modified,
            priority=priority,
        )
        block = ResourceContentBlock(
            name=name,
            uri=uri,
            description=description,
            mime_type=mime_type,
            size=size,
            title=title,
            annotations=annotations,
        )
        return cls(content=block, message_id=message_id)

    @classmethod
    def embedded_text_resource(
        cls,
        text: str,
        uri: str,
        mime_type: str | None = None,
        *,
        message_id: str | None = None,
        audience: Audience | None = None,
        last_modified: datetime | str | None = None,
        priority: float | None = None,
    ) -> Self:
        """Create a chunk containing an embedded text resource.

        Args:
            text: The text to embed.
            uri: The URI of the resource.
            mime_type: The MIME type of the resource.
            message_id: Unique identifier for the message this chunk belongs to.
            audience: The audience to apply to the resource.
            last_modified: The last modified date of the resource.
            priority: The priority of the resource.
        """
        annotations = Annotations.optionally_create(
            audience=audience,
            last_modified=last_modified,
            priority=priority,
        )
        contents = TextResourceContents(text=text, mime_type=mime_type, uri=uri)
        content = EmbeddedResourceContentBlock(annotations=annotations, resource=contents)
        return cls(content=content, message_id=message_id)

    @classmethod
    def embedded_blob_resource(
        cls,
        data: bytes | str,
        uri: str,
        mime_type: str | None = None,
        *,
        message_id: str | None = None,
        audience: Audience | None = None,
        last_modified: datetime | str | None = None,
        priority: float | None = None,
    ) -> Self:
        """Create a chunk containing an embedded blob resource.

        Args:
            data: The data to embed.
            uri: The URI of the resource.
            mime_type: The MIME type of the resource.
            message_id: Unique identifier for the message this chunk belongs to.
            audience: The audience to apply to the resource.
            last_modified: The last modified date of the resource.
            priority: The priority of the resource.
        """
        if isinstance(data, bytes):
            data = base64.b64encode(data).decode()
        annotations = Annotations.optionally_create(
            audience=audience,
            last_modified=last_modified,
            priority=priority,
        )
        resource = BlobResourceContents(blob=data, mime_type=mime_type, uri=uri)
        content = EmbeddedResourceContentBlock(annotations=annotations, resource=resource)
        return cls(content=content, message_id=message_id)


class UserMessageChunk(BaseChunk):
    """A chunk of the user's message being streamed."""

    session_update: Literal["user_message_chunk"] = Field(default="user_message_chunk", init=False)
    """User message chunk."""


class AgentMessageChunk(BaseChunk):
    """A chunk of the agent's response being streamed."""

    session_update: Literal["agent_message_chunk"] = Field(
        default="agent_message_chunk", init=False
    )
    """Agent message chunk."""


class AgentThoughtChunk(BaseChunk):
    """A chunk of the agent's internal reasoning being streamed."""

    session_update: Literal["agent_thought_chunk"] = Field(
        default="agent_thought_chunk", init=False
    )
    """Agent thought chunk."""


class ToolCallProgress(AnnotatedObject):
    """Update on the status or results of a tool call."""

    session_update: Literal["tool_call_update"] = Field(default="tool_call_update", init=False)
    """Tool call update."""

    content: DefaultOnError[SkipInvalidItems[Sequence[ToolCallContent] | None]] = None
    """Replace the content collection."""

    kind: DefaultOnError[ToolCallKind | None] = None
    """Update the tool kind."""

    locations: DefaultOnError[SkipInvalidItems[Sequence[ToolCallLocation] | None]] = None
    """Replace the locations collection."""

    raw_input: DefaultOnError[Any | None] = None
    """Update the raw input."""

    raw_output: DefaultOnError[Any | None] = None
    """Update the raw output."""

    status: DefaultOnError[ToolCallStatus | None] = None
    """Update the execution status."""

    title: DefaultOnError[str | None] = None
    """Update the human-readable title."""

    name: DefaultOnError[str | None] = None
    """Update the programmatic name of the tool being invoked."""

    tool_call_id: str
    """The ID of the tool call being updated."""


class CurrentModeUpdate(AnnotatedObject):
    """The current mode of the session has changed.

    See protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)
    """

    current_mode_id: str
    """The ID of the current mode"""

    session_update: Literal["current_mode_update"] = Field(
        default="current_mode_update", init=False
    )


class AgentPlanUpdate(AnnotatedObject):
    """The agent's execution plan for complex tasks.

    See protocol docs: [Agent Plan](https://agentclientprotocol.com/protocol/agent-plan).
    """

    session_update: Literal["plan"] = Field(default="plan", init=False)

    entries: DefaultOnError[SkipInvalidItems[Sequence[PlanEntry]]]
    """The list of tasks to be accomplished.

    When updating a plan, the agent must send a complete list of all entries
    with their current status. The client replaces the entire plan with each update."""


class AvailableCommandsUpdate(AnnotatedObject):
    """Available commands are ready or have changed."""

    session_update: Literal["available_commands_update"] = Field(
        default="available_commands_update", init=False
    )
    """Available commands are ready or have changed."""

    available_commands: DefaultOnError[SkipInvalidItems[Sequence[AvailableCommand]]]
    """Commands the agent can execute"""


class ConfigOptionUpdate(AnnotatedObject):
    """A session configuration option value has changed.

    The full set of configuration options and their current values is sent
    on every change. Clients should replace their cached list entirely.

    See protocol docs: `Session Config Options <https://agentclientprotocol.com/protocol/session-config-options>`_
    """

    session_update: Literal["config_option_update"] = Field(
        default="config_option_update", init=False
    )

    config_options: DefaultOnError[SkipInvalidItems[Sequence[SessionConfigOption]]]
    """The full set of configuration options and their current values."""


class ToolCallStart(AnnotatedObject):
    """Notification that a new tool call has been initiated."""

    session_update: Literal["tool_call"] = Field(default="tool_call", init=False)
    """Notification that a new tool call has been initiated."""

    content: DefaultOnError[SkipInvalidItems[Sequence[ToolCallContent] | None]] = None
    """Content produced by the tool call."""

    kind: DefaultOnError[ToolCallKind | None] = None
    """The category of tool being invoked.

    Helps clients choose appropriate icons and UI treatment.
    """

    locations: DefaultOnError[SkipInvalidItems[Sequence[ToolCallLocation] | None]] = None
    """File locations affected by this tool call.

    Enables "follow-along" features in clients.
    """

    raw_input: DefaultOnError[Any | None] = None
    """Raw input parameters sent to the tool."""

    raw_output: DefaultOnError[Any | None] = None
    """Raw output returned by the tool."""

    status: DefaultOnError[ToolCallStatus | None] = None
    """Current execution status of the tool call."""

    title: str
    """Human-readable title describing what the tool is doing."""

    name: DefaultOnError[str | None] = None
    """Programmatic name of the tool being invoked."""

    tool_call_id: str
    """Unique identifier for this tool call within the session."""


class Cost(AnnotatedObject):
    """Cost information for a session."""

    amount: float
    """Total cumulative cost for session."""

    currency: str
    """Unit of ``amount``: an ISO 4217 currency code (e.g. ``USD``), or e.g. ``credits``."""


class Usage(AnnotatedObject):
    """**UNSTABLE**: Token usage information for a prompt turn."""

    total_tokens: int = Field(ge=0)
    """Sum of all token types across session."""

    input_tokens: int = Field(ge=0)
    """Total input tokens across all turns."""

    output_tokens: int = Field(ge=0)
    """Total output tokens across all turns."""

    thought_tokens: DefaultOnError[int | None] = Field(default=None, ge=0)
    """Total thought/reasoning tokens."""

    cached_read_tokens: DefaultOnError[int | None] = Field(default=None, ge=0)
    """Total cache read tokens."""

    cached_write_tokens: DefaultOnError[int | None] = Field(default=None, ge=0)
    """Total cache write tokens."""


class UsageUpdate(AnnotatedObject):
    """Context window and cost update for a session."""

    session_update: Literal["usage_update"] = Field(default="usage_update", init=False)

    used: int = Field(ge=0)
    """Tokens currently in context."""

    size: int = Field(ge=0)
    """Total context window size in tokens."""

    cost: DefaultOnError[Cost | None] = None
    """Cumulative session cost (optional)."""


class SessionInfoUpdate(AnnotatedObject):
    """Incremental update to session metadata.

    Used to notify clients of changes to session info (title, timestamps)
    without requiring a full session list refresh.

    Fields that are None are not changed. To update a field, set it to a value.
    """

    session_update: Literal["session_info_update"] = Field(
        default="session_info_update", init=False
    )

    title: DefaultOnError[str | None] = None
    """New title for the session, or None to leave unchanged."""

    updated_at: DefaultOnError[str | None] = None
    """New ISO 8601 timestamp, or None to leave unchanged."""


NoticeSeverity = Literal["info", "warning", "error"] | str
"""Severity hint for a session notice."""


class Notice(AnnotatedObject):
    """Fire-and-forget information for the user.

    Only sent if the client advertised ``session.notices``.
    """

    session_update: Literal["notice"] = Field(default="notice", init=False)

    severity: NoticeSeverity
    """Presentation severity hint."""

    title: str
    """Required non-empty plain-text title that can stand alone."""

    description: DefaultOnError[str | None] = None
    """Optional plain-text detail or guidance."""


CompactionId = str
"""Agent-owned ID of a context compaction, unique within the session."""

CompactionStatus = Literal["in_progress", "completed", "failed", "cancelled"] | str
"""Lifecycle state of a context compaction."""


class CompactionUpdate(AnnotatedObject):
    """A context compaction upsert.

    The first notification fixes the compaction's timeline position.
    Later updates with the same ID patch that entity in place.
    Only sent if the client advertised ``session.compaction``.
    """

    session_update: Literal["compaction_update"] = Field(default="compaction_update", init=False)

    compaction_id: CompactionId
    """The Agent-owned ID of this compaction."""

    status: CompactionStatus
    """Current lifecycle status."""

    summary: DefaultOnError[SkipInvalidItems[Sequence[ContentBlock] | None]] = None
    """Complete replacement user-displayable summary content for the compaction."""

    error: DefaultOnError[str | None] = None
    """Human-readable error details for the compaction."""


class CompactionSummaryChunk(AnnotatedObject):
    """A content block appended to a compaction's summary.

    A first-seen ID creates an in-progress compaction. Chunks append in receive order.
    """

    session_update: Literal["compaction_summary_chunk"] = Field(
        default="compaction_summary_chunk", init=False
    )

    compaction_id: CompactionId
    """ID of the compaction whose summary receives this content."""

    content: ContentBlock
    """One content block to append."""


class PlanUpdate(AnnotatedObject):
    """Create or replace an ID-addressed plan.

    Only sent if the client advertised the ``plan`` capability.
    """

    session_update: Literal["plan_update"] = Field(default="plan_update", init=False)

    plan: PlanUpdateContent
    """Replaces the whole plan with this ``plan_id``, even if its ``type`` changes."""


class PlanRemoved(AnnotatedObject):
    """Remove an ID-addressed plan.

    Only sent if the client advertised the ``plan`` capability.
    """

    session_update: Literal["plan_removed"] = Field(default="plan_removed", init=False)

    plan_id: PlanId
    """The plan ID to remove. Clients MUST ignore a removal for an unknown ID."""


class PromptSuggestion(AnnotatedObject):
    """**UNSTABLE**: A prompt the client can offer to the user."""

    prompt: Sequence[ContentBlock]
    """The content of the suggested prompt."""


class PromptSuggestionsUpdate(AnnotatedObject):
    """**UNSTABLE**: Suggested next prompts, replacing any previous suggestions.

    Only sent if the client advertised ``session.prompt_suggestions``.
    """

    session_update: Literal["prompt_suggestions_update"] = Field(
        default="prompt_suggestions_update", init=False
    )

    suggestions: SkipInvalidItems[Sequence[PromptSuggestion]]
    """The suggestions the Client can offer, ordered most likely first."""


class RunningStateUpdate(AnnotatedObject):
    """Foreground work is in progress."""

    state: Literal["running"] = Field(default="running", init=False)


class IdleStateUpdate(AnnotatedObject):
    """The child is ready to process another prompt."""

    state: Literal["idle"] = Field(default="idle", init=False)

    stop_reason: DefaultOnError[
        Literal["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled", "error"]
        | str
        | None
    ] = None
    """Why foreground work stopped. Omitted when there is no stop reason."""

    error: DefaultOnError[Error | None] = None
    """For ``stop_reason: "error"``: the failure, as a JSON-RPC error object."""

    usage: DefaultOnError[Usage | None] = None
    """Token usage for completed foreground work."""


class RequiresActionStateUpdate(AnnotatedObject):
    """Foreground work is blocked on user action."""

    state: Literal["requires_action"] = Field(default="requires_action", init=False)


class UnknownStateUpdate(AnnotatedObject):
    """The Agent cannot currently determine foreground activity.

    Also used for custom or future states, whose raw value is kept in ``state``.
    """

    state: str = "unknown"


def _state_discriminator(v: Any) -> str:
    state = v.get("state") if isinstance(v, dict) else getattr(v, "state", None)
    return state if state in {"running", "idle", "requires_action"} else "unknown"


StateUpdate = Annotated[
    Annotated[RunningStateUpdate, Tag("running")]
    | Annotated[IdleStateUpdate, Tag("idle")]
    | Annotated[RequiresActionStateUpdate, Tag("requires_action")]
    | Annotated[UnknownStateUpdate, Tag("unknown")],
    Discriminator(_state_discriminator),
]
"""State snapshot of a subagent session."""


class SubagentUpdate(AnnotatedObject):
    """A subagent session started by the parent, or a change to it.

    Only sent if the client advertised the ``subagents`` capability.
    """

    session_update: Literal["subagent_update"] = Field(default="subagent_update", init=False)

    session_id: str
    """The opaque session ID identifying the child in all ACP messages."""

    title: DefaultOnError[str | None] = None
    """The parent's human-readable display title for this child."""

    description: DefaultOnError[str | None] = None
    """The parent's description of the child's role or purpose."""

    capabilities: DefaultOnError[SubagentSessionCapabilities | None] = None
    """Client-initiated session mutations permitted for this subagent session."""

    state: DefaultOnError[StateUpdate | None] = None
    """Current state snapshot for the child session."""

    cwd: str | None = None
    """The child's working directory. Must be an absolute path."""

    additional_directories: Sequence[str] | None = None
    """Additional workspace roots for the child. Each path must be absolute."""


class SessionMessage(AnnotatedObject):
    """A message exchanged between sessions (e.g. parent and subagent).

    Only sent if the client advertised the ``subagents`` capability.
    Upserts by ``message_id``: omitted fields are left unchanged, ``None`` clears them.
    """

    session_update: Literal["session_message"] = Field(default="session_message", init=False)

    message_id: str
    """Identifier of this message within the enclosing session's transcript."""

    sender_session_id: DefaultOnError[str | None] = None
    """The sending session."""

    recipient_session_id: DefaultOnError[str | None] = None
    """The receiving session."""

    sender_message_id: DefaultOnError[str | None] = None
    """On an incoming view, the ``message_id`` of the outgoing view in the sender's transcript."""

    recipient_message_id: DefaultOnError[str | None] = None
    """On an outgoing view, the ``message_id`` of the incoming view in the recipient."""

    content: DefaultOnError[SkipInvalidItems[Sequence[ContentBlock] | None]] = None
    """Replaces all content when non-empty. ``None`` or ``[]`` clears it."""


class SessionMessageChunk(AnnotatedObject):
    """A content block appended to a ``session_message``.

    Only sent if the client advertised the ``subagents`` capability.
    """

    session_update: Literal["session_message_chunk"] = Field(
        default="session_message_chunk", init=False
    )

    message_id: str
    """Identifier of this message within the enclosing session's transcript."""

    content: ContentBlock
    """A single content block appended to the message."""


class CursorUpdate(AnnotatedObject):
    """**UNSTABLE**: A session cursor was announced or its capabilities changed.

    Upserts by ``cursor_id``. The first update for a cursor MUST include ``anchor``,
    which is fixed afterwards; later updates change only ``capabilities``.
    Only sent if the client advertised ``session.cursors``.
    """

    session_update: Literal["cursor_update"] = Field(default="cursor_update", init=False)

    cursor_id: CursorId
    """The Agent-owned ID of this cursor, unique within the session."""

    anchor: CursorAnchor | None = None
    """Where the cursor is. Required on the first update for a cursor."""

    capabilities: DefaultOnError[CursorCapabilities | None] = None
    """The methods that currently accept this cursor; a new cursor starts with none."""


SessionUpdate = Annotated[
    (
        UserMessageChunk
        | AgentMessageChunk
        | AgentThoughtChunk
        | ToolCallStart
        | ToolCallProgress
        | AvailableCommandsUpdate
        | AgentPlanUpdate
        | PlanUpdate
        | PlanRemoved
        | CurrentModeUpdate
        | ConfigOptionUpdate
        | SessionInfoUpdate
        | UsageUpdate
        | Notice
        | CompactionUpdate
        | CompactionSummaryChunk
        | PromptSuggestionsUpdate
        | SubagentUpdate
        | SessionMessage
        | SessionMessageChunk
        | CursorUpdate
    ),
    Field(discriminator="session_update"),
]
