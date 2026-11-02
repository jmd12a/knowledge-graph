from __future__ import annotations

from typing import Any, Literal
from typing_extensions import NotRequired, TypedDict

from pydantic import Field

from .knowledge import Entity, Relation
from .model import StrictModel
from .source import Contribution, VectorPoint, validate_stored_evidence

SUCCESS_STATES = {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}
# 队首未完成任务占据知识库维护顺序；正式写入后的状态必须优先修复。
HEAD_STATES = {"PREPARING", "PREPARED", "DRAINING", "APPLYING", "VERIFYING", "NEEDS_REPAIR"}
WRITE_STATES = {"APPLYING", "VERIFYING", "NEEDS_REPAIR"}


class Job(TypedDict):
    job_id: str
    kb_id: str
    operation: Literal["IMPORT", "DELETE", "REBUILD", "REINDEX"]
    document_id: str | None
    input: dict[str, Any]
    queue_seq: int
    state: str
    attempt: int
    plan_id: str | None
    plan_path: str | None
    last_step: str | None
    error: dict[str, Any] | None
    counts: dict[str, Any]
    created_at: str
    updated_at: str
    next_attempt_at: float
    cleanup_pending: int
    input_json: NotRequired[str]


class PreparedMutation(StrictModel):
    schema_version: Literal[1]
    plan_id: str
    job_id: str
    kb_id: str
    operation: Literal["IMPORT", "DELETE", "REBUILD", "REINDEX"]
    document_ids: list[str]
    model: dict[str, Any]
    model_revision: str
    embedding_profile: dict[str, Any]
    parser_settings: dict[str, Any]
    entities: list[Entity]
    relations: list[Relation]
    contributions: list[Contribution]
    chunk_points: list[VectorPoint]
    object_points: list[VectorPoint]
    issues: list[dict[str, Any]]
    issue_documents: list[str]
    delete_entities: list[str]
    delete_relations: list[str]
    delete_chunks: list[str]
    delete_objects: list[str]
    activate_documents: list[str]
    delete_documents: list[str]
    cleanup_paths: list[str]
    cleanup_job_ids: list[str] = Field(default_factory=list)
    warnings: list[dict[str, Any]]
    counts: dict[str, Any]


PREPARED_FIELDS = frozenset(PreparedMutation.model_fields)


def validate_atomic_reference_mappings(
    contributions: list[dict], chunks: dict, entities: list[dict] | None = None
) -> None:
    """Validate explicit reference pairs without inventing mappings for legacy statements."""
    from .common import canonical
    from .errors import KGError

    try:
        by_id = {claim["contribution_id"]: claim for claim in contributions}
        for claim in contributions:
            if not claim["field_path"].startswith("/atomic_groups/"):
                continue
            statement = claim["value"]
            if not isinstance(statement, dict):
                raise ValueError("Atomic statement must be an object")
            # Older statements recorded a count but never stored the endpoint pair mapping.
            if "resolved_references" not in statement:
                continue
            pairs = statement["resolved_references"]
            check = statement.get("completeness_check", {})
            if not isinstance(pairs, list) or not isinstance(check, dict):
                raise ValueError("Invalid atomic reference mapping structure")
            count = check.get("resolved_reference_count", 0)
            required = check.get("required_reference_count", 0)
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count != len(pairs)
                or isinstance(required, bool)
                or not isinstance(required, int)
                or required < 0
                or (required and not pairs)
            ):
                raise ValueError("Reference counts do not match the explicit pair mapping")
            flat = {canonical(reference) for reference in claim["evidence"]}
            for pair in pairs:
                if not isinstance(pair, dict) or set(pair) != {"reference", "target"}:
                    raise ValueError("A reference pair must contain exactly its two endpoints")
                for side in ("reference", "target"):
                    references = pair[side]
                    if not isinstance(references, list) or not references:
                        raise ValueError("Reference pair endpoints must be nonempty evidence arrays")
                    for reference in references:
                        if (
                            not isinstance(reference, dict)
                            or reference.get("role") != "fact"
                            or reference.get("document_id") != claim["document_id"]
                            or canonical(reference) not in flat
                        ):
                            raise ValueError("A pair endpoint is not fact evidence of its contribution")
                        chunk = chunks.get(reference["chunk_id"])
                        if (
                            not chunk
                            or chunk.get("kb_id") != claim["kb_id"]
                            or chunk.get("document_id") != claim["document_id"]
                        ):
                            raise ValueError("A pair endpoint crosses document or knowledge base scope")
                        validate_stored_evidence(reference, chunk)
        for entity in entities or []:
            for group_name, group in entity.get("atomic_groups", {}).items():
                for variant in group.get("declarations", []):
                    for source in variant.get("source_declarations", []):
                        claim = by_id.get(source.get("contribution_id"))
                        statement = claim.get("value") if claim else None
                        explicit = isinstance(statement, dict) and "resolved_references" in statement
                        if not explicit and "resolved_references" not in source:
                            continue
                        if (
                            not claim
                            or not isinstance(statement, dict)
                            or claim["target_kind"] != "entity"
                            or claim["target_id"] != entity["entity_id"]
                            or claim["field_path"] != "/atomic_groups/" + group_name
                            or source.get("document_id") != claim["document_id"]
                        ):
                            raise ValueError("A projected reference mapping has no matching contribution")
                        if (explicit and "resolved_references" not in source) or canonical(
                            source.get("resolved_references", [])
                        ) != canonical(statement.get("resolved_references", [])):
                            raise ValueError(
                                "Projected reference pairs differ from their source contribution"
                            )
                        if explicit:
                            expected = statement.get("completeness_check", {})
                            actual = source.get("completeness_check", {})
                            if not isinstance(actual, dict) or any(
                                canonical(actual.get(field, 0)) != canonical(expected.get(field, 0))
                                for field in ("resolved_reference_count", "required_reference_count")
                            ):
                                raise ValueError(
                                    "Projected reference counts differ from their source contribution"
                                )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise KGError(
            "EVIDENCE_INVALID", "Atomic reference pairs do not match their verified source contribution"
        ) from exc


def validate_prepared_mutation(plan: dict, job: dict | None = None) -> None:
    """校验持久化计划的结构、任务绑定和跨存储引用，拒绝不可安全重放的计划。"""
    import math
    from uuid import UUID
    from .common import canonical, valid_id
    from .errors import KGError
    from .model import BusinessModel, EmbeddingProfile, check_value, validate_identity_key

    try:
        PreparedMutation.model_validate(plan)
        if PREPARED_FIELDS - plan.keys():
            raise ValueError("Incomplete prepared mutation")
        model = BusinessModel.model_validate(plan["model"])
        profile = EmbeddingProfile.model_validate(plan["embedding_profile"])
        profile_data = profile.model_dump()
        for field in ("plan_id", "job_id", "kb_id", "model_revision"):
            valid_id(plan[field])
            if UUID(plan[field]).version != 4:
                raise ValueError("Prepared identifiers must use UUIDv4")
        if job is not None:
            if any(plan[key] != job[key] for key in ("job_id", "kb_id", "operation", "plan_id")):
                raise ValueError("Plan does not match its persisted job binding")
            if plan["operation"] == "REBUILD" and plan["model"] != job["input"]["model"]:
                raise ValueError("Target model differs from the immutable request")
            if (
                plan["operation"] == "REINDEX"
                and plan["embedding_profile"] != job["input"]["embedding_profile"]
            ):
                raise ValueError("Target embedding profile differs from the immutable request")

        # 各类记录的随机 ID 须各自唯一；实体与对象索引中的点须一一对应。
        groups = {
            "entities": [x["entity_id"] for x in plan["entities"]],
            "relations": [x["relation_id"] for x in plan["relations"]],
            "chunks": [x["id"] for x in plan["chunk_points"]],
            "contributions": [x["contribution_id"] for x in plan["contributions"]],
            "objects": [x["id"] for x in plan["object_points"]],
        }
        for values in groups.values():
            if len(values) != len(set(values)):
                raise ValueError("Duplicate prepared object identifier")
            for value in values:
                valid_id(value)
                if UUID(value).version != 4:
                    raise ValueError("Prepared object identifiers must use UUIDv4")
        entity_ids, relation_ids = set(groups["entities"]), set(groups["relations"])
        if set(groups["objects"]) != entity_ids:
            raise ValueError("Object index does not match the final entity set")
        document_ids = set(plan["document_ids"])
        if document_ids & set(plan["delete_documents"]) or set(plan["activate_documents"]) - document_ids:
            raise ValueError("Inconsistent document transition")
        for value in document_ids | set(plan["delete_documents"]) | set(plan["cleanup_job_ids"]):
            valid_id(value)

        chunks = {point["id"]: point["payload"] for point in plan["chunk_points"]}
        objects = {point["id"]: point["payload"] for point in plan["object_points"]}
        for point in plan["chunk_points"] + plan["object_points"]:
            if (
                point["payload"].get("kb_id") != plan["kb_id"]
                or point["payload"].get("embedding_profile") != profile_data
            ):
                raise ValueError("Index point has the wrong knowledge base or vector profile")
            vector = point["vector"]
            if len(vector) != profile.dimension or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector
            ):
                raise ValueError("Invalid prepared vector")
            if profile.distance == "Cosine" and not any(vector):
                raise ValueError("Zero cosine vector")
        for chunk_id, chunk in chunks.items():
            if (
                chunk.get("chunk_id") != chunk_id
                or chunk.get("document_id") not in document_ids
                or not isinstance(chunk.get("text"), str)
            ):
                raise ValueError("Invalid chunk business fields")
        for entity in plan["entities"]:
            validate_identity_key(entity["identity"])
            if entity["kb_id"] != plan["kb_id"] or set(entity["document_ids"]) - document_ids:
                raise ValueError("Entity crosses knowledge base or document scope")
            payload = objects[entity["entity_id"]]
            expected = {
                "kb_id": plan["kb_id"],
                "object_kind": "entity",
                "object_id": entity["entity_id"],
                "entity_type": entity["entity_type"],
                "name": entity["canonical_name"],
                "aliases": entity["aliases"],
                "domain_ids": entity["domain_ids"],
                "search_text": entity["search_text"],
                "object_revision": entity["object_revision"],
                "embedding_profile": profile_data,
            }
            if payload != expected:
                raise ValueError("Object payload differs from the full final business fields")
        for relation in plan["relations"]:
            validate_identity_key(relation["identity"])
            if (
                relation["kb_id"] != plan["kb_id"]
                or relation["source_id"] not in entity_ids
                or relation["target_id"] not in entity_ids
            ):
                raise ValueError("Invalid relation endpoint scope")
            if (
                relation["identity"].get("source_id") != relation["source_id"]
                or relation["identity"].get("target_id") != relation["target_id"]
                or relation["identity"].get("relation_type") != relation["relation_type"]
            ):
                raise ValueError("Relation identity differs from its business fields")

        # 引用必须同时满足知识库、来源资料、目标对象和精确证据映射约束。
        by_target = {}
        for contribution in plan["contributions"]:
            target_ids = entity_ids if contribution["target_kind"] == "entity" else relation_ids
            if (
                contribution["kb_id"] != plan["kb_id"]
                or contribution["target_id"] not in target_ids
                or contribution["document_id"] not in document_ids
                or contribution["chunk_id"] not in chunks
            ):
                raise ValueError("Invalid contribution references")
            if not contribution["evidence"]:
                raise ValueError("Contribution has no evidence")
            by_target.setdefault(contribution["target_id"], set()).add(contribution["contribution_id"])
            for evidence in contribution["evidence"]:
                chunk = chunks.get(evidence["chunk_id"])
                if (
                    not chunk
                    or chunk["document_id"] != contribution["document_id"]
                    or evidence["document_id"] != contribution["document_id"]
                ):
                    raise ValueError("Evidence crosses document scope")
                validate_stored_evidence(evidence, chunk)
        validate_atomic_reference_mappings(plan["contributions"], chunks, plan["entities"])
        for item in plan["entities"] + plan["relations"]:
            definition = (
                model.node_types[item["entity_type"]]
                if "entity_id" in item
                else model.relation_types[item["relation_type"]]
            )
            for field, property_definition in definition.properties.items():
                if property_definition.queryable:
                    for supported in item["properties"].get(field, {}).get("values", []):
                        check_value(supported["value"], property_definition)
            object_id = item.get("entity_id", item.get("relation_id"))
            if set(item["contribution_ids"]) != by_target.get(object_id, set()):
                raise ValueError("Final object contribution set is incomplete")
        canonical(plan)
    except (ValueError, TypeError, KeyError, OverflowError, KGError) as exc:
        raise KGError(
            "PLAN_INVALID",
            "Prepared mutation is structurally invalid or differs from its persisted binding",
            503,
        ) from exc
