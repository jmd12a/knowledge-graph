from __future__ import annotations

from typing import Any, Literal
from typing_extensions import NotRequired, TypedDict


class SupportedValue(TypedDict):
    value: Any
    contribution_ids: list[str]


class PropertyState(TypedDict):
    state: Literal["confirmed", "conflicted", "missing"]
    values: list[SupportedValue]
    merge: NotRequired[Literal["single", "alternatives", "union", "supported_text"]]


class Conflict(TypedDict):
    field_path: str
    reason: str
    contribution_ids: NotRequired[list[str]]
    relation_ids: NotRequired[list[str]]
    group: NotRequired[dict[str, str]]


class Entity(TypedDict):
    kb_id: str
    kind: Literal["entity"]
    origin: Literal["graph"]
    entity_id: str
    entity_type: str
    identity: dict[str, Any]
    canonical_name: str
    aliases: list[str]
    domain_ids: list[str]
    properties: dict[str, PropertyState]
    conflicts: list[Conflict]
    completeness: Literal["complete", "identity_only"]
    missing_fields: list[str]
    contribution_ids: list[str]
    document_ids: list[str]
    object_revision: str
    atomic_groups: dict[str, Any]
    search_text: str
    description: NotRequired[str]
    description_fragments: NotRequired[list[dict[str, Any]]]


class Relation(TypedDict):
    kb_id: str
    kind: Literal["relation"]
    origin: Literal["graph"]
    relation_id: str
    relation_type: str
    source_id: str
    target_id: str
    identity: dict[str, Any]
    properties: dict[str, PropertyState]
    domain_ids: list[str]
    conflicts: list[Conflict]
    contribution_ids: list[str]
    document_ids: list[str]
    object_revision: str
    search_text: str
