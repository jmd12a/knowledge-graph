from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import canonical, new_id, valid_id
from .errors import KGError


# 身份保留完整 JSON；超出存储预算时拒绝，不截断或替换为摘要。
MAX_IDENTITY_KEY_BYTES = 4096
MIN_PROJECTED_INTEGER = -(1 << 63)
MAX_PROJECTED_INTEGER = (1 << 63) - 1


def validate_identity_key(identity: dict) -> str:
    """Return the complete normalized JSON key after checking its storage budget."""
    try:
        encoded = canonical(identity)
        byte_length = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, OverflowError, UnicodeError) as exc:
        raise KGError("FIELD_TYPE_ERROR", "Identity must be finite UTF-8 JSON data") from exc
    if byte_length > MAX_IDENTITY_KEY_BYTES:
        raise KGError(
            "IDENTITY_TOO_LARGE",
            "Normalized identity exceeds the UTF-8 JSON byte budget",
            max_bytes=MAX_IDENTITY_KEY_BYTES,
            actual_bytes=byte_length,
        )
    return encoded


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyDef(StrictModel):
    type: Literal["string", "string_list", "integer", "number", "boolean", "date", "object", "array"] = (
        "string"
    )
    required: bool = False
    merge: Literal["single", "alternatives", "union", "supported_text"] = "single"
    queryable: bool = False
    description: str = ""
    enum: list[Any] | None = None

    @model_validator(mode="after")
    def check(self):
        if self.queryable and self.type not in {"string", "integer", "number", "boolean", "date"}:
            raise ValueError("Only scalar properties can be queryable")
        if self.merge == "supported_text" and self.type != "string":
            raise ValueError("supported_text requires string")
        if self.enum is not None:
            plain = self.model_copy(update={"enum": None})
            for value in self.enum:
                try:
                    check_value(value, plain)
                except KGError as exc:
                    raise ValueError("Enum value does not match the declared property type") from exc
            if len({canonical(value) for value in self.enum}) != len(self.enum):
                raise ValueError("Enum values must be unique")
        return self


class IdentityDef(StrictModel):
    namespace: str
    fields: list[str] = Field(min_length=1)
    normalization: list[str] | dict[str, list[str]] = Field(default_factory=lambda: ["trim"])
    scope: Literal["global", "document"] = "global"

    @model_validator(mode="after")
    def check(self):
        if not self.namespace.strip() or len(self.fields) != len(set(self.fields)):
            raise ValueError("Identity namespace and distinct fields are required")
        smallest_identity = {"namespace": self.namespace, "fields": {field: 0 for field in self.fields}}
        if self.scope == "document":
            smallest_identity["document_id"] = "0" * 36
        try:
            validate_identity_key(smallest_identity)
        except KGError as exc:
            raise ValueError(
                "Configured identity namespace and fields exceed the 4096-byte UTF-8 JSON budget"
            ) from exc
        if isinstance(self.normalization, dict) and set(self.normalization) - set(self.fields):
            raise ValueError("Normalization refers to a non-identity field")
        all_rules = (
            self.normalization
            if isinstance(self.normalization, list)
            else sum(self.normalization.values(), [])
        )
        if set(all_rules) - {"trim", "lower", "upper", "casefold", "nfc", "nfkc", "collapse_whitespace"}:
            raise ValueError("Unknown identity normalization")
        return self


class AtomicGroup(StrictModel):
    name: str
    fields: list[str] = Field(min_length=1)


class NodeType(StrictModel):
    description: str = ""
    identity: IdentityDef
    properties: dict[str, PropertyDef]
    atomic_groups: list[AtomicGroup] = Field(default_factory=list)
    name_field: str = "name"
    name_priority: list[str] = Field(default_factory=list)
    domain_ids: list[str] = Field(default_factory=list)


class RelationType(StrictModel):
    description: str = ""
    source_types: list[str] = Field(min_length=1)
    target_types: list[str] = Field(min_length=1)
    identity_properties: list[str] = Field(default_factory=list)
    properties: dict[str, PropertyDef] = Field(default_factory=dict)
    conflict_rule: dict[str, int] = Field(default_factory=dict)
    domain_ids: list[str] = Field(default_factory=list)


class BusinessModel(StrictModel):
    name: str
    node_types: dict[str, NodeType] = Field(min_length=1)
    relation_types: dict[str, RelationType] = Field(default_factory=dict)
    extraction: dict[str, Any] = Field(default_factory=dict)
    domains: list[dict[str, Any]] = Field(default_factory=list)
    mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check(self):
        for name, node in self.node_types.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                raise ValueError("Invalid node type name")
            if set(node.identity.fields) - node.properties.keys():
                raise ValueError(f"Unknown identity property in {name}")
            if any(
                node.properties[field].type not in {"string", "integer", "number", "boolean", "date"}
                for field in node.identity.fields
            ):
                raise ValueError("Identity fields must have scalar types")
            if len({group.name for group in node.atomic_groups}) != len(node.atomic_groups):
                raise ValueError("Atomic group names must be unique")
            if any(
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", group.name)
                or len(group.fields) != len(set(group.fields))
                for group in node.atomic_groups
            ):
                raise ValueError("Invalid atomic group name or duplicate field")
            for field in node.properties:
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field):
                    raise ValueError("Invalid property name")
            if any(set(group.fields) - node.properties.keys() for group in node.atomic_groups):
                raise ValueError("Atomic group has unknown fields")
            members = [field for group in node.atomic_groups for field in group.fields]
            if len(members) != len(set(members)):
                raise ValueError("Atomic groups cannot overlap")

        # 关系类型必须引用已有节点类型，身份属性与冲突规则也须可被模型解释。
        for name, rel in self.relation_types.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
                raise ValueError("Invalid relation type name")
            if (set(rel.source_types) | set(rel.target_types)) - self.node_types.keys():
                raise ValueError("Unknown relation endpoint type")
            if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field) for field in rel.properties):
                raise ValueError("Invalid relation property name")
            if len(rel.identity_properties) != len(set(rel.identity_properties)):
                raise ValueError("Relation identity properties must be distinct")
            if any(
                rel.properties[field].type not in {"string", "integer", "number", "boolean", "date"}
                for field in rel.identity_properties
                if field in rel.properties
            ):
                raise ValueError("Relation identity properties must have scalar types")
            if set(rel.identity_properties) - rel.properties.keys():
                raise ValueError("Unknown relation identity property")
            if set(rel.conflict_rule) - {"max_distinct_targets"}:
                raise ValueError("Unknown relation conflict rule")
            if any(isinstance(value, bool) or value <= 0 for value in rel.conflict_rule.values()):
                raise ValueError("Relation conflict limits must be positive integers")

        # 领域目录既要引用有效，也要能沿父节点有限地回到根。
        ids = [d.get("id") for d in self.domains]
        if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("Domains require unique ids")
        parents = {d["id"]: d.get("parent_id") for d in self.domains}
        for domain in parents:
            visited, current = set(), domain
            while current is not None:
                if current in visited or current not in parents:
                    raise ValueError("Invalid or cyclic domain hierarchy")
                visited.add(current)
                current = parents[current]
        for definition in list(self.node_types.values()) + list(self.relation_types.values()):
            if set(definition.domain_ids) - set(ids):
                raise ValueError("Type references an unknown domain")

        # 映射仅接受声明式取值和转换选项，端点类型仍服从同一业务模型。
        for name, mapping in self.mappings.items():
            if set(mapping) - {"format", "records", "entities", "relations"}:
                raise ValueError("Unknown structured mapping option")
            if (
                not isinstance(mapping.get("entities"), dict)
                or not isinstance(mapping.get("relations", []), list)
                or not isinstance(mapping.get("records", {}), dict)
            ):
                raise ValueError("Invalid structured mapping shape")
            for definition in list(mapping["entities"].values()) + mapping.get("relations", []):
                if not isinstance(definition, dict) or not isinstance(definition.get("fields", {}), dict):
                    raise ValueError("Invalid mapping entity or relation shape")
                if any(
                    not isinstance(specification, dict)
                    for specification in definition.get("fields", {}).values()
                ):
                    raise ValueError("Mapping field options must be mappings")
            records = mapping.get("records", {})
            allowed_record_keys = (
                {"header", "encoding"} if mapping.get("format") == "csv" else {"pointer", "encoding"}
            )
            if set(records) - allowed_record_keys:
                raise ValueError("Unknown record selection option")
            record_pointer = records.get("pointer", "")
            if (
                not isinstance(record_pointer, str)
                or (record_pointer and not record_pointer.startswith("/"))
                or re.search(r"~(?![01])", record_pointer)
            ):
                raise ValueError("Invalid record JSON pointer")
            if mapping.get("format") not in {"csv", "json"} or not mapping.get("entities"):
                raise ValueError(f"Invalid mapping {name}")
            for entity in mapping["entities"].values():
                if entity.get("type") not in self.node_types:
                    raise ValueError("Unknown mapping entity type")
                if set(entity.get("fields", {})) - self.node_types[entity["type"]].properties.keys():
                    raise ValueError("Unknown mapped property")
                for field in entity.get("fields", {}).values():
                    if set(field) - {"column", "pointer", "cast", "empty"}:
                        raise ValueError("Unsupported executable or unknown mapping expression")
                    if field.get("cast", "string") not in {"string", "integer", "number", "boolean", "date"}:
                        raise ValueError("Unknown mapping cast")
                    if field.get("empty", "error") not in {"error", "omit"}:
                        raise ValueError("Unknown empty policy")
            for rel in mapping.get("relations", []):
                if rel.get("type") not in self.relation_types:
                    raise ValueError("Unknown mapped relation")
                relation = self.relation_types[rel["type"]]
                if set(rel.get("fields", {})) - relation.properties.keys():
                    raise ValueError("Unknown mapped relation property")
                if any(rel.get(k) not in mapping["entities"] for k in ("source_ref", "target_ref")):
                    raise ValueError("Unknown mapping endpoint")
                source_type = mapping["entities"][rel["source_ref"]]["type"]
                target_type = mapping["entities"][rel["target_ref"]]["type"]
                if source_type not in relation.source_types or target_type not in relation.target_types:
                    raise ValueError("Mapped relation endpoint types are not allowed")
            for definition in list(mapping["entities"].values()) + mapping.get("relations", []):
                for field_name, specification in definition.get("fields", {}).items():
                    if not isinstance(specification, dict) or set(specification) - {
                        "column",
                        "pointer",
                        "cast",
                        "empty",
                    }:
                        raise ValueError("Unknown mapping field option")
                    source_key = "column" if mapping["format"] == "csv" else "pointer"
                    if (
                        source_key not in specification
                        or ("pointer" if source_key == "column" else "column") in specification
                    ):
                        raise ValueError("Mapping field must declare the format-specific source")
                    source = specification[source_key]
                    if not isinstance(source, str) or (source_key == "column" and not source):
                        raise ValueError("Invalid mapping source")
                    if source_key == "pointer" and (
                        source and not source.startswith("/") or re.search(r"~(?![01])", source)
                    ):
                        raise ValueError("Invalid JSON pointer")
                    if specification.get("cast", "string") not in {
                        "string",
                        "integer",
                        "number",
                        "boolean",
                        "date",
                    }:
                        raise ValueError("Unknown mapping cast")
                    if specification.get("empty", "error") not in {"error", "omit"}:
                        raise ValueError("Unknown empty policy")
                    properties = (
                        self.node_types[definition["type"]].properties
                        if "source_ref" not in definition
                        else self.relation_types[definition["type"]].properties
                    )
                    if specification.get("empty") == "omit" and properties[field_name].required:
                        raise ValueError("Required mapped fields cannot omit empty values")
        return self


class EmbeddingProfile(StrictModel):
    revision_id: str = Field(default_factory=new_id)
    provider: Literal["openai", "bailian"] = "openai"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen3.7-text-embedding"
    dimension: int = Field(default=1024, ge=1, le=65536)
    distance: Literal["Cosine", "Dot", "Euclid"] = "Cosine"
    api_key_env: str = "DASHSCOPE_API_KEY"
    batch_size: int = Field(default=10, ge=1, le=128)
    timeout: float = Field(default=60, gt=0)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe(self):
        valid_id(self.revision_id)
        if set(self.extra_body) & {"model", "input", "dimensions", "encoding_format"}:
            raise ValueError("extra_body cannot override embedding profile fields")
        from uuid import UUID

        if UUID(self.revision_id).version != 4:
            raise ValueError("Embedding revision_id must be UUIDv4")
        if any("key" in k.lower() or "token" in k.lower() or "secret" in k.lower() for k in self.extra_body):
            raise ValueError("Credentials must be supplied through api_key_env")
        if "@" in self.base_url or "?" in self.base_url:
            raise ValueError("Base URL cannot contain credentials or query parameters")
        return self


def normalize(value: Any, rules: list[str]):
    """按配置顺序规范化字符串，非字符串值保持原样。"""
    if not isinstance(value, str):
        return value
    for rule in rules:
        if rule == "trim":
            value = value.strip()
        elif rule == "lower":
            value = value.lower()
        elif rule == "upper":
            value = value.upper()
        elif rule == "casefold":
            value = value.casefold()
        elif rule in {"nfc", "nfkc"}:
            value = unicodedata.normalize(rule.upper(), value)
        elif rule == "collapse_whitespace":
            value = " ".join(value.split())
    return value


def identity_value(node: NodeType, values: dict, document_id: str) -> dict:
    """校验并规范化身份字段，仅来源内身份附加 document_id 隔离不同资料。"""
    result = {"namespace": node.identity.namespace, "fields": {}}
    for field in node.identity.fields:
        if field not in values or values[field] is None or values[field] == "":
            raise KGError("MISSING_IDENTITY", f"Missing identity field: {field}")
        rules = node.identity.normalization
        result["fields"][field] = normalize(
            values[field], rules if isinstance(rules, list) else rules.get(field, [])
        )
        if result["fields"][field] == "":
            raise KGError("MISSING_IDENTITY", f"Empty normalized identity: {field}")
    if node.identity.scope == "document":
        result["document_id"] = document_id
    validate_identity_key(result)
    return result


def check_value(value: Any, definition: PropertyDef) -> Any:
    """检查字段类型、查询投影范围和枚举约束，返回未经转换的原值。"""
    kind = definition.type
    valid = {
        "string": lambda: isinstance(value, str),
        "string_list": lambda: isinstance(value, list) and all(isinstance(x, str) for x in value),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "date": lambda: isinstance(value, str),
    }[kind]()
    if not valid:
        raise KGError("FIELD_TYPE_ERROR", f"Expected {kind}")

    # 仅供图查询投影的整数受存储范围限制；普通 JSON 数值保留自身范围。
    if (
        definition.queryable
        and kind in {"integer", "number"}
        and isinstance(value, int)
        and not MIN_PROJECTED_INTEGER <= value <= MAX_PROJECTED_INTEGER
    ):
        raise KGError(
            "FIELD_TYPE_ERROR",
            "Queryable integer values must fit the signed 64-bit projection range",
            min_value=MIN_PROJECTED_INTEGER,
            max_value=MAX_PROJECTED_INTEGER,
        )
    if kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise KGError("FIELD_TYPE_ERROR", "Expected ISO date") from exc
    try:
        encoded = canonical(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise KGError("FIELD_TYPE_ERROR", "Property value must be finite and JSON-serializable") from exc
    if definition.enum is not None and encoded not in {canonical(item) for item in definition.enum}:
        raise KGError("FIELD_TYPE_ERROR", "Value is not in configured enum")
    return value


def select_mapping(model, media_type, mapping_id):
    """选择显式映射或唯一适用映射，避免对结构化资料猜测字段关联。"""
    if media_type not in {"csv", "json"}:
        if mapping_id:
            raise KGError("INVALID_ARGUMENT", "Mappings apply only to CSV and JSON")
        return None, None
    if mapping_id:
        mapping = model.mappings.get(mapping_id)
        if not mapping or mapping["format"] != media_type:
            raise KGError("INVALID_MODEL", "Mapping does not apply to this file")
        return mapping_id, mapping
    suitable = [(key, value) for key, value in model.mappings.items() if value["format"] == media_type]
    if len(suitable) != 1:
        raise KGError("MAPPING_REQUIRED", "CSV/JSON needs one explicit applicable mapping")
    return suitable[0]
