"""v2 content blocks: text, media, resources and skill references."""

from collections.abc import Sequence  # noqa: TC003
from typing import Annotated, Literal

from pydantic import MISSING, Discriminator, Field, Tag

from aclientp.schema.lenient import DefaultOnError, SkipInvalidItems  # noqa: TC001
from aclientp.schema.v2.base import OTHER_TAG, AnnotatedObject, Unknown, open_discriminator
from aclientp.schema.v2.common import Annotations, Icon, MediaType  # noqa: TC001


class TextContent(AnnotatedObject):
    """Text provided to or from an LLM. All agents MUST support it in prompts."""

    type: Literal["text"] = Field(default="text", init=False)

    text: str
    """The text; clients SHOULD render it as Markdown."""

    annotations: DefaultOnError[Annotations | MISSING | None] = MISSING
    """Display and routing hints."""


class ImageContent(AnnotatedObject):
    """An image provided to or from an LLM. Requires the ``image`` prompt capability."""

    type: Literal["image"] = Field(default="image", init=False)

    data: str
    """Base64-encoded image data."""

    mime_type: MediaType
    """MIME type of the image."""

    uri: DefaultOnError[str | MISSING | None] = MISSING
    """URI associated with the image."""

    annotations: DefaultOnError[Annotations | MISSING | None] = MISSING
    """Display and routing hints."""


class AudioContent(AnnotatedObject):
    """Audio provided to or from an LLM. Requires the ``audio`` prompt capability."""

    type: Literal["audio"] = Field(default="audio", init=False)

    data: str
    """Base64-encoded audio data."""

    mime_type: MediaType
    """MIME type of the audio."""

    annotations: DefaultOnError[Annotations | MISSING | None] = MISSING
    """Display and routing hints."""


class ResourceLink(AnnotatedObject):
    """A link to a resource the receiver may be able to read. All agents MUST support it."""

    type: Literal["resource_link"] = Field(default="resource_link", init=False)

    name: str
    """Human-readable name."""

    uri: str
    """URI of the resource."""

    title: DefaultOnError[str | MISSING | None] = MISSING
    """Display title."""

    description: DefaultOnError[str | MISSING | None] = MISSING
    """Plain-text description of the linked resource."""

    icons: DefaultOnError[SkipInvalidItems[Sequence[Icon] | MISSING | None]] = MISSING
    """Icons a user interface can show."""

    mime_type: DefaultOnError[MediaType | MISSING | None] = MISSING
    """MIME type of the resource."""

    size: DefaultOnError[int | MISSING | None] = Field(default=MISSING, ge=0)
    """Size of the resource in bytes."""

    annotations: DefaultOnError[Annotations | MISSING | None] = MISSING
    """Display and routing hints."""


class TextResourceContents(AnnotatedObject):
    """Text contents of a resource."""

    text: str
    """The text."""

    uri: str
    """URI of the resource."""

    mime_type: DefaultOnError[MediaType | MISSING | None] = MISSING
    """MIME type of the resource."""


class BlobResourceContents(AnnotatedObject):
    """Binary contents of a resource."""

    blob: str
    """Base64-encoded bytes."""

    uri: str
    """URI of the resource."""

    mime_type: DefaultOnError[MediaType | MISSING | None] = MISSING
    """MIME type of the resource."""


EmbeddedResourceResource = TextResourceContents | BlobResourceContents
"""Contents of an embedded resource: text or binary."""


class EmbeddedResource(AnnotatedObject):
    """Resource contents embedded in a prompt or tool call result.

    Requires the ``embeddedContext`` prompt capability in prompts.
    """

    type: Literal["resource"] = Field(default="resource", init=False)

    resource: EmbeddedResourceResource
    """The embedded contents."""

    annotations: DefaultOnError[Annotations | MISSING | None] = MISSING
    """Display and routing hints."""


class SkillReference(AnnotatedObject):
    """**UNSTABLE**: A skill the user invokes with a prompt.

    Names a ``skill`` entry of the session's command list. Requires the ``skill``
    prompt capability.
    """

    type: Literal["skill"] = Field(default="skill", init=False)

    name: str
    """The ``name`` of a ``skill`` entry in the session's current command list."""


class UnknownContent(Unknown):
    """A custom or future content block; ``type`` values starting with ``_`` are extensions."""

    type: str
    """The content block type."""


ContentBlock = Annotated[
    Annotated[TextContent, Tag("text")]
    | Annotated[ImageContent, Tag("image")]
    | Annotated[AudioContent, Tag("audio")]
    | Annotated[ResourceLink, Tag("resource_link")]
    | Annotated[EmbeddedResource, Tag("resource")]
    | Annotated[SkillReference, Tag("skill")]
    | Annotated[UnknownContent, Tag(OTHER_TAG)],
    Discriminator(
        open_discriminator(
            "type",
            frozenset({"text", "image", "audio", "resource_link", "resource", "skill"}),
        )
    ),
]
"""Displayable content: in prompts, model output, tool call results and more."""
