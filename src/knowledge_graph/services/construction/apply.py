from __future__ import annotations

import asyncio
import math
from collections import defaultdict

from ...domain import KGError, SUCCESS_STATES
from ...domain.jobs import WRITE_STATES, validate_atomic_reference_mappings
from ...ports.resources import Resources
from ...runtime import blocking
from ...domain.source import validate_stored_evidence
from .maintenance import check_vector_capacity


class MutationApplier:
    def __init__(self, resources: Resources):
        self.c = resources

    async def apply(self, job):
        """排空读请求后重放完整计划，验证通过并提交完成状态后恢复可查询状态。"""
        c, kb_id = self.c, job["kb_id"]
        job = await c.metadata.job(job["job_id"])
        kb = await c.metadata.kb(kb_id)
        if job["state"] in SUCCESS_STATES and kb["maintenance_state"] == "READY":
            return
        if job["state"] not in {"PREPARED", "DRAINING", "APPLYING", "VERIFYING", "NEEDS_REPAIR"}:
            raise KGError("INVALID_JOB_STATE", "This task has no prepared mutation to apply", 409)
        plan = await blocking(c.files.read_plan, job)
        check_vector_capacity(
            len(plan["chunk_points"]), len(plan["object_points"]), c.settings.maintenance_scan_limit
        )

        # 进入写入阶段后必须按原计划修复，不能退回仍可查询的准备状态。
        writing_started = job["state"] in WRITE_STATES
        if kb["maintenance_job_id"] not in {None, job["job_id"]}:
            raise KGError("INVALID_JOB_STATE", "Another task owns this knowledge base's maintenance", 409)
        if not writing_started:
            await c.metadata.maintenance(kb_id, "DRAINING", job["job_id"])
            await c.metadata.update_job(job["job_id"], state="DRAINING")
        try:
            async with c.gate.exclusive(kb_id, c.settings.drain_timeout):
                await c.metadata.maintenance(kb_id, "APPLYING", job["job_id"])
                await c.metadata.update_job(job["job_id"], state="APPLYING", last_step="begin")
                # Every recovery replays the entire immutable plan, including each delete/upsert pair.
                # last_step 记录进度，不用于跳过已执行的跨库存储步骤。
                await c.vectors.ensure(
                    kb_id, plan["embedding_profile"], recreate=plan["operation"] == "REINDEX"
                )
                if plan["operation"] == "REBUILD":
                    await c.vectors.delete(kb_id, "chunks", all_points=True)
                else:
                    await c.vectors.delete(kb_id, "chunks", ids=plan["delete_chunks"])
                await c.vectors.upsert(kb_id, "chunks", plan["chunk_points"])
                await c.metadata.update_job(job["job_id"], last_step="chunks")

                # 重建索引不改来源贡献和图事实，其余维护操作按同一计划更新两者。
                if plan["operation"] != "REINDEX":
                    await c.metadata.replace_contributions(
                        kb_id, plan["contributions"], plan["issues"], plan["issue_documents"]
                    )
                    await c.metadata.update_job(job["job_id"], last_step="contributions")
                    await c.graph.apply(
                        kb_id,
                        plan["model"],
                        plan["entities"],
                        plan["relations"],
                        plan["delete_entities"],
                        plan["delete_relations"],
                        rebuild=plan["operation"] == "REBUILD",
                    )
                    await c.metadata.update_job(job["job_id"], last_step="graph")

                await c.vectors.delete(kb_id, "objects", ids=plan["delete_objects"])
                await c.vectors.upsert(kb_id, "objects", plan["object_points"])
                await c.metadata.update_job(job["job_id"], state="VERIFYING", last_step="objects")

                # 跨库结果核对成功后才回收原文件和检查点，并最终提交资料状态。
                await self.verify(plan)
                await c.metadata.update_job(job["job_id"], last_step="verified")
                for path in plan["cleanup_paths"]:
                    await blocking(c.files.remove, path)
                await c.metadata.update_job(job["job_id"], last_step="raw_cleanup")
                try:
                    await c.metadata.finish(job["job_id"], plan)
                except BaseException as exc:
                    # A lost acknowledgement is not evidence that the final SQLite transaction failed.
                    observed = await c.metadata.job(job["job_id"])
                    current = await c.metadata.kb(kb_id)
                    if observed["state"] not in SUCCESS_STATES or current["maintenance_state"] != "READY":
                        raise
                    if isinstance(exc, asyncio.CancelledError):
                        raise
        except KGError as exc:
            if exc.code == "DRAIN_TIMEOUT" and not writing_started:
                observed = await c.metadata.job(job["job_id"])
                current = await c.metadata.kb(kb_id)
                if (
                    observed["state"] == "DRAINING"
                    and current["maintenance_state"] == "DRAINING"
                    and current["maintenance_job_id"] == job["job_id"]
                ):
                    await c.metadata.maintenance(kb_id, "READY", None)
                    await c.metadata.update_job(job["job_id"], state="PREPARED")
            raise

    async def verify(self, plan):
        """核对各存储的最终快照，并验证来源、证据、对象与关系端点的完整关联。"""
        c, kb_id = self.c, plan["kb_id"]
        entities, relations = await c.graph.all(kb_id)
        if {item["entity_id"]: item for item in entities} != {
            item["entity_id"]: item for item in plan["entities"]
        } or {item["relation_id"]: item for item in relations} != {
            item["relation_id"]: item for item in plan["relations"]
        }:
            raise KGError("VERIFY_FAILED", "Graph contents do not match the prepared final state", 503)

        contributions = await c.metadata.contributions(kb_id)
        if {claim["contribution_id"]: claim for claim in contributions} != {
            claim["contribution_id"]: claim for claim in plan["contributions"]
        }:
            raise KGError("VERIFY_FAILED", "Source contributions do not match the prepared final state", 503)
        if plan["operation"] != "REINDEX":
            issues = await c.metadata.candidate_issues(plan["job_id"], len(plan["issues"]) + 1, 0)
            if {issue["issue_id"]: issue for issue in issues} != {
                issue["issue_id"]: issue for issue in plan["issues"]
            }:
                raise KGError("VERIFY_FAILED", "Candidate issues do not match the prepared task result", 503)

        chunks = {}
        for kind, expected in (("chunks", plan["chunk_points"]), ("objects", plan["object_points"])):
            stored = await c.vectors.all(kb_id, kind, max_points=c.settings.maintenance_scan_limit)
            actual = {point["id"]: point for point in stored}
            if len(actual) != len(stored) or set(actual) != {point["id"] for point in expected}:
                raise KGError(
                    "VERIFY_FAILED", "Index IDs do not match the prepared state", 503, collection=kind
                )
            for point in expected:
                observed = actual[point["id"]]
                if observed["payload"] != point["payload"] or not vector_equal(
                    point["vector"], observed["vector"], plan["embedding_profile"]["distance"]
                ):
                    raise KGError(
                        "VERIFY_FAILED",
                        "Index payload or vector does not match its prepared value",
                        503,
                        collection=kind,
                        point_id=point["id"],
                    )
            if kind == "chunks":
                chunks = {point_id: point["payload"] for point_id, point in actual.items()}

        # 单库内容一致还不够，继续检查跨库存储引用及原文坐标映射。
        entity_by_id = {entity["entity_id"]: entity for entity in entities}
        relation_by_id = {relation["relation_id"]: relation for relation in relations}
        by_target = defaultdict(set)
        by_document = defaultdict(set)
        for contribution in contributions:
            targets = entity_by_id if contribution["target_kind"] == "entity" else relation_by_id
            if (
                contribution["kb_id"] != kb_id
                or contribution["target_id"] not in targets
                or contribution["chunk_id"] not in chunks
                or chunks[contribution["chunk_id"]]["document_id"] != contribution["document_id"]
            ):
                raise KGError(
                    "VERIFY_FAILED",
                    "A contribution references an absent or cross-source object or chunk",
                    503,
                )
            for evidence in contribution["evidence"]:
                chunk = chunks.get(evidence["chunk_id"])
                if (
                    not chunk
                    or chunk["kb_id"] != kb_id
                    or chunk["document_id"] != evidence["document_id"]
                    or evidence["document_id"] != contribution["document_id"]
                ):
                    raise KGError(
                        "VERIFY_FAILED", "Evidence crosses source document or knowledge base boundaries", 503
                    )
                try:
                    validate_stored_evidence(evidence, chunk)
                except KGError as exc:
                    raise KGError(
                        "VERIFY_FAILED",
                        "Evidence cannot be verified against its complete indexed source mapping",
                        503,
                    ) from exc
            key = (contribution["target_kind"], contribution["target_id"])
            by_target[key].add(contribution["contribution_id"])
            by_document[key].add(contribution["document_id"])

        try:
            validate_atomic_reference_mappings(contributions, chunks, entities)
        except KGError as exc:
            raise KGError(
                "VERIFY_FAILED", "Atomic reference mappings do not match the indexed source evidence", 503
            ) from exc
        for item in entities + relations:
            object_id = item["entity_id"] if item["kind"] == "entity" else item["relation_id"]
            key = (item["kind"], object_id)
            if (
                item["kb_id"] != kb_id
                or set(item["contribution_ids"]) != by_target[key]
                or set(item["document_ids"]) != by_document[key]
            ):
                raise KGError(
                    "VERIFY_FAILED",
                    "An object's source references do not match its complete contributions",
                    503,
                )
        for relation in relations:
            if relation["source_id"] not in entity_by_id or relation["target_id"] not in entity_by_id:
                raise KGError(
                    "VERIFY_FAILED", "A relation endpoint is absent from the same knowledge base", 503
                )


def vector_equal(expected, actual, distance):
    """按存储浮点容差比较向量；Cosine 的预期值先对齐存储端归一化结果。"""
    if len(expected) != len(actual) or any(not math.isfinite(value) for value in actual):
        return False
    if distance == "Cosine":
        norm = math.sqrt(sum(value * value for value in expected))
        expected = [value / norm for value in expected] if norm else expected
    return all(
        math.isclose(left, right, rel_tol=2e-5, abs_tol=2e-6)
        for left, right in zip(expected, actual, strict=True)
    )
