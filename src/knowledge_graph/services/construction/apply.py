from __future__ import annotations

import asyncio
import math
from collections import defaultdict

from ...domain import SUCCESS_STATES, KGError
from ...domain.jobs import WRITE_STATES, validate_atomic_reference_mappings
from ...domain.source import validate_stored_evidence
from ...ports.resources import Resources
from ...runtime import blocking
from .changes import select_writes
from .maintenance import check_vector_capacity
from .merge import effective_contributions


class MutationApplier:
    def __init__(self, resources: Resources):
        self.c = resources

    async def apply(self, job):
        """排空读请求后重放固定写入范围，完整校验通过后恢复可查询状态。"""
        c, kb_id = self.c, job["kb_id"]
        job = await c.metadata.job(job["job_id"])
        if job["state"] == "CLEANING":
            await self._complete_cleanup(job["job_id"], await blocking(c.files.read_plan, job))
            return

        kb = await c.metadata.kb(kb_id)
        if job["state"] in SUCCESS_STATES and kb["maintenance_state"] == "READY":
            return
        if job["state"] not in {"PREPARED", "DRAINING", "APPLYING", "VERIFYING", "NEEDS_REPAIR"}:
            raise KGError("INVALID_JOB_STATE", "This task has no prepared mutation to apply", 409)
        plan = await blocking(c.files.read_plan, job)
        writes = select_writes(plan)
        capacity = (
            plan["counts"]
            if plan.get("verification")
            else {
                "current_chunks": len(plan["chunk_points"]),
                "current_entities": len(plan["object_points"]),
            }
        )
        check_vector_capacity(
            capacity["current_chunks"], capacity["current_entities"], c.settings.maintenance_scan_limit
        )

        if kb["maintenance_job_id"] not in {None, job["job_id"]}:
            raise KGError("INVALID_JOB_STATE", "Another task owns this knowledge base's maintenance", 409)
        # 已开始写入的任务重放原计划，无需再次切换到排空状态。
        if job["state"] not in WRITE_STATES:
            await c.metadata.start_publication(job)

        async with c.gate.exclusive(kb_id, c.settings.drain_timeout):
            await c.metadata.begin_publication_writes(job)
            needs_cleanup = await self._publish_plan(kb_id, job["job_id"], plan, writes)

        if needs_cleanup:
            await self._complete_cleanup(job["job_id"], plan)

    async def _publish_plan(self, kb_id, job_id, plan, writes):
        """在排他锁内重放完整计划，核验后提交发布结果。"""
        c = self.c
        operation = plan["operation"]
        # last_step 只记录进度。恢复时仍重放所有写入。
        await c.vectors.ensure(kb_id, plan["embedding_profile"], recreate=operation == "REINDEX")
        if operation in {"IMPORT", "DELETE"} and plan["write_mode"] == "delta":
            await self._write_delta(plan, writes)
        else:
            await self._write_chunks(plan, writes)
            await self._write_members(plan)
            await c.metadata.update_job(job_id, last_step="chunks")
            if operation != "REINDEX":
                await self._write_contributions(plan, writes)
                await c.metadata.update_job(job_id, last_step="contributions")
                await self._write_graph(plan, writes)
                await c.metadata.update_job(job_id, last_step="graph")
            await self._write_object_vectors(plan, writes)

        await c.metadata.update_job(job_id, state="VERIFYING", last_step="objects")
        await self.verify(plan)
        await c.metadata.update_job(job_id, last_step="verified")
        try:
            return await c.metadata.finish(job_id, plan)
        except BaseException as exc:
            # 提交响应丢失时，用持久化状态判断发布是否已经生效。
            observed = await c.metadata.job(job_id)
            current = await c.metadata.kb(kb_id)
            cleanup = observed["state"] == "CLEANING"
            if current["maintenance_state"] != "READY" or (
                not cleanup and observed["state"] not in SUCCESS_STATES
            ):
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            return cleanup

    async def _write_delta(self, plan, writes):
        async def write_vectors():
            await self._write_chunks(plan, writes)
            await self._write_object_vectors(plan, writes)

        async def write_metadata():
            await self._write_members(plan)
            await self._write_contributions(plan, writes)

        pending = asyncio.gather(
            write_vectors(),
            write_metadata(),
            self._write_graph(plan, writes),
            return_exceptions=True,
        )
        try:
            results = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # 仍持有排他锁时排空已经发起的写入。
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
            raise
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _write_chunks(self, plan, writes):
        vectors, kb_id = self.c.vectors, plan["kb_id"]
        rebuild = plan["operation"] == "REBUILD"
        index = plan["chunk_index"]
        await vectors.delete(
            kb_id, "chunks", ids=None if rebuild else plan["delete_chunks"], all_points=rebuild
        )
        await vectors.upsert(kb_id, "chunks", writes["chunk_points"])
        await vectors.delete(
            kb_id, "chunk_vectors", ids=None if rebuild else index["delete_ids"], all_points=rebuild
        )
        write_ids = set(index["write_ids"])
        await vectors.upsert(
            kb_id, "chunk_vectors", [point for point in index["points"] if point["id"] in write_ids]
        )

    async def _write_members(self, plan):
        index = plan["chunk_index"]
        scope = (
            None
            if plan["write_mode"] == "full"
            else sorted({point["id"] for point in index["points"]} | set(index["delete_ids"]))
        )
        await self.c.metadata.replace_vector_members(plan["kb_id"], scope, index["members"])

    async def _write_contributions(self, plan, writes):
        scope = plan["write_set"]["contribution_document_ids"] if plan["write_mode"] == "delta" else None
        await self.c.metadata.replace_contributions(
            plan["kb_id"],
            writes["contributions"],
            plan["issues"],
            plan["issue_documents"],
            document_ids=scope,
        )

    async def _write_graph(self, plan, writes):
        await self.c.graph.apply(
            plan["kb_id"],
            plan["model"],
            writes["entities"],
            writes["relations"],
            plan["delete_entities"],
            plan["delete_relations"],
            rebuild=plan["operation"] == "REBUILD",
        )

    async def _write_object_vectors(self, plan, writes):
        await self.c.vectors.delete(plan["kb_id"], "objects", ids=plan["delete_objects"])
        await self.c.vectors.upsert(plan["kb_id"], "objects", writes["object_points"])

    async def _complete_cleanup(self, job_id, plan):
        for path in plan["cleanup_paths"]:
            await blocking(self.c.files.remove, path)
        await self.c.metadata.update_job(
            job_id,
            state="SUCCEEDED_WITH_WARNINGS" if plan["warnings"] else "SUCCEEDED",
            last_step="committed",
            error_json=None,
        )

    async def verify(self, plan):
        """核对计划范围内的最终状态、来源证据与关系端点。"""
        c, kb_id = self.c, plan["kb_id"]
        scope = plan.get("verification")
        if scope:
            entities, relations, contributions = await self._read_scoped_result(plan, scope)
        else:
            entities, relations = await c.graph.all(kb_id)
            contributions = await c.metadata.contributions(kb_id)
        if {item["entity_id"]: item for item in entities} != {
            item["entity_id"]: item for item in plan["entities"]
        } or {item["relation_id"]: item for item in relations} != {
            item["relation_id"]: item for item in plan["relations"]
        }:
            raise KGError("VERIFY_FAILED", "Graph contents do not match the prepared final state", 503)

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

        chunks = await self._verify_vector_indexes(plan, scope)
        await self._verify_chunk_members(plan, chunks)
        external_endpoint_ids = set()
        if scope:
            reference_points = await c.vectors.read(kb_id, "chunks", scope["evidence_chunk_ids"])
            if {point["id"] for point in reference_points} != set(scope["evidence_chunk_ids"]):
                raise KGError("VERIFY_FAILED", "A source evidence chunk is missing", 503)
            chunks.update({point["id"]: point["payload"] for point in reference_points})
            endpoints = await c.graph.by_id(kb_id, "entity", scope["endpoint_ids"])
            external_endpoint_ids = {item["entity_id"] for item in endpoints}
            if external_endpoint_ids != set(scope["endpoint_ids"]):
                raise KGError("VERIFY_FAILED", "An external relation endpoint is missing", 503)

        # 单库内容一致还不够，继续检查跨库存储引用及原文坐标映射。
        entity_by_id = {entity["entity_id"]: entity for entity in entities}
        relation_by_id = {relation["relation_id"]: relation for relation in relations}
        active_contributions, suppressed_dependencies = effective_contributions(contributions)
        projection = plan.get("identity_dependency_projection")
        if projection and (
            sorted(row["contribution_id"] for row in active_contributions) != projection["active"]
            or sorted(row["contribution_id"] for row in suppressed_dependencies) != projection["suppressed"]
        ):
            raise KGError("VERIFY_FAILED", "Identity dependency projection changed after preparation", 503)
        active_ids = {row["contribution_id"] for row in active_contributions}
        by_target = defaultdict(set)
        by_document = defaultdict(set)
        for contribution in contributions:
            targets = entity_by_id if contribution["target_kind"] == "entity" else relation_by_id
            if (
                contribution["kb_id"] != kb_id
                or (
                    contribution["contribution_id"] in active_ids and contribution["target_id"] not in targets
                )
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
            if contribution["contribution_id"] in active_ids:
                key = (contribution["target_kind"], contribution["target_id"])
                by_target[key].add(contribution["contribution_id"])
                by_document[key].add(contribution["document_id"])

        try:
            validate_atomic_reference_mappings(active_contributions, chunks, entities)
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
        endpoint_ids = entity_by_id.keys() | external_endpoint_ids
        for relation in relations:
            if relation["source_id"] not in endpoint_ids or relation["target_id"] not in endpoint_ids:
                raise KGError(
                    "VERIFY_FAILED", "A relation endpoint is absent from the same knowledge base", 503
                )

    async def _verify_vector_indexes(self, plan, scope):
        """核对计划涉及的索引点，保留切片供证据校验。"""
        c, kb_id = self.c, plan["kb_id"]
        chunks = {}
        index = plan["chunk_index"]
        for kind, expected, deleted in (
            ("chunks", plan["chunk_points"], plan["delete_chunks"]),
            ("objects", plan["object_points"], plan["delete_objects"]),
            ("chunk_vectors", index["points"], index["delete_ids"]),
        ):
            dense = kind != "chunks"
            if scope:
                ids = sorted({point["id"] for point in expected} | set(deleted))
                stored = await c.vectors.read(kb_id, kind, ids, with_vectors=dense)
            else:
                stored = await c.vectors.all(
                    kb_id,
                    kind,
                    with_vectors=dense,
                    max_points=c.settings.maintenance_scan_limit,
                )
            actual = {point["id"]: point for point in stored}
            if len(actual) != len(stored) or set(actual) != {point["id"] for point in expected}:
                raise KGError(
                    "VERIFY_FAILED", "Index IDs do not match the prepared state", 503, collection=kind
                )
            for point in expected:
                observed = actual[point["id"]]
                if observed["payload"] != point["payload"] or (
                    dense
                    and not vector_equal(
                        point["vector"], observed["vector"], plan["embedding_profile"]["distance"]
                    )
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
        return chunks

    async def _verify_chunk_members(self, plan, chunks):
        c, kb_id, index = self.c, plan["kb_id"], plan["chunk_index"]
        if plan.get("verification"):
            vector_ids = sorted({point["id"] for point in index["points"]} | set(index["delete_ids"]))
            rows, source_rows = await asyncio.gather(
                c.metadata.vector_members(kb_id, vector_ids=vector_ids),
                c.metadata.vector_members(kb_id, document_ids=plan["write_set"]["contribution_document_ids"]),
            )
            rows += source_rows
        else:
            rows = await c.metadata.vector_members(kb_id)
        if {row["chunk_id"]: row for row in rows} != {row["chunk_id"]: row for row in index["members"]}:
            raise KGError("VERIFY_FAILED", "Chunk vector membership differs from the prepared plan", 503)
        if await c.vectors.count(kb_id, "chunk_vectors") != index["current_count"]:
            raise KGError("VERIFY_FAILED", "Chunk vector count differs from the prepared plan", 503)
        missing = sorted({row["chunk_id"] for row in index["members"]} - chunks.keys())
        external = await c.vectors.read(kb_id, "chunks", missing)
        sources = chunks | {point["id"]: point["payload"] for point in external}
        for row in index["members"]:
            source = sources.get(row["chunk_id"])
            if not source or source["document_id"] != row["document_id"]:
                raise KGError("VERIFY_FAILED", "A chunk vector member lacks its source payload", 503)
        for point in index["points"]:
            payload = point["payload"]
            if sources[payload["representative_chunk_id"]]["text"] != payload["text"]:
                raise KGError("VERIFY_FAILED", "A chunk vector differs from its representative text", 503)

    async def _read_scoped_result(self, plan, scope):
        c, kb_id = self.c, plan["kb_id"]
        entity_ids = sorted({item["entity_id"] for item in plan["entities"]} | set(plan["delete_entities"]))
        relation_ids = sorted(
            {item["relation_id"] for item in plan["relations"]} | set(plan["delete_relations"])
        )
        targets = [("entity", key) for key in entity_ids] + [("relation", key) for key in relation_ids]
        document_ids = plan["write_set"]["contribution_document_ids"]
        entities, direct, grouped, claims, source_claims = await asyncio.gather(
            c.graph.by_id(kb_id, "entity", entity_ids),
            c.graph.by_id(kb_id, "relation", relation_ids),
            c.graph.relation_groups(kb_id, scope["relation_groups"]),
            c.metadata.target_contributions(kb_id, targets),
            c.metadata.contributions(kb_id, document_ids),
        )
        relations = list({item["relation_id"]: item for item in direct + grouped}.values())
        contributions = list({item["contribution_id"]: item for item in claims + source_claims}.values())
        graph_counts, claim_count, chunk_count, object_count, source_chunk_count = await asyncio.gather(
            c.graph.counts(kb_id),
            c.metadata.contribution_count(kb_id),
            c.vectors.count(kb_id, "chunks"),
            c.vectors.count(kb_id, "objects"),
            c.vectors.count(kb_id, "chunks", document_ids=document_ids),
        )
        counts = plan["counts"]
        if (
            graph_counts["entities"] != counts["current_entities"]
            or graph_counts["relations"] != counts["current_relations"]
            or claim_count != counts["current_contributions"]
            or chunk_count != counts["current_chunks"]
            or object_count != counts["current_entities"]
            or source_chunk_count != len(plan["chunk_points"])
        ):
            raise KGError("VERIFY_FAILED", "Storage counts do not match the scoped maintenance plan", 503)
        return entities, relations, contributions


def vector_equal(expected, actual, distance):
    """按存储浮点容差比较向量；Cosine 的预期值先对齐存储端归一化结果。"""
    if (
        not isinstance(actual, list)
        or len(expected) != len(actual)
        or any(not math.isfinite(value) for value in actual)
    ):
        return False
    if distance == "Cosine":
        norm = math.sqrt(sum(value * value for value in expected))
        expected = [value / norm for value in expected] if norm else expected
    return all(
        math.isclose(left, right, rel_tol=2e-5, abs_tol=2e-6)
        for left, right in zip(expected, actual, strict=True)
    )
