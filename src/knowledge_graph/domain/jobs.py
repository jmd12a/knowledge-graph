from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from typing_extensions import NotRequired, TypedDict

from .knowledge import Entity, Relation
from .model import StrictModel
from .source import Contribution, VectorPoint, validate_stored_evidence

SUCCESS_STATES = {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}
# 队首未完成任务占据知识库维护顺序；正式写入后的状态必须优先修复。
HEAD_STATES = {
    "PREPARING",
    "PREPARED",
    "DRAINING",
    "APPLYING",
    "VERIFYING",
    "NEEDS_REPAIR",
    "CLEANING",
    "CLEANUP_FAILED",
}
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
    retry_generation: int
    input_json: NotRequired[str]


class MutationWriteSet(StrictModel):
    entity_ids: list[str]
    relation_ids: list[str]
    chunk_ids: list[str]
    object_ids: list[str]
    contribution_document_ids: list[str]


class VerificationScope(StrictModel):
    endpoint_ids: list[str]
    evidence_chunk_ids: list[str]
    relation_groups: list[list[str]]


class ChunkVectorMember(StrictModel):
    kb_id: str
    chunk_id: str
    document_id: str
    vector_id: str


class ChunkIndexPlan(StrictModel):
    points: list[VectorPoint]
    members: list[ChunkVectorMember]
    write_ids: list[str]
    delete_ids: list[str]
    current_count: int = Field(ge=0)


class PreparedMutation(StrictModel):
    schema_version: Literal[4, 5]
    chunk_index: ChunkIndexPlan
    verification: VerificationScope | None = None
    write_mode: Literal["full", "delta"] | None = None
    write_set: MutationWriteSet | None = None
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
    identity_dependency_targets: list[list[str]] = Field(default_factory=list)
    identity_dependency_projection: dict[str, list[str]] | None = None
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


def prepared_fields(plan: dict) -> frozenset[str]:
    return (
        PREPARED_FIELDS - {"identity_dependency_targets", "identity_dependency_projection"}
        if plan.get("schema_version") == 4
        else PREPARED_FIELDS
    )


def validate_write_set(plan: dict, groups: dict, job: dict | None) -> None:
    mode, scope = plan["write_mode"], plan["write_set"]
    expected_mode = "delta" if plan["operation"] in {"IMPORT", "DELETE"} else "full"
    if mode != expected_mode or not isinstance(scope, dict):
        raise ValueError("Write mode does not match the maintenance operation")
    for field, group in (
        ("entity_ids", "entities"),
        ("relation_ids", "relations"),
        ("chunk_ids", "chunks"),
        ("object_ids", "objects"),
    ):
        values = scope[field]
        selected, final = set(values), set(groups[group])
        if len(values) != len(selected) or selected - final or (mode == "full" and selected != final):
            raise ValueError("Write set does not match the prepared final objects")
    documents = scope["contribution_document_ids"]
    selected = set(documents)
    expected = set(plan["activate_documents"]) | set(plan["delete_documents"])
    if mode == "full":
        expected |= set(plan["document_ids"])
    if len(documents) != len(selected) or selected != expected:
        raise ValueError("Contribution write scope does not match the document transition")
    if mode == "delta":
        if len(selected) != 1 or (job is not None and selected != {job["document_id"]}):
            raise ValueError("Incremental writes must belong to the task's source document")
        if plan["operation"] == "IMPORT" and (plan["delete_documents"] or not plan["activate_documents"]):
            raise ValueError("An import must activate its source document")
        if plan["operation"] == "DELETE" and (plan["activate_documents"] or not plan["delete_documents"]):
            raise ValueError("A deletion must withdraw its source document")


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


def _validate_chunk_index(plan, chunks, document_ids):
    from .common import valid_id

    index = plan["chunk_index"]
    points = {point["id"]: point for point in index["points"]}
    members = {row["chunk_id"]: row for row in index["members"]}
    writes, deleted = set(index["write_ids"]), set(index["delete_ids"])
    if (
        len(points) != len(index["points"])
        or len(members) != len(index["members"])
        or len(writes) != len(index["write_ids"])
        or len(deleted) != len(index["delete_ids"])
        or writes - points.keys()
        or deleted & points.keys()
        or {row["vector_id"] for row in members.values()} != points.keys()
        or chunks.keys() - members.keys()
        or members.keys() & set(plan["delete_chunks"])
    ):
        raise ValueError("Chunk vector groups do not match the write and verification scope")
    if plan["write_mode"] == "full" and (
        members.keys() != chunks.keys()
        or writes != points.keys()
        or deleted
        or index["current_count"] != len(points)
    ):
        raise ValueError("Full rebuild must cover all chunk vector groups")
    for key in points.keys() | deleted | members.keys():
        valid_id(key)
    for chunk_id, member in members.items():
        if member["kb_id"] != plan["kb_id"] or member["document_id"] not in document_ids:
            raise ValueError("Chunk vector membership crosses document scope")
        if chunk_id in chunks and chunks[chunk_id]["document_id"] != member["document_id"]:
            raise ValueError("Chunk vector membership belongs to a different source")
    for vector_id, point in points.items():
        payload = point["payload"]
        representative = payload["representative_chunk_id"]
        member = members.get(representative)
        if not member or member["vector_id"] != vector_id or not isinstance(payload["text"], str):
            raise ValueError("Representative must belong to its own vector group")
        if representative in chunks and chunks[representative]["text"] != payload["text"]:
            raise ValueError("Representative text differs from its source chunk")


def validate_prepared_mutation(plan: dict, job: dict | None = None) -> None:
    """校验持久化计划的结构、任务绑定和跨存储引用，拒绝不可安全重放的计划。"""
    import math
    from uuid import UUID

    from .common import canonical, valid_id
    from .errors import KGError
    from .model import BusinessModel, EmbeddingProfile, check_value, validate_identity_key
    from .source import effective_contributions

    try:
        PreparedMutation.model_validate(plan)
        if type(plan["schema_version"]) is not int or set(plan) != prepared_fields(plan):
            raise ValueError("Prepared mutation fields do not match its schema version")
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
        active_contributions, suppressed_dependencies = effective_contributions(plan["contributions"])
        if plan["schema_version"] == 5:
            projection = plan["identity_dependency_projection"]
            if (
                projection is None
                or set(projection) != {"active", "suppressed"}
                or (
                    projection["active"] != sorted(row["contribution_id"] for row in active_contributions)
                    or projection["suppressed"]
                    != sorted(row["contribution_id"] for row in suppressed_dependencies)
                )
            ):
                raise ValueError("Identity dependency projection differs from its raw contributions")
            for pair in plan["identity_dependency_targets"]:
                if len(pair) != 2 or pair[0] not in {"entity", "relation"}:
                    raise ValueError("Invalid identity dependency target")
                valid_id(pair[1])
        validate_write_set(plan, groups, job)
        entity_ids, relation_ids = set(groups["entities"]), set(groups["relations"])
        if set(groups["objects"]) != entity_ids:
            raise ValueError("Object index does not match the final entity set")
        verification = plan.get("verification")
        external_chunks, external_endpoints = set(), set()
        if (plan["write_mode"] == "delta") != (verification is not None):
            raise ValueError("Verification scope does not match the write mode")
        if verification is not None:
            external_chunks = set(verification["evidence_chunk_ids"])
            external_endpoints = set(verification["endpoint_ids"])
            if external_chunks & (
                set(groups["chunks"]) | set(plan["delete_chunks"])
            ) or external_endpoints & (entity_ids | set(plan["delete_entities"])):
                raise ValueError("External verification references overlap written or deleted records")
            for key in external_chunks | external_endpoints:
                valid_id(key)
            for group in verification["relation_groups"]:
                if len(group) != 2 or not model.relation_types[group[1]].conflict_rule.get(
                    "max_distinct_targets"
                ):
                    raise ValueError("Invalid relation conflict group")
                valid_id(group[0])

        document_ids = set(plan["document_ids"])
        if document_ids & set(plan["delete_documents"]) or set(plan["activate_documents"]) - document_ids:
            raise ValueError("Inconsistent document transition")
        for value in document_ids | set(plan["delete_documents"]) | set(plan["cleanup_job_ids"]):
            valid_id(value)

        chunks = {point["id"]: point["payload"] for point in plan["chunk_points"]}
        objects = {point["id"]: point["payload"] for point in plan["object_points"]}
        for point in plan["chunk_points"] + plan["object_points"] + plan["chunk_index"]["points"]:
            if (
                point["payload"].get("kb_id") != plan["kb_id"]
                or point["payload"].get("embedding_profile") != profile_data
            ):
                raise ValueError("Index point has the wrong knowledge base or vector profile")
        if any(point["vector"] is not None for point in plan["chunk_points"]):
            raise ValueError("Source chunks must be stored as payload-only points")
        for point in plan["object_points"] + plan["chunk_index"]["points"]:
            vector = point["vector"]
            if (
                not isinstance(vector, list)
                or len(vector) != profile.dimension
                or any(
                    isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                    for x in vector
                )
            ):
                raise ValueError("Invalid prepared vector")
            if profile.distance == "Cosine" and not any(vector):
                raise ValueError("Zero cosine vector")
        _validate_chunk_index(plan, chunks, document_ids)
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
        allowed_endpoints = entity_ids | external_endpoints
        for relation in plan["relations"]:
            validate_identity_key(relation["identity"])
            if (
                relation["kb_id"] != plan["kb_id"]
                or relation["source_id"] not in allowed_endpoints
                or relation["target_id"] not in allowed_endpoints
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
        active_ids = {row["contribution_id"] for row in active_contributions}
        for contribution in plan["contributions"]:
            target_ids = entity_ids if contribution["target_kind"] == "entity" else relation_ids
            if (
                contribution["kb_id"] != plan["kb_id"]
                or (
                    contribution["contribution_id"] in active_ids
                    and contribution["target_id"] not in target_ids
                )
                or contribution["document_id"] not in document_ids
                or (
                    contribution["chunk_id"] not in chunks and contribution["chunk_id"] not in external_chunks
                )
            ):
                raise ValueError("Invalid contribution references")
            if not contribution["evidence"]:
                raise ValueError("Contribution has no evidence")
            if contribution["contribution_id"] in active_ids:
                by_target.setdefault(contribution["target_id"], set()).add(contribution["contribution_id"])
            for evidence in contribution["evidence"]:
                if evidence["document_id"] != contribution["document_id"]:
                    raise ValueError("Evidence crosses document scope")
                chunk = chunks.get(evidence["chunk_id"])
                if chunk is not None:
                    if chunk["document_id"] != contribution["document_id"]:
                        raise ValueError("Evidence crosses document scope")
                    validate_stored_evidence(evidence, chunk)
                elif evidence["chunk_id"] not in external_chunks:
                    raise ValueError("Evidence is outside the verification scope")
        # 旧来源切片由写后校验按 ID 读取，避免在计划中复制它们的正文。
        if not external_chunks:
            validate_atomic_reference_mappings(active_contributions, chunks, plan["entities"])
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
