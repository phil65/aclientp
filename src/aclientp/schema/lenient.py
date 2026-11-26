"""Lenient deserialization, as declared by the schema's ``x-deserialize-*`` annotations.

The ACP schema marks fields whose bad values must not fail a whole message:

- ``x-deserialize-default-on-error``: an invalid value (an unknown enum value, a
  malformed ``_meta``) is replaced by the field's default: :data:`DefaultOnError`.
- ``x-deserialize-skip-invalid-items``: invalid items of a list (an unknown content
  block or config option type) are dropped, the rest is kept: :data:`SkipInvalidItems`.
- ``x-deserialize-default-on-null``: ``null`` reads as an empty list or mapping
  (the field's type is one): :data:`DefaultOnNull`.

The markers wrap a field's whole annotation. Combined, :data:`DefaultOnError` goes
outermost, so a list whose bad items can be dropped is not replaced as a whole::

    content: DefaultOnError[SkipInvalidItems[list[ToolCallContent] | None]] = None

Model instances are never replaced or dropped: an invalid one was built by local code
(a programming error, e.g. the wrong model in a list), so it raises (upstream #204).

``schema_reference check`` compares the markers with the schema's annotations.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import inspect
import types
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUseDefault, core_schema


if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import GetCoreSchemaHandler, ValidationInfo


__all__ = [
    "DefaultOnError",
    "DefaultOnNull",
    "LenientMarker",
    "SkipInvalidItems",
    "coerce_protocol_version",
]

type DeserializeKind = Literal["default-on-error", "skip-invalid-items", "default-on-null"]


def _is_local(value: Any) -> bool:
    """Whether ``value`` is (or holds) a model instance, i.e. was built by local code."""
    if isinstance(value, BaseModel):
        return True
    return isinstance(value, list | tuple) and any(isinstance(v, BaseModel) for v in value)


def _default_on_error(value: Any, handler: Any, info: ValidationInfo) -> Any:
    try:
        return handler(value)
    except ValidationError:
        if _is_local(value):
            raise
        raise PydanticUseDefault from None


def _skip_invalid_items(value: Any, handler: Any, info: ValidationInfo) -> Any:
    if not isinstance(value, list):
        return handler(value)
    try:
        return handler(value)
    except ValidationError:
        if _is_local(value):
            raise
    valid = []
    for item in value:
        try:
            handler([item])
        except ValidationError:
            continue
        valid.append(item)
    return handler(valid)


def _empty_value(source: Any) -> list[Any] | dict[str, Any] | None:
    """The empty container of a (possibly optional) list or mapping type."""
    if isinstance(source, TypeAliasType):
        return _empty_value(source.__value__)
    origin = get_origin(source)
    if origin is Annotated:
        return _empty_value(get_args(source)[0])
    if origin is Union or isinstance(source, types.UnionType):
        return next(
            (empty for arg in get_args(source) if (empty := _empty_value(arg)) is not None), None
        )
    if inspect.isclass(origin or source):
        if issubclass(origin or source, Mapping):
            return {}
        if issubclass(origin or source, Sequence) and not issubclass(origin or source, str):
            return []
    return None


def _default_on_null(source: Any) -> Callable[[Any, Any, ValidationInfo], Any]:
    """``null`` reads as the empty list or mapping (serde's ``Default``) of the type."""
    empty = _empty_value(source)

    def validate(value: Any, handler: Any, info: ValidationInfo) -> Any:
        if value is None:
            if empty is None:
                raise PydanticUseDefault
            return handler(type(empty)())
        return handler(value)

    return validate


@dataclass(frozen=True, slots=True)
class LenientMarker:
    """``Annotated`` metadata for one ``x-deserialize-*`` annotation."""

    kind: DeserializeKind

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        match self.kind:
            case "default-on-error":
                function = _default_on_error
            case "skip-invalid-items":
                function = _skip_invalid_items
            case "default-on-null":
                function = _default_on_null(source)
        return core_schema.with_info_wrap_validator_function(function, handler(source))


DEFAULT_ON_ERROR = LenientMarker("default-on-error")
SKIP_INVALID_ITEMS = LenientMarker("skip-invalid-items")
DEFAULT_ON_NULL = LenientMarker("default-on-null")


if TYPE_CHECKING:
    type DefaultOnError[T] = T
    type SkipInvalidItems[T] = T
    type DefaultOnNull[T] = T
else:

    class DefaultOnError:
        """``x-deserialize-default-on-error``: an invalid value reads as the default."""

        def __class_getitem__(cls, item: Any) -> Any:
            return Annotated[item, DEFAULT_ON_ERROR]

    class SkipInvalidItems:
        """``x-deserialize-skip-invalid-items``: invalid list items are dropped."""

        def __class_getitem__(cls, item: Any) -> Any:
            return Annotated[item, SKIP_INVALID_ITEMS]

    class DefaultOnNull:
        """``x-deserialize-default-on-null``: ``null`` reads as an empty list or mapping."""

        def __class_getitem__(cls, item: Any) -> Any:
            return Annotated[item, DEFAULT_ON_NULL]


def coerce_protocol_version(value: Any) -> Any:
    """Read a ``protocolVersion`` like the Rust SDK: strings that are no integer are 0.

    Some clients (older Zed builds) sent MCP-style date strings such as
    ``"2024-11-05"``. Integers stay as they are, numeric strings become integers; an
    agent then answers with the version it supports and the client decides.
    Other types are returned unchanged (and fail validation).
    """
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return value
