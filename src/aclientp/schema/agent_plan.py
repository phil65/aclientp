from collections.abc import Sequence  # noqa: TC003
from typing import Annotated, Literal

from pydantic import Field

from aclientp.schema.base import AnnotatedObject
from aclientp.schema.lenient import DefaultOnError, SkipInvalidItems  # noqa: TC001


PlanEntryPriority = Literal["high", "medium", "low"]
PlanEntryStatus = Literal["pending", "in_progress", "completed"]


class PlanEntry(AnnotatedObject):
    """A single entry in the execution plan.

    Represents a task or goal that the assistant intends to accomplish
    as part of fulfilling the user's request.
    See protocol docs: [Plan Entries](https://agentclientprotocol.com/protocol/agent-plan#plan-entries)
    """

    content: str
    """Human-readable description of what this task aims to accomplish."""

    priority: PlanEntryPriority
    """The relative importance of this task.

    Used to indicate which tasks are most critical to the overall goal.
    """

    status: PlanEntryStatus
    """Current execution status of this task."""


PlanId = str
"""Agent-owned identifier of a plan, unique within the session."""


class PlanItems(AnnotatedObject):
    """A plan made of structured entries."""

    type: Literal["items"] = Field(default="items", init=False)

    plan_id: PlanId
    """The plan ID to update."""

    entries: DefaultOnError[SkipInvalidItems[Sequence[PlanEntry]]]
    """The complete list of tasks to be accomplished."""


class PlanMarkdown(AnnotatedObject):
    """A plan made of Markdown content."""

    type: Literal["markdown"] = Field(default="markdown", init=False)

    plan_id: PlanId
    """The plan ID to update."""

    content: str
    """The complete plan as Markdown, not just the text that changed."""

    path: DefaultOnError[str | None] = None
    """The absolute path of the file that holds this plan. Omitted and ``None`` mean the
    plan has no file; ``content`` is displayed either way."""


PlanUpdateContent = Annotated[PlanItems | PlanMarkdown, Field(discriminator="type")]
"""Content of an ID-addressed plan update."""
