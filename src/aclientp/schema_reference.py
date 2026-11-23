"""Pinned upstream ACP schema references: syncing and conformance checks.

The hand-written models in :mod:`aclientp.schema` follow the upstream ACP JSON schema
(``agentclientprotocol/agent-client-protocol``). Instead of generating code, the
repository pins one upstream schema release per protocol version and checks the
models against it:

- ``schema/v{N}/VERSION``: upstream git tag, e.g. ``schema-v1.33.0``
- ``schema/v{N}/schema.unstable.json`` and ``meta.unstable.json``: the
  vendored upstream files of that tag (the unstable schema is a superset of the
  stable one)
- ``schema/v{N}/conformance.toml``: renames and known, documented differences

Commands (``uv run python -m aclientp.schema_reference <command>``):

- ``latest``: show the pinned and the newest upstream tag
- ``sync [--tag TAG] [--dry-run]``: download a tag (default: newest), print the
  schema changelog against the pinned one, and update the pin
- ``check``: compare the models with the pinned schema
- ``refs [--update NAME [--commit SHA]]``: show what changed in the pinned reference
  implementations (``schema/references.toml``) since their pinned commit, or move a pin
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
import importlib
import inspect
import json
import pkgutil
import sys
import tomllib
import types
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ForwardRef,
    Literal,
    TypeAliasType,
    TypeGuard,
    Union,
    get_args,
    get_origin,
)

from pydantic import MISSING, BaseModel
from upathtools import UPath


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from pydantic.fields import FieldInfo as PydanticFieldInfo


REPO = "agentclientprotocol/agent-client-protocol"
REFERENCE_ROOT = UPath(__file__).resolve().parents[2] / "schema"
REFERENCE_FILES = ("schema.unstable.json", "meta.unstable.json")
MODEL_PACKAGES: dict[int, str] = {1: "aclientp.schema", 2: "aclientp.schema.v2"}
"""Package holding the hand-written models of each protocol version."""

METHOD_GROUPS = {
    "agentMethods": "AgentMethod",
    "clientMethods": "ClientMethod",
    "protocolMethods": "ProtocolMethod",
    "proxyMethods": "ProxyMethod",
}
"""``meta.unstable.json`` method groups and the ``Literal`` aliases mirroring them."""

type JSON = dict[str, Any]


# ---------------------------------------------------------------------------
# Upstream schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shape:
    """An object shape of the upstream schema: a named definition or a union variant."""

    key: str
    """``Name`` for a definition, ``Union[prop=value]`` for a tagged union variant."""

    properties: frozenset[str]
    """Wire names of all properties, including inherited (``allOf``) ones."""

    required: frozenset[str]
    """Wire names of required properties."""

    union: str | None = None
    """Name of the union a variant belongs to."""

    discriminator: tuple[str, str] | None = None
    """``(property, value)`` that selects a variant."""

    bases: frozenset[str] = frozenset()
    """Definitions this shape inherits via ``allOf``."""

    nullable: frozenset[str] = frozenset()
    """Wire names of properties that accept ``null``."""

    constraints: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    """Numeric and length constraints per property, as pydantic names (``ge``, ...)."""

    branch_of: str | None = None
    """For a variant made from a tagged branch inside an object definition: that
    definition. Our side models such an object either as one model of the same name,
    or as a union of the same name whose members are the branches."""


@dataclass(frozen=True)
class EnumDef:
    """A string enum of the upstream schema."""

    name: str
    values: frozenset[str]
    open: bool
    """Whether other strings are allowed too."""


@dataclass(frozen=True)
class UpstreamSchema:
    """The parts of an upstream schema the checks compare."""

    shapes: dict[str, Shape]
    enums: dict[str, EnumDef]
    union_members: dict[str, frozenset[str]]
    """Union name to the keys of its members (variants and referenced definitions)."""

    methods: dict[str, frozenset[str]]
    """``meta.unstable.json`` method group to method names."""

    probes: tuple[Probe, ...] = ()
    """Payloads that upstream rejects through ``not`` or ``if`` / ``then`` constraints."""

    @classmethod
    def parse(cls, schema: JSON, meta: JSON) -> UpstreamSchema:
        """Extract shapes, enums, unions and methods from the upstream files."""
        defs: dict[str, JSON] = schema["$defs"]
        shapes: dict[str, Shape] = {}
        enums: dict[str, EnumDef] = {}
        unions: dict[str, frozenset[str]] = {}
        for name, node in defs.items():
            variants = node.get("oneOf") or node.get("anyOf")
            if variants is not None and "properties" not in node:
                if enum := _parse_enum(name, variants):
                    enums[name] = enum
                    continue
                members: set[str] = set()
                for variant in variants:
                    if ref := variant.get("$ref"):
                        members.add(_ref_name(ref))
                    elif shape := _parse_variant(name, variant, defs):
                        shapes[shape.key] = shape
                        members.add(shape.key)
                    elif (parts := variant.get("allOf")) and len(parts) == 1 and "$ref" in parts[0]:
                        # Untagged variant, e.g. ``{"title": "stdio", "allOf": [{"$ref": ...}]}``
                        members.add(_ref_name(parts[0]["$ref"]))
                unions[name] = frozenset(members)
            elif "enum" in node:
                enums[name] = EnumDef(name, frozenset(node["enum"]), open=False)
            elif _is_object(node, defs):
                collected = _collect(node, defs)
                shapes[name] = Shape(
                    name,
                    collected.properties,
                    collected.required,
                    bases=collected.bases,
                    nullable=collected.nullable,
                    constraints=collected.constraints,
                )
                branches = [
                    branch
                    for part in node.get("oneOf", []) + node.get("anyOf", [])
                    if (branch := _parse_branch(name, node, part, defs))
                ]
                for branch in branches:
                    shapes[branch.key] = branch
                if branches:
                    unions[name] = frozenset(branch.key for branch in branches)
        methods = {
            group: frozenset(meta[group].values()) for group in METHOD_GROUPS if group in meta
        }
        probes = tuple(probe for name, node in defs.items() for probe in _probes(name, node, defs))
        return cls(shapes, enums, unions, methods, probes)

    @property
    def inlined_bases(self) -> frozenset[str]:
        """Definitions that only serve as ``allOf`` bases of union variants."""
        used_as_base = {
            base for shape in self.shapes.values() if shape.union for base in shape.bases
        }
        referenced = {member for members in self.union_members.values() for member in members}
        return frozenset(used_as_base - referenced)


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _parse_enum(name: str, variants: list[JSON]) -> EnumDef | None:
    values: set[str] = set()
    is_open = False
    for variant in variants:
        if isinstance(const := variant.get("const"), str):
            values.add(const)
        elif variant.get("type") == "string" and "properties" not in variant:
            is_open = True
        else:
            return None
    return EnumDef(name, frozenset(values), is_open) if values else None


def _is_object(node: JSON, defs: Mapping[str, JSON]) -> bool:
    if node.get("type") == "object" or "properties" in node:
        return True
    return any(
        _is_object(defs[_ref_name(part["$ref"])] if "$ref" in part else part, defs)
        for part in node.get("allOf", [])
    )


@dataclass(frozen=True)
class _Collected:
    properties: frozenset[str]
    required: frozenset[str]
    bases: frozenset[str]
    nullable: frozenset[str]
    constraints: dict[str, dict[str, float]]


def _collect(node: JSON, defs: Mapping[str, JSON]) -> _Collected:
    """Properties, required and nullable properties and ``allOf`` bases of an object.

    Properties of ``anyOf`` / ``oneOf`` branches inside an object count as
    optional properties of the object.
    """
    own: JSON = node.get("properties", {})
    props = set(own)
    required = set(node.get("required", []))
    nullable = {prop for prop, schema in own.items() if _accepts_null(schema, defs)}
    constraints = {prop: c for prop, schema in own.items() if (c := _constraints(schema, defs))}
    bases: set[str] = set()
    for part in node.get("allOf", []):
        if ref := part.get("$ref"):
            bases.add(_ref_name(ref))
            part = defs[_ref_name(ref)]
        sub = _collect(part, defs)
        props |= sub.properties
        required |= sub.required
        bases |= sub.bases
        nullable |= sub.nullable
        constraints = {**sub.constraints, **constraints}
    branch_required: list[frozenset[str]] = []
    for part in node.get("anyOf", []) + node.get("oneOf", []):
        resolved = defs[_ref_name(part["$ref"])] if "$ref" in part else part
        sub = _collect(resolved, defs)
        props |= sub.properties
        bases |= sub.bases
        nullable |= sub.nullable
        constraints = {**sub.constraints, **constraints}
        branch_required.append(sub.required)
    if branch_required:
        # Required in every branch means required for the object.
        required |= frozenset.intersection(*branch_required)
    return _Collected(
        frozenset(props), frozenset(required), frozenset(bases), frozenset(nullable), constraints
    )


CONSTRAINT_KEYS = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minLength": "min_length",
    "maxLength": "max_length",
    "minItems": "min_length",
    "maxItems": "max_length",
}
"""JSON schema keywords and the pydantic constraints they correspond to."""


def _constraints(schema: JSON, defs: Mapping[str, JSON] | None = None) -> dict[str, float]:
    """Numeric and length constraints of a property schema.

    Also inside ``anyOf: [x, null]`` and, with ``defs``, behind a ``$ref`` to a non-object
    definition such as a constrained string type.
    """
    found = {CONSTRAINT_KEYS[k]: float(v) for k, v in schema.items() if k in CONSTRAINT_KEYS}
    if defs is not None and (ref := schema.get("$ref")):
        target = defs.get(_ref_name(ref), {})
        if not _is_object(target, defs):
            found = _constraints(target, defs) | found
    for key in ("anyOf", "oneOf", "allOf"):
        parts = [part for part in schema.get(key, []) if part.get("type") != "null"]
        if len(parts) == 1:
            found |= _constraints(parts[0], defs)
    return found


_TYPED_KEYWORDS = ("$ref", "anyOf", "oneOf", "allOf", "const", "enum", "properties")
"""Keywords that constrain a schema without a ``type``."""


def _accepts_null(schema: JSON, defs: Mapping[str, JSON], depth: int = 0) -> bool:
    """Whether a property schema accepts ``null``."""
    kind = schema.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    if kind is None and not any(k in schema for k in _TYPED_KEYWORDS):
        return True  # no type constraint: any JSON value, including null
    if depth > 8:  # noqa: PLR2004
        return False
    if ref := schema.get("$ref"):
        return _accepts_null(defs[_ref_name(ref)], defs, depth + 1)
    branches = schema.get("anyOf", []) + schema.get("oneOf", [])
    if any(_accepts_null(part, defs, depth + 1) for part in branches):
        return True
    parts = schema.get("allOf", [])
    return len(parts) == 1 and _accepts_null(parts[0], defs, depth + 1)


def _parse_variant(union: str, variant: JSON, defs: Mapping[str, JSON]) -> Shape | None:
    tag = next(
        (
            (prop, schema["const"])
            for prop, schema in variant.get("properties", {}).items()
            if isinstance(schema.get("const"), str)
        ),
        None,
    )
    if tag is None:
        return None
    collected = _collect(variant, defs)
    return Shape(
        f"{union}[{tag[0]}={tag[1]}]",
        collected.properties,
        collected.required,
        union,
        tag,
        collected.bases,
        collected.nullable,
        collected.constraints,
    )


def _parse_branch(name: str, node: JSON, branch: JSON, defs: Mapping[str, JSON]) -> Shape | None:
    """A tagged ``oneOf`` / ``anyOf`` branch of an object, merged with the object."""
    merged: JSON = {
        "properties": {**node.get("properties", {}), **branch.get("properties", {})},
        "required": [*node.get("required", []), *branch.get("required", [])],
        "allOf": [*node.get("allOf", []), *branch.get("allOf", [])],
    }
    shape = _parse_variant(name, merged, defs)
    return None if shape is None else replace(shape, branch_of=name)


# ---------------------------------------------------------------------------
# Conditional constraints: ``not`` and ``if`` / ``then``
# ---------------------------------------------------------------------------
#
# These constraints do not show in the fields of a model (a validator enforces them on
# our side), so they are compared by behaviour: each one yields a payload upstream
# rejects because of it, and a valid control payload next to it. The models must reject
# the first and accept the second.


@dataclass(frozen=True)
class Probe:
    """A payload upstream rejects through a ``not`` or ``if`` / ``then`` constraint."""

    id: str
    """Finding id if the models accept ``payload``, e.g.
    ``conditional:MessageMcpRequest.toolCallId->sessionId``."""

    definition: str
    """Upstream definition to validate against (a model or a union alias of ours)."""

    payload: JSON
    """Rejected upstream."""

    control: JSON
    """The same payload without the violation: valid upstream."""

    detail: str


_NO_EXAMPLE = object()


def _example(schema: Any, defs: Mapping[str, JSON], depth: int = 0) -> Any:  # noqa: PLR0911
    """A minimal value valid for ``schema`` (required properties only), or ``_NO_EXAMPLE``."""
    if not isinstance(schema, dict) or depth > 12:  # noqa: PLR2004
        return _NO_EXAMPLE if schema is False else "x"
    if ref := schema.get("$ref"):
        return _example(defs[_ref_name(ref)], defs, depth + 1)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if _is_object(schema, defs):
        return _example_object(schema, defs, depth)
    for key in ("anyOf", "oneOf"):
        for part in schema.get(key, []):
            if part.get("type") != "null" and (value := _example(part, defs, depth + 1)) is not (
                _NO_EXAMPLE
            ):
                return value
    if parts := schema.get("allOf"):
        return _example(parts[-1], defs, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    match kind:
        case "string":
            return "x" * max(1, int(schema.get("minLength", 1)))
        case "integer" | "number":
            if "minimum" in schema:
                return schema["minimum"]
            return schema["exclusiveMinimum"] + 1 if "exclusiveMinimum" in schema else 0
        case "boolean":
            return False
        case "array":
            item = _example(schema.get("items", {}), defs, depth + 1)
            return [item] * int(schema.get("minItems", 0))
        case "null":
            return None
        case _:
            return "x"


def _example_object(schema: JSON, defs: Mapping[str, JSON], depth: int) -> Any:
    properties: JSON = dict(schema.get("properties", {}))
    required: list[str] = list(schema.get("required", []))
    value: dict[str, Any] = {}
    for part in schema.get("allOf", []):
        sub = _example(part, defs, depth + 1)
        if isinstance(sub, dict):
            value |= sub
    branches = schema.get("anyOf", []) + schema.get("oneOf", [])
    if branches:  # an object with tagged branches: satisfy the first one
        sub = _example(_merged_branch(schema, branches[0]), defs, depth + 1)
        if isinstance(sub, dict):
            value |= sub
    for prop in required:
        if prop not in value:
            sub = _example(properties.get(prop, {}), defs, depth + 1)
            if sub is _NO_EXAMPLE:
                return _NO_EXAMPLE
            value[prop] = sub
    return value


def _merged_branch(node: JSON, branch: JSON) -> JSON:
    """A branch of an object definition, with the object's own properties."""
    return {
        "type": "object",
        "properties": {**node.get("properties", {}), **branch.get("properties", {})},
        "required": [*node.get("required", []), *branch.get("required", [])],
        "allOf": [*node.get("allOf", []), *branch.get("allOf", [])],
        **({"not": branch["not"]} if "not" in branch else {}),
    }


def _excluded_tags(node: JSON) -> list[tuple[str, Any]]:
    """``(property, value)`` pairs a ``not`` excludes, e.g. ``not: {type: const boolean}``."""
    parts = node.get("anyOf", [node])
    pairs: list[tuple[str, Any]] = []
    for part in parts:
        props: JSON = part.get("properties", {})
        required = part.get("required", [])
        if len(props) != 1 or list(props) != required or "const" not in next(iter(props.values())):
            return []
        prop, schema = next(iter(props.items()))
        pairs.append((prop, schema["const"]))
    return pairs


def _rejects(schema: Any, value: Any, defs: Mapping[str, JSON], depth: int = 0) -> bool:  # noqa: PLR0911
    """Whether ``value`` certainly fails ``schema`` (False when unsure)."""
    if not isinstance(schema, dict) or depth > 12:  # noqa: PLR2004
        return schema is False
    if ref := schema.get("$ref"):
        return _rejects(defs[_ref_name(ref)], value, defs, depth + 1)
    if "const" in schema and value != schema["const"]:
        return True
    if "enum" in schema and value not in schema["enum"]:
        return True
    if any(_rejects(part, value, defs, depth + 1) for part in schema.get("allOf", [])):
        return True
    for key in ("anyOf", "oneOf"):
        if (parts := schema.get(key)) and all(_rejects(p, value, defs, depth + 1) for p in parts):
            return True
    kind = schema.get("type")
    if kind is None:
        return False
    kinds = kind if isinstance(kind, list) else [kind]
    if not any(_json_type_matches(k, value) for k in kinds):
        return True
    if isinstance(value, dict) and _is_object(schema, defs):
        collected = _collect(schema, defs)
        if not collected.required <= value.keys():
            return True
        props: JSON = schema.get("properties", {})
        return any(_rejects(props[k], v, defs, depth + 1) for k, v in value.items() if k in props)
    return False


def _json_type_matches(kind: str, value: Any) -> bool:  # noqa: PLR0911
    match kind:
        case "null":
            return value is None
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "string":
            return isinstance(value, str)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
    return True


def _probes(name: str, node: JSON, defs: Mapping[str, JSON]) -> Iterator[Probe]:
    """Probes of the ``not`` and ``if`` / ``then`` constraints of definition ``name``."""
    conditional = isinstance(node.get("if"), dict) and isinstance(node.get("then"), dict)
    if conditional and (probe := _conditional_probe(name, node, defs)):
        yield probe
    branches: list[JSON] = node.get("anyOf", []) + node.get("oneOf", [])
    is_object = "properties" in node or node.get("type") == "object"
    for index, branch in enumerate(branches):
        if "not" not in branch:
            continue
        resolved = _merged_branch(node, branch) if is_object else branch
        base = _example({k: v for k, v in resolved.items() if k != "not"}, defs)
        if not isinstance(base, dict):
            continue
        siblings = [
            _merged_branch(node, other) if is_object else other
            for i, other in enumerate(branches)
            if i != index
        ]
        for prop, value in _excluded_tags(branch["not"]):
            payload = {**base, prop: value}
            if not all(_rejects(sibling, payload, defs) for sibling in siblings):
                continue  # another branch may accept it: not a certain rejection
            yield Probe(
                f"accepts-excluded:{name}[{prop}={value}]",
                name,
                payload,
                base,
                f"upstream excludes {prop}={value!r} from a fallback branch, the models accept it",
            )


def _conditional_probe(name: str, node: JSON, defs: Mapping[str, JSON]) -> Probe | None:
    condition: JSON = node["if"]
    consequence: JSON = node["then"]
    when = list(condition.get("required", [])) or [
        prop for prop in condition.get("properties", {}) if prop in node.get("required", [])
    ]
    needs = [prop for prop in consequence.get("required", []) if prop not in when]
    if not when or not needs:
        return None
    base = _example({k: v for k, v in node.items() if k not in ("if", "then", "else")}, defs)
    if not isinstance(base, dict):
        return None
    props: JSON = node.get("properties", {})
    control = dict(base)
    for prop in when:
        schema = condition.get("properties", {}).get(prop) or props.get(prop, {})
        control[prop] = _example(schema, defs)
    for prop in needs:
        schema = consequence.get("properties", {}).get(prop) or props.get(prop, {})
        if "$ref" not in schema and prop in props:
            schema = {"allOf": [props[prop], schema]}
        control[prop] = _example(schema, defs)
    if any(v is _NO_EXAMPLE for v in control.values()):
        return None
    payload = {k: v for k, v in control.items() if k not in needs}
    if_part = "+".join(when)
    then_part = "+".join(needs)
    return Probe(
        f"conditional:{name}.{if_part}->{then_part}",
        name,
        payload,
        control,
        f"upstream requires {then_part} when {if_part} is given, the models do not",
    )


# ---------------------------------------------------------------------------
# Hand-written models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OurSchema:
    """Models and module-level aliases (unions, ``Literal`` enums) of a model package."""

    models: dict[str, type[BaseModel]]
    aliases: dict[str, object]

    @classmethod
    def load(cls, package_name: str) -> OurSchema:
        """Collect models and aliases from the direct modules of ``package_name``."""
        package = importlib.import_module(package_name)
        models: dict[str, type[BaseModel]] = {}
        aliases: dict[str, object] = {}
        for info in pkgutil.iter_modules(package.__path__):
            if info.ispkg:
                continue
            module = importlib.import_module(f"{package_name}.{info.name}")
            for name, obj in vars(module).items():
                if inspect.isclass(obj):
                    if issubclass(obj, BaseModel) and obj.__module__ == module.__name__:
                        models[name] = obj
                elif not name.startswith("_") and _is_type_alias(obj):
                    aliases.setdefault(name, obj)
        return cls(models, aliases)

    def union_members(self, name: str) -> list[type[BaseModel]]:
        """Models of the union alias ``name`` (empty if there is none)."""
        alias = self.aliases.get(name)
        return [] if alias is None else [m for m in _flatten(alias) if _is_model(m)]

    def literal_values(self, name: str) -> tuple[frozenset[str], bool] | None:
        """Values of the ``Literal`` alias ``name``, and whether it also allows ``str``."""
        alias = self.aliases.get(name)
        if alias is None:
            return None
        members = list(_flatten(alias))
        values = {arg for m in members if get_origin(m) is Literal for arg in get_args(m)}
        if not values:
            return None
        return frozenset(str(v) for v in values), str in members


def _is_model(obj: object) -> TypeGuard[type[BaseModel]]:
    return inspect.isclass(obj) and issubclass(obj, BaseModel)


def _is_type_alias(obj: object) -> bool:
    if isinstance(obj, types.UnionType | TypeAliasType):
        return True
    return get_origin(obj) in (Literal, Annotated, Union)


def _members(tp: object) -> list[object]:
    """Members of a union annotation, keeping ``Annotated`` members whole."""
    if isinstance(tp, TypeAliasType):
        return _members(tp.__value__)
    if get_origin(tp) is Union or isinstance(tp, types.UnionType):
        return [m for arg in get_args(tp) for m in _members(arg)]
    return [tp]


def _flatten(tp: object) -> Iterator[object]:
    """Members of a (possibly annotated or aliased) union; ``Literal`` stays whole."""
    if isinstance(tp, TypeAliasType):
        yield from _flatten(tp.__value__)
        return
    origin = get_origin(tp)
    if origin is Annotated:
        yield from _flatten(get_args(tp)[0])
    elif origin is Union or isinstance(tp, types.UnionType):
        for arg in get_args(tp):
            yield from _flatten(arg)
    else:
        yield tp


@dataclass(frozen=True)
class FieldInfo:
    """How a model field behaves on the wire."""

    required: bool
    """The field must be present when parsing."""

    omittable: bool
    """The field can be left out when serializing: optional with a ``None`` or
    ``MISSING`` default."""

    accepts_none: bool
    """The annotation allows ``None`` (sent as ``null`` unless excluded)."""

    constraints: dict[str, float]
    """Numeric and length constraints, e.g. ``{"ge": 1}``."""


def wire_fields(model: type[BaseModel]) -> dict[str, FieldInfo]:
    """Wire name and behaviour of each serialized field of ``model``."""
    if any(isinstance(info.annotation, ForwardRef) for info in model.model_fields.values()):
        # Annotations stay unresolved until the model is rebuilt (first validation).
        model.model_rebuild(force=True)
    return {
        (info.alias or name): FieldInfo(
            required=info.is_required(),
            omittable=not info.is_required()
            and (info.default is None or info.default is MISSING)
            and info.default_factory is None,
            accepts_none=any(member in (type(None), Any) for member in _flatten(info.annotation)),
            constraints=_field_constraints(info),
        )
        for name, info in model.model_fields.items()
        if not info.exclude
    }


def _field_constraints(info: PydanticFieldInfo) -> dict[str, float]:
    found: dict[str, float] = {}
    # A constrained type in an optional field (``Key | MISSING | None`` with
    # ``Key = Annotated[str, StringConstraints(...)]``) keeps its metadata on the member.
    members = [m for m in _members(info.annotation) if m not in (type(None), MISSING)]
    metadata = list(info.metadata)
    if len(members) == 1 and get_origin(members[0]) is Annotated:
        metadata.extend(get_args(members[0])[1:])
    for item in metadata:
        for name in ("ge", "le", "gt", "lt", "min_length", "max_length"):
            value = getattr(item, name, None)
            if isinstance(value, int | float):
                found[name] = float(value)
    return found


def _discriminator_value(model: type[BaseModel], prop: str) -> str | None:
    for name, info in model.model_fields.items():
        if (info.alias or name) == prop and isinstance(info.default, str):
            return info.default
    return None


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conformance:
    """``conformance.toml``: renames and known differences of a pinned reference."""

    renames: dict[str, str] = field(default_factory=dict)
    """Upstream definition name to our model name."""

    known: dict[str, str] = field(default_factory=dict)
    """Finding id to the reason the difference is accepted."""

    check_nullability: bool = False
    """Compare which fields accept ``null``. For model packages that send ``None`` as
    ``null`` and leave out ``MISSING`` fields (v2), not for ``exclude_none`` ones (v1)."""

    check_parse_nullability: bool = False
    """Report fields where upstream allows ``null`` but the model rejects ``None``
    (valid messages fail to parse). Implied by ``check_nullability``; for packages
    that never send ``null`` (v1)."""

    check_constraints: bool = False
    """Compare numeric and length constraints (``minimum``, ``minItems``, ...)."""

    allow_missing: bool = False
    """Accept upstream shapes, enums and methods without a counterpart yet, while a
    model package is being written. Fields of the shapes that exist are still checked."""

    @classmethod
    def load(cls, path: UPath) -> Conformance:
        """Read ``conformance.toml``; a missing file means no renames or differences."""
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text("utf-8"))
        return cls(
            dict(data.get("renames", {})),
            dict(data.get("known", {})),
            bool(data.get("check_nullability", False)),
            bool(data.get("check_parse_nullability", False)),
            bool(data.get("check_constraints", False)),
            bool(data.get("allow_missing", False)),
        )


@dataclass(frozen=True)
class Finding:
    """One difference between the models and the reference."""

    id: str
    """Stable id, e.g. ``missing-field:ToolCallUpdate.name``; key in ``conformance.toml``."""

    detail: str


def check(
    upstream: UpstreamSchema,
    ours: OurSchema,
    renames: Mapping[str, str],
    *,
    check_nullability: bool = False,
    check_parse_nullability: bool = False,
    check_constraints: bool = False,
) -> list[Finding]:
    """Compare the models with the upstream schema.

    ``renames`` maps upstream definition names (shapes, unions, enums) to ours.
    """
    findings: list[Finding] = []
    matched = _match_shapes(upstream, ours, renames)
    inlined = upstream.inlined_bases
    # Discriminators that variants add to the definitions they inherit from.
    added_tags: dict[str, set[str]] = {}
    for shape in upstream.shapes.values():
        if shape.discriminator:
            for base in shape.bases:
                added_tags.setdefault(base, set()).add(shape.discriminator[0])
    for key, shape in sorted(upstream.shapes.items()):
        if shape.branch_of and shape.branch_of in matched:
            continue  # the object is one model on our side, compared as a whole
        model = matched.get(key)
        if model is None:
            if key not in inlined and key not in upstream.union_members:
                findings.append(Finding(f"missing-shape:{key}", "no matching model"))
            continue
        tags = added_tags.get(key, set())
        findings.extend(
            _compare_fields(
                key,
                shape,
                model,
                tags,
                check_nullability,
                check_constraints,
                check_parse_nullability=check_parse_nullability,
            )
        )
    findings.extend(_check_enums(upstream, ours, renames))
    findings.extend(_check_methods(upstream, ours))
    findings.extend(_check_probes(upstream, ours, renames))
    return findings


def _check_probes(
    upstream: UpstreamSchema, ours: OurSchema, renames: Mapping[str, str]
) -> Iterator[Finding]:
    from pydantic import TypeAdapter, ValidationError

    for probe in upstream.probes:
        target = renames.get(probe.definition, probe.definition)
        model: object = ours.models.get(target) or ours.aliases.get(target)
        if model is None:
            continue  # reported as a missing shape or union
        adapter: TypeAdapter[Any] = TypeAdapter(model)
        try:
            adapter.validate_json(json.dumps(probe.control))
        except ValidationError as exc:
            yield Finding(
                f"unprobed:{probe.id.split(':', 1)[1]}",
                f"the models reject the valid control payload {probe.control}, so the "
                f"constraint cannot be checked: {exc.errors()[0]['msg']}",
            )
            continue
        try:
            adapter.validate_json(json.dumps(probe.payload))
        except ValidationError:
            continue
        yield Finding(probe.id, f"{probe.detail}: {probe.payload}")


def _check_enums(
    upstream: UpstreamSchema, ours: OurSchema, renames: Mapping[str, str]
) -> Iterator[Finding]:
    for name, enum in sorted(upstream.enums.items()):
        values = ours.literal_values(renames.get(name, name))
        if values is None:
            yield Finding(f"missing-enum:{name}", "no Literal alias of this name")
            continue
        our_values, our_open = values
        for value in sorted(enum.values - our_values):
            yield Finding(f"missing-enum-value:{name}.{value}", "upstream value not in Literal")
        for value in sorted(our_values - enum.values):
            yield Finding(f"extra-enum-value:{name}.{value}", "Literal value not upstream")
        if enum.open and not our_open:
            yield Finding(
                f"closed-enum:{name}", "upstream allows other strings, the Literal does not"
            )


def _check_methods(upstream: UpstreamSchema, ours: OurSchema) -> Iterator[Finding]:
    for group, alias in METHOD_GROUPS.items():
        upstream_methods = upstream.methods.get(group, frozenset())
        values = ours.literal_values(alias)
        our_methods = values[0] if values else frozenset()
        for method in sorted(upstream_methods - our_methods):
            yield Finding(f"missing-method:{alias}.{method}", "upstream method not in Literal")
        for method in sorted(our_methods - upstream_methods):
            yield Finding(f"extra-method:{alias}.{method}", "method not upstream")


def _match_shapes(
    upstream: UpstreamSchema, ours: OurSchema, renames: Mapping[str, str]
) -> dict[str, type[BaseModel]]:
    matched: dict[str, type[BaseModel]] = {}
    for key, shape in upstream.shapes.items():
        if model := ours.models.get(renames.get(key, key)):
            matched[key] = model
        elif shape.union and shape.discriminator:
            prop, value = shape.discriminator
            union = renames.get(shape.union, shape.union)
            # Without our union, only models named like an upstream base of the
            # variant qualify: tags such as `type: "text"` recur across unions.
            candidates = ours.union_members(union) or [
                ours.models[base] for base in shape.bases if base in ours.models
            ]
            hits = [m for m in candidates if _discriminator_value(m, prop) == value]
            if len(hits) > 1:
                # Same tag in several unions: prefer the model whose fields fit best.
                hits.sort(key=lambda m: len(shape.properties ^ set(wire_fields(m))))
            if hits:
                matched[key] = hits[0]
    return matched


def _compare_fields(
    key: str,
    shape: Shape,
    model: type[BaseModel],
    added_tags: set[str],
    check_nullability: bool,
    check_constraints: bool = False,
    *,
    check_parse_nullability: bool = False,
) -> Iterator[Finding]:
    fields = wire_fields(model)
    tag = shape.discriminator[0] if shape.discriminator else None
    where = f"{model.__name__} ({key})" if model.__name__ != key else key
    for prop in sorted(shape.properties - set(fields)):
        yield Finding(f"missing-field:{key}.{prop}", f"{where} lacks it")
    for prop in sorted(set(fields) - shape.properties - added_tags):
        yield Finding(f"extra-field:{key}.{prop}", f"{where} has it, upstream does not")
    for prop in sorted(shape.properties & set(fields)):
        if prop == tag:
            continue
        info = fields[prop]
        if prop in shape.required and info.omittable:
            yield Finding(
                f"omittable-field:{key}.{prop}",
                f"required upstream, but {where} may leave it out (None default)",
            )
        elif info.required and prop not in shape.required:
            yield Finding(
                f"required-field:{key}.{prop}",
                f"optional upstream, required in {where}: valid messages fail to parse",
            )
        if check_constraints and dict(shape.constraints.get(prop, {})) != info.constraints:
            yield Finding(
                f"constraint:{key}.{prop}",
                f"upstream {dict(shape.constraints.get(prop, {}))}, {where} {info.constraints}",
            )
        if not (check_nullability or check_parse_nullability):
            continue
        if prop in shape.nullable and not info.accepts_none:
            yield Finding(
                f"rejects-null:{key}.{prop}",
                f"upstream allows null, {where} does not: valid messages fail to parse",
            )
        elif check_nullability and info.accepts_none and prop not in shape.nullable:
            yield Finding(
                f"sends-null:{key}.{prop}",
                f"{where} accepts None and would send null, upstream does not allow it",
            )


@dataclass(frozen=True)
class Report:
    """Findings of a check, split against the known differences."""

    new: list[Finding]
    """Differences not listed in ``conformance.toml``."""

    stale: dict[str, str]
    """Listed differences that no longer occur."""

    tag: str = ""
    """Upstream tag of the checked reference."""

    @property
    def ok(self) -> bool:
        return not self.new and not self.stale


def run_check(protocol_version: int, root: UPath = REFERENCE_ROOT) -> Report:
    """Check the models of ``protocol_version`` against its pinned reference."""
    reference = Reference.load(protocol_version, root)
    conformance = Conformance.load(reference.directory / "conformance.toml")
    ours = OurSchema.load(MODEL_PACKAGES[protocol_version])
    findings = check(
        reference.parsed,
        ours,
        conformance.renames,
        check_nullability=conformance.check_nullability,
        check_parse_nullability=conformance.check_parse_nullability,
        check_constraints=conformance.check_constraints,
    )
    if conformance.allow_missing:
        findings = [f for f in findings if not f.id.startswith(MISSING_KINDS)]
    return replace(split_known(findings, conformance.known), tag=reference.tag)


MISSING_KINDS = ("missing-shape:", "missing-enum:", "missing-method:")
"""Finding kinds that ``allow_missing`` accepts."""


def split_known(findings: Iterable[Finding], known: Mapping[str, str]) -> Report:
    """Split findings into undocumented ones, and find stale known differences."""
    findings = list(findings)
    ids = {f.id for f in findings}
    return Report(
        new=[f for f in findings if f.id not in known],
        stale={k: v for k, v in known.items() if k not in ids},
    )


# ---------------------------------------------------------------------------
# Pinned references and syncing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """A pinned upstream schema release."""

    protocol_version: int
    tag: str
    schema: JSON
    meta: JSON
    directory: UPath

    @classmethod
    def load(cls, protocol_version: int, root: UPath = REFERENCE_ROOT) -> Reference:
        """Load the pinned reference of ``protocol_version``."""
        directory = root / f"v{protocol_version}"
        tag = (directory / "VERSION").read_text("utf-8").strip()
        schema = json.loads((directory / "schema.unstable.json").read_text("utf-8"))
        meta = json.loads((directory / "meta.unstable.json").read_text("utf-8"))
        return cls(protocol_version, tag, schema, meta, directory)

    @property
    def parsed(self) -> UpstreamSchema:
        return UpstreamSchema.parse(self.schema, self.meta)


def upstream_tags(protocol_version: int) -> list[str]:
    """Upstream schema tags of ``protocol_version``, oldest first."""
    import httpx2

    url = f"https://api.github.com/repos/{REPO}/git/matching-refs/tags/schema-v{protocol_version}."
    response = httpx2.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=30)
    response.raise_for_status()
    tags = [ref["ref"].removeprefix("refs/tags/") for ref in response.json()]
    return sorted(tags, key=tag_sort_key)


def tag_sort_key(tag: str) -> tuple[tuple[int, ...], int, str, int]:
    """Sort key of an upstream schema tag such as ``schema-v2.0.0-alpha.17``.

    A release sorts after its pre-releases (``2.0.0-alpha.17`` < ``2.0.0``).
    """
    release, _, pre = tag.removeprefix("schema-v").partition("-")
    numbers = tuple(int(part) for part in release.split("."))
    if not pre:
        return numbers, 1, "", 0
    label, _, number = pre.partition(".")
    return numbers, 0, label, int(number or 0)


def download(protocol_version: int, tag: str) -> dict[str, str]:
    """Download the reference files of ``tag``, verbatim."""
    import httpx2

    base = f"https://raw.githubusercontent.com/{REPO}/{tag}/schema/v{protocol_version}"
    files: dict[str, str] = {}
    with httpx2.Client(timeout=60) as client:
        for name in REFERENCE_FILES:
            response = client.get(f"{base}/{name}")
            response.raise_for_status()
            files[name] = response.text
    return files


def parse_files(files: Mapping[str, str]) -> UpstreamSchema:
    """Parse downloaded reference files."""
    return UpstreamSchema.parse(
        json.loads(files["schema.unstable.json"]), json.loads(files["meta.unstable.json"])
    )


def write_reference(protocol_version: int, tag: str, files: Mapping[str, str], root: UPath) -> None:
    """Store downloaded reference files, unchanged, as the pin of ``protocol_version``.

    Keeping upstream's bytes means a ``git diff`` between pins is upstream's diff.
    """
    directory = root / f"v{protocol_version}"
    directory.mkdir(parents=True, exist_ok=True)
    for name in REFERENCE_FILES:
        (directory / name).write_text(files[name], encoding="utf-8")
    (directory / "VERSION").write_text(tag + "\n", encoding="utf-8")


def changelog(old: UpstreamSchema, new: UpstreamSchema) -> list[str]:
    """Human-readable changes between two upstream schemas."""
    lines: list[str] = []
    lines.extend(_diff_keys("shape", old.shapes, new.shapes))
    for key in sorted(old.shapes.keys() & new.shapes.keys()):
        before, after = old.shapes[key], new.shapes[key]
        lines.extend(f"+ field {key}.{p}" for p in sorted(after.properties - before.properties))
        lines.extend(f"- field {key}.{p}" for p in sorted(before.properties - after.properties))
        common = before.properties & after.properties
        lines.extend(
            f"~ field {key}.{p} is now required"
            for p in sorted((after.required - before.required) & common)
        )
        lines.extend(
            f"~ field {key}.{p} is now optional"
            for p in sorted((before.required - after.required) & common)
        )
    lines.extend(_diff_keys("enum", old.enums, new.enums))
    for name in sorted(old.enums.keys() & new.enums.keys()):
        before_values, after_values = old.enums[name].values, new.enums[name].values
        lines.extend(f"+ enum value {name}.{v}" for v in sorted(after_values - before_values))
        lines.extend(f"- enum value {name}.{v}" for v in sorted(before_values - after_values))
    lines.extend(_diff_keys("union", old.union_members, new.union_members))
    for name in sorted(old.union_members.keys() & new.union_members.keys()):
        before_members, after_members = old.union_members[name], new.union_members[name]
        lines.extend(f"+ union member {name}: {m}" for m in sorted(after_members - before_members))
        lines.extend(f"- union member {name}: {m}" for m in sorted(before_members - after_members))
    for group in METHOD_GROUPS:
        before_methods = old.methods.get(group, frozenset())
        after_methods = new.methods.get(group, frozenset())
        lines.extend(f"+ method {group}: {m}" for m in sorted(after_methods - before_methods))
        lines.extend(f"- method {group}: {m}" for m in sorted(before_methods - after_methods))
    return lines


def _diff_keys(kind: str, old: Iterable[str], new: Iterable[str]) -> list[str]:
    before, after = set(old), set(new)
    return [f"+ {kind} {k}" for k in sorted(after - before)] + [
        f"- {kind} {k}" for k in sorted(before - after)
    ]


# ---------------------------------------------------------------------------
# Pinned reference implementations
# ---------------------------------------------------------------------------

REFERENCES_FILE = REFERENCE_ROOT / "references.toml"


@dataclass(frozen=True)
class ReferenceImplementation:
    """An upstream implementation pinned by commit (``schema/references.toml``)."""

    name: str
    repo: str
    commit: str
    paths: tuple[str, ...]
    """Paths in the repository whose changes are of interest."""

    description: str = ""


def load_references(path: UPath = REFERENCES_FILE) -> list[ReferenceImplementation]:
    """Read the pinned reference implementations."""
    data = tomllib.loads(path.read_text("utf-8"))
    return [
        ReferenceImplementation(
            name=name,
            repo=entry["repo"],
            commit=entry["commit"],
            paths=tuple(entry.get("paths", ())),
            description=entry.get("description", ""),
        )
        for name, entry in data.items()
    ]


@dataclass(frozen=True)
class UpstreamCommit:
    """A commit of a reference implementation."""

    sha: str
    date: str
    subject: str


@dataclass(frozen=True)
class ReferenceChanges:
    """What changed in a reference implementation since its pinned commit."""

    reference: ReferenceImplementation
    head: UpstreamCommit
    commits: list[UpstreamCommit]
    """Commits since the pin that touched the reference's paths, newest first."""

    files: list[str]
    """Files under the reference's paths that differ between the pin and the head."""

    @property
    def compare_url(self) -> str:
        repo, pin, head = self.reference.repo, self.reference.commit, self.head.sha
        return f"https://github.com/{repo}/compare/{pin[:12]}...{head[:12]}"


def _github(endpoint: str, **params: str | int) -> Any:
    import os

    import httpx2

    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    response = httpx2.get(
        f"https://api.github.com/{endpoint}", params=params, headers=headers, timeout=30
    )
    response.raise_for_status()
    return response.json()


def _commit(data: JSON) -> UpstreamCommit:
    return UpstreamCommit(
        sha=data["sha"],
        date=data["commit"]["committer"]["date"][:10],
        subject=data["commit"]["message"].split("\n", 1)[0],
    )


def reference_changes(reference: ReferenceImplementation) -> ReferenceChanges:
    """Fetch the changes of ``reference`` since its pinned commit."""
    repo = f"repos/{reference.repo}"
    head = _commit(_github(f"{repo}/commits/HEAD"))
    pinned = _commit(_github(f"{repo}/commits/{reference.commit}"))
    compare = _github(f"{repo}/compare/{reference.commit}...{head.sha}")
    files = sorted(
        entry["filename"]
        for entry in compare.get("files", [])
        if any(_under(entry["filename"], path) for path in reference.paths)
    )
    seen: dict[str, UpstreamCommit] = {}
    for path in reference.paths:
        for data in _github(f"{repo}/commits", path=path, since=f"{pinned.date}T00:00:00Z"):
            commit = _commit(data)
            if commit.sha != reference.commit:
                seen.setdefault(commit.sha, commit)
    in_range = {entry["sha"] for entry in compare.get("commits", [])}
    commits = sorted(
        (c for c in seen.values() if c.sha in in_range), key=lambda c: c.date, reverse=True
    )
    return ReferenceChanges(reference, head, commits, files)


def _under(filename: str, path: str) -> bool:
    return filename == path or filename.startswith(path.rstrip("/") + "/")


def update_reference(name: str, commit: str, path: UPath = REFERENCES_FILE) -> None:
    """Move the pin of reference ``name`` to ``commit``, keeping the file's layout."""
    lines = path.read_text("utf-8").splitlines(keepends=True)
    section: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif section == name and stripped.startswith("commit"):
            lines[index] = f'commit = "{commit}"\n'
            path.write_text("".join(lines), encoding="utf-8")
            return
    msg = f"no reference named {name!r} in {path}"
    raise KeyError(msg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(protocol_version: int, report: Report) -> None:
    print(f"ACP v{protocol_version} models vs {report.tag}")
    for finding in report.new:
        print(f"  NEW    {finding.id}: {finding.detail}")
    for key, reason in report.stale.items():
        print(f"  STALE  {key}: listed in conformance.toml ({reason}) but no longer differs")
    if report.ok:
        print("  ok: no undocumented differences")


def _refs_command(update: str | None, commit: str | None) -> int:
    references = load_references()
    if update is not None:
        reference = next((r for r in references if r.name == update), None)
        if reference is None:
            print(f"unknown reference {update!r}", file=sys.stderr)
            return 1
        target = commit or _commit(_github(f"repos/{reference.repo}/commits/HEAD")).sha
        update_reference(update, target)
        print(f"{update}: pinned {target[:12]} (was {reference.commit[:12]})")
        return 0
    for reference in references:
        changes = reference_changes(reference)
        print(f"{reference.name} ({reference.repo})")
        print(
            f"  pinned {reference.commit[:12]}, head {changes.head.sha[:12]} ({changes.head.date})"
        )
        if not changes.commits and not changes.files:
            print("  no changes in the watched paths")
            continue
        for c in changes.commits:
            print(f"  {c.sha[:12]} {c.date} {c.subject}")
        if changes.files:
            print(f"  changed files: {', '.join(changes.files)}")
        print(f"  {changes.compare_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``python -m aclientp.schema_reference``."""
    parser = argparse.ArgumentParser(
        prog="python -m aclientp.schema_reference", description=__doc__
    )
    parser.add_argument("--protocol", type=int, default=1, choices=(1, 2))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("latest", help="show the pinned and the newest upstream tag")
    sync = commands.add_parser("sync", help="update the pinned reference")
    sync.add_argument("--tag", help="upstream tag (default: newest)")
    sync.add_argument("--dry-run", action="store_true", help="only print the changelog")
    check_parser = commands.add_parser("check", help="compare the models with the pinned reference")
    check_parser.add_argument(
        "--root",
        type=UPath,
        default=REFERENCE_ROOT,
        help="directory holding v1/ and v2/ references (default: the pinned ones)",
    )
    refs = commands.add_parser("refs", help="changes in the pinned reference implementations")
    refs.add_argument("--update", metavar="NAME", help="move the pin of this reference")
    refs.add_argument("--commit", help="commit to pin (default: the current upstream head)")
    args = parser.parse_args(argv)
    version: int = args.protocol

    if args.command == "refs":
        return _refs_command(args.update, args.commit)

    if args.command == "latest":
        pinned = Reference.load(version).tag
        newest = upstream_tags(version)[-1]
        print(f"pinned: {pinned}\nnewest: {newest}" + (" (up to date)" if pinned == newest else ""))
        return 0
    if args.command == "sync":
        tag: str = args.tag or upstream_tags(version)[-1]
        files = download(version, tag)
        if (REFERENCE_ROOT / f"v{version}" / "VERSION").exists():
            current = Reference.load(version)
            print(f"{current.tag} -> {tag}")
            new = parse_files(files)
            for line in changelog(current.parsed, new) or ["(no changes)"]:
                print(f"  {line}")
        else:
            print(f"no pinned reference yet -> {tag}")
        if not args.dry_run:
            write_reference(version, tag, files, REFERENCE_ROOT)
            print(f"pinned {tag}; run `check` to see what the models need")
        return 0
    if version not in MODEL_PACKAGES:
        print(f"no models for ACP v{version} yet", file=sys.stderr)
        return 1
    report = run_check(version, args.root)
    _print_report(version, report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
