from __future__ import annotations

from pathlib import Path

from ...domain import KGError
from ...domain.jobs import HEAD_STATES
from ...runtime import blocking


def check_vector_capacity(chunk_count, object_count, max_points):
    """确保最终索引可在维护扫描预算内完整验证。"""
    for kind, count in (("chunks", chunk_count), ("objects", object_count)):
        if count > max_points:
            raise KGError(
                "SCAN_BUDGET_EXCEEDED",
                "Final vector index exceeds the maintenance verification budget; increase the budget before retrying",
                422,
                collection=kind,
                max_points=max_points,
                required_points=count,
                execution_complete=False,
            )


async def deletion_cleanup(resources, job, document):
    """计算验证后要回收的来源文件和相关任务检查点，此处不执行删除。"""
    document_id = document["document_id"]
    cleanup, cleanup_job_ids = [], []
    if document.get("raw_path"):
        cleanup.append(str(Path(document["raw_path"]).parent))
    for previous in await resources.metadata.jobs_for_kb(job["kb_id"]):
        if previous["job_id"] == job["job_id"]:
            continue
        if previous["state"] in HEAD_STATES:
            if previous.get("document_id") == document_id:
                raise KGError(
                    "INVALID_JOB_STATE",
                    "An unfinished task must be repaired before deleting its source",
                    409,
                    job_id=previous["job_id"],
                )
            continue
        matches = previous.get("document_id") == document_id
        directory = resources.files.job_dir(previous["job_id"])
        exists = await blocking(resources.files.exists, directory)
        # 重建等任务可能间接保存该来源，不能只检查任务的 document_id。
        if exists and not matches:
            for content in await blocking(resources.files.job_checkpoints, previous["job_id"]):
                payload = content.get("payload", content)
                referenced = set(payload.get("document_ids", [])) | set(payload.get("documents", {}))
                referenced.update(payload.get("baseline_document_ids", []))
                if document_id in referenced:
                    matches = True
                    break
        if matches:
            if exists:
                cleanup.append(str(directory))
            cleanup_job_ids.append(previous["job_id"])
    return sorted(set(cleanup)), sorted(set(cleanup_job_ids))


def task_counts(
    operation,
    contributions,
    entities,
    relations,
    chunk_points,
    issues,
    documents,
    old_entities,
    old_relations,
):
    """分别统计本次操作涉及的对象数量与维护后的 current_* 全库总量。"""
    scope = set(documents)
    if operation == "REINDEX":
        scoped = []
        adopted_entities = adopted_relations = set()
        chunks = len(chunk_points)
    elif operation == "DELETE":
        scoped = []
        adopted_entities = {item["entity_id"] for item in old_entities} - {
            item["entity_id"] for item in entities
        }
        adopted_relations = {item["relation_id"] for item in old_relations} - {
            item["relation_id"] for item in relations
        }
        chunks = 0
    else:
        scoped = [claim for claim in contributions if claim["document_id"] in scope]
        adopted_entities = {claim["target_id"] for claim in scoped if claim["target_kind"] == "entity"}
        adopted_relations = {claim["target_id"] for claim in scoped if claim["target_kind"] == "relation"}
        chunks = sum(point["payload"]["document_id"] in scope for point in chunk_points)
    return {
        "entities": len(adopted_entities),
        "relations": len(adopted_relations),
        "chunks": chunks,
        "adopted_contributions": len(scoped),
        "rejected_candidates": len(issues),
        "current_entities": len(entities),
        "current_relations": len(relations),
        "current_chunks": len(chunk_points),
        "current_contributions": len(contributions),
    }


def conflict_warnings(entities, relations, affected_ids=None):
    affected_ids = None if affected_ids is None else set(affected_ids)
    output = []
    for item in entities + relations:
        object_id = item["entity_id"] if item["kind"] == "entity" else item["relation_id"]
        if item["conflicts"] and (affected_ids is None or object_id in affected_ids):
            output.append(
                {
                    "code": "SOURCE_CONFLICT",
                    "kind": item["kind"],
                    "object_id": object_id,
                    "conflicts": item["conflicts"],
                }
            )
    return output
