from __future__ import annotations

import asyncio
import logging
import math

from ...domain import BusinessModel, EmbeddingProfile, KGError, new_id
from ...ports.resources import Resources
from ...runtime import blocking
from .alignment import align
from .apply import MutationApplier
from .changes import build_write_set, select_writes
from .chunk_vectors import ChunkVectorPreparation
from .extraction import extract_text, mapped_extraction
from .extraction_batches import reusable_batches
from .maintenance import check_vector_capacity, conflict_warnings, deletion_cleanup, task_counts
from .merge import aggregate, relation_conflict_groups


log = logging.getLogger(__name__)


class Construction:
    def __init__(self, resources: Resources):
        self.c = resources
        self.applier = MutationApplier(resources)

    async def prepare(self, job):
        """生成并保存可重放的最终状态计划；正式知识写入由 apply 执行。"""
        if job["state"] != "PREPARING":
            raise KGError("INVALID_JOB_STATE", "Only a preparing task can generate a mutation plan", 409)
        c, operation, kb_id = self.c, job["operation"], job["kb_id"]
        kb = await c.metadata.kb(kb_id)
        if kb["maintenance_state"] != "READY":
            raise KGError(
                "KB_MAINTENANCE", "The knowledge base must finish its current repair before preparing", 503
            )

        # 固定本轮准备使用的模型、向量配置和有效资料，恢复时逐项检查基线。
        model_data = job["input"]["model"] if operation == "REBUILD" else kb["model"]
        model = BusinessModel.model_validate(model_data)
        model_data = model.model_dump()
        profile = job["input"]["embedding_profile"] if operation == "REINDEX" else kb["embedding_profile"]
        profile = EmbeddingProfile.model_validate(profile).model_dump()
        parser_settings = {
            key: getattr(c.settings, key) for key in ("chunk_target", "chunk_max", "chunk_overlap")
        }
        scoped = operation in {"IMPORT", "DELETE"}
        if scoped:
            document = await c.metadata.document(kb_id, job["document_id"])
            active = [document] if document["active"] else []
        else:
            active = await c.metadata.active_documents(kb_id)
        document_ids = sorted(item["document_id"] for item in active)

        draft_path = c.files.attempt_dir(job) / "preparation.json"
        try:
            draft = await blocking(c.files.read_preparation, job)
        except KGError as exc:
            if exc.code != "PLAN_INVALID":
                raise
            # PREPARING cannot have published knowledge writes. A broken intermediate may be regenerated.
            draft = None
        baseline = {
            "job_id": job["job_id"],
            "kb_id": kb_id,
            "operation": operation,
            "model": model_data,
            "embedding_profile": profile,
            "model_revision_baseline": kb["model_revision"],
            "baseline_embedding_profile": kb["embedding_profile"],
            "baseline_document_ids": document_ids,
            "parser_settings": parser_settings,
        }
        # 手动重试已退出队列的准备任务时，新的 queue_seq 对应新的基线。
        if (
            draft is None
            or draft.get("queue_seq") != job["queue_seq"]
            or draft.get("scoped", False) != scoped
        ):
            draft = {
                **baseline,
                "queue_seq": job["queue_seq"],
                "scoped": scoped,
                "plan_id": new_id(),
                "model_revision": new_id() if operation == "REBUILD" else kb["model_revision"],
                "documents": {},
                "entity_ids": {},
                "relation_ids": {},
                "contribution_ids": {},
                "vectors": {},
            }
        elif any(draft.get(key) != value for key, value in baseline.items()):
            raise KGError(
                "PREPARATION_BASELINE_CHANGED", "Preparation no longer matches its persisted baseline", 503
            )

        if c.settings.worker_distribution is not None:
            await blocking(c.files.begin_preparation, job)
            if not await c.metadata.owns_preparation(job):
                raise KGError("PREPARATION_REPLACED", "Another attempt owns this preparation", 409)

        async def persist():
            await blocking(c.files.atomic, draft_path, draft)

        await persist()

        if scoped:
            return await self._prepare_scoped(job, document, model, draft, persist)

        # 整库重建与向量重建仍按完整快照执行。
        old_entities, old_relations = await c.graph.all(kb_id)
        old_chunks = await c.vectors.all(kb_id, "chunks", max_points=c.settings.maintenance_scan_limit)
        old_objects = await c.vectors.all(kb_id, "objects", max_points=c.settings.maintenance_scan_limit)
        contributions = await c.metadata.contributions(kb_id, document_ids)
        previous_contributions = list(contributions)
        issues, warnings = [], []
        processed_documents = []

        if operation == "REBUILD":
            contributions, chunk_points = [], []
            # 重建只沿用身份规则兼容的对象 ID，属性与来源贡献重新计算。
            old_model = BusinessModel.model_validate(kb["model"])
            compatible_types = {
                name
                for name, node in model.node_types.items()
                if name in old_model.node_types
                and node.identity.model_dump() == old_model.node_types[name].identity.model_dump()
            }
            aligned_entities = [item for item in old_entities if item["entity_type"] in compatible_types]
            aligned_ids = {item["entity_id"] for item in aligned_entities}
            aligned_relations = [
                item
                for item in old_relations
                if item["source_id"] in aligned_ids and item["target_id"] in aligned_ids
            ]
            reuse = None
            for document in active:
                processed_documents.append(document["document_id"])
                entry, reuse = await self._prepare_document(
                    job, document, model, draft, persist, reuse, aligned_entities, aligned_relations
                )
                contributions.extend(entry["contributions"])
                issues.extend(entry["issues"])
                chunk_points.extend(self._chunk_points(entry, kb_id, profile))
                warnings.extend(self._source_warnings(document, model, entry["contributions"]))
            if active and not contributions:
                warnings.append(self._no_graph_facts())
        elif operation == "REINDEX":
            chunk_points = [
                {**point, "payload": point["payload"] | {"embedding_profile": profile}, "vector": None}
                for point in old_chunks
            ]
        else:
            raise KGError("INVALID_JOB_STATE", "Unknown maintenance operation", 409)

        # 先从保留的来源贡献合并图对象，再为最终切片和对象准备向量。
        contributions = list({claim["contribution_id"]: claim for claim in contributions}.values())
        if "final_entities" not in draft:
            if operation == "REINDEX":
                draft["final_entities"], draft["final_relations"] = old_entities, old_relations
            else:
                draft["final_entities"], draft["final_relations"] = aggregate(
                    kb_id,
                    contributions,
                    model,
                    old_entities,
                    old_relations,
                )
            await persist()
        entities, relations = draft["final_entities"], draft["final_relations"]
        check_vector_capacity(len(chunk_points), len(entities), c.settings.maintenance_scan_limit)
        object_points = self._object_points(entities, kb_id, profile)
        await self._prepare_vectors(
            object_points, old_objects, draft, profile, persist
        )

        if issues:
            warnings.append({"code": "CANDIDATE_ISSUES", "count": len(issues)})
        old_revisions = {
            item.get("entity_id", item.get("relation_id")): item["object_revision"]
            for item in old_entities + old_relations
        }
        changed_ids = {
            item.get("entity_id", item.get("relation_id"))
            for item in entities + relations
            if old_revisions.get(item.get("entity_id", item.get("relation_id"))) != item["object_revision"]
        }
        scoped_ids = {
            claim["target_id"] for claim in contributions if claim["document_id"] in processed_documents
        }
        warnings.extend(
            conflict_warnings(entities, relations, changed_ids | scoped_ids) if operation != "REINDEX" else []
        )
        final_chunk_ids = {point["id"] for point in chunk_points}
        final_entity_ids = {entity["entity_id"] for entity in entities}
        final_relation_ids = {relation["relation_id"] for relation in relations}
        counts = task_counts(
            operation,
            contributions,
            entities,
            relations,
            chunk_points,
            issues,
            processed_documents,
            old_entities,
            old_relations,
        )
        counts["removed_contributions"] = len(
            {claim["contribution_id"] for claim in previous_contributions}
            - {claim["contribution_id"] for claim in contributions}
        )
        if operation == "REINDEX":
            counts.update(reindexed_chunks=len(chunk_points), reindexed_objects=len(object_points))

        # 完整计划先原子落盘，再发布 PREPARED，供写入与恢复共用。
        plan = {
            "schema_version": 4,
            "verification": None,
            "write_mode": "full",
            "plan_id": draft["plan_id"],
            "job_id": job["job_id"],
            "kb_id": kb_id,
            "operation": operation,
            "document_ids": document_ids,
            "model": model_data,
            "model_revision": draft["model_revision"],
            "embedding_profile": profile,
            "parser_settings": parser_settings,
            "entities": entities,
            "relations": relations,
            "contributions": contributions,
            "chunk_points": chunk_points,
            "object_points": object_points,
            "issues": issues,
            "issue_documents": sorted(processed_documents),
            "delete_entities": sorted({item["entity_id"] for item in old_entities} - final_entity_ids),
            "delete_relations": sorted({item["relation_id"] for item in old_relations} - final_relation_ids),
            "delete_chunks": sorted({point["id"] for point in old_chunks} - final_chunk_ids),
            "delete_objects": sorted({point["id"] for point in old_objects} - final_entity_ids),
            "activate_documents": [],
            "delete_documents": [],
            "cleanup_paths": [],
            "cleanup_job_ids": [],
            "warnings": warnings,
            "counts": counts,
        }
        await self._prepare_chunk_index(job, plan, draft, persist)
        return await self._publish(
            job,
            draft,
            plan,
            {
                "entities": old_entities,
                "relations": old_relations,
                "chunk_points": old_chunks,
                "object_points": old_objects,
            },
        )

    async def _prepare_scoped(self, job, document, model, draft, persist):
        c, operation, kb_id = self.c, job["operation"], job["kb_id"]
        document_id = document["document_id"]
        profile = draft["embedding_profile"]
        issues, warnings, cleanup, cleanup_jobs = [], [], [], []
        old_chunks, chunk_points = [], []
        if operation == "IMPORT":
            if document["deleted_at"] or document["active"]:
                raise KGError("INVALID_JOB_STATE", "Only a pending source can be imported", 409)
            entry, _ = await self._prepare_document(job, document, model, draft, persist, None)
            changes = entry["contributions"]
            issues = entry["issues"]
            chunk_points = self._chunk_points(entry, kb_id, profile)
            warnings.extend(self._source_warnings(document, model, changes))
            if not changes:
                warnings.append(self._no_graph_facts())
        else:
            changes, old_chunks, (cleanup, cleanup_jobs) = await asyncio.gather(
                c.metadata.contributions(kb_id, [document_id]),
                c.vectors.all(
                    kb_id,
                    "chunks",
                    with_vectors=False,
                    document_ids=[document_id],
                    max_points=c.settings.maintenance_scan_limit,
                ),
                deletion_cleanup(c, job, document),
            )

        targets = {(row["target_kind"], row["target_id"]) for row in changes}
        groups = relation_conflict_groups(changes, model)
        entity_ids = {key for kind, key in targets if kind == "entity"}
        old_entities, direct_relations, group_relations = await asyncio.gather(
            c.graph.by_id(kb_id, "entity", sorted(entity_ids)),
            c.graph.by_id(kb_id, "relation", sorted(key for kind, key in targets if kind == "relation")),
            c.graph.relation_groups(kb_id, groups),
        )
        old_relations = list({row["relation_id"]: row for row in direct_relations + group_relations}.values())
        targets.update(("relation", row["relation_id"]) for row in old_relations)
        remaining = await c.metadata.target_contributions(kb_id, sorted(targets), active_only=True)
        contributions = [row for row in remaining if row["document_id"] != document_id]
        if operation == "IMPORT":
            contributions.extend(changes)

        external_endpoints = sorted(
            {
                row["value"][field]
                for row in contributions
                if row["field_path"] == "$relation"
                for field in ("source_id", "target_id")
            }
            - entity_ids
        )
        endpoints = await c.graph.by_id(kb_id, "entity", external_endpoints)
        if "final_entities" not in draft:
            draft["final_entities"], draft["final_relations"] = aggregate(
                kb_id,
                contributions,
                model,
                old_entities,
                old_relations,
                endpoint_ids={row["entity_id"] for row in endpoints},
            )
            await persist()
        entities, relations = draft["final_entities"], draft["final_relations"]
        final_entity_ids = {row["entity_id"] for row in entities}
        final_relation_ids = {row["relation_id"] for row in relations}
        delete_entities = sorted({row["entity_id"] for row in old_entities} - final_entity_ids)
        delete_relations = sorted({row["relation_id"] for row in old_relations} - final_relation_ids)
        delete_chunks = sorted(point["id"] for point in old_chunks)
        old_objects, graph_counts, chunk_count, claim_count = await asyncio.gather(
            c.vectors.read(kb_id, "objects", sorted(entity_ids), with_vectors=True),
            c.graph.counts(kb_id),
            c.vectors.count(kb_id, "chunks"),
            c.metadata.contribution_count(kb_id),
        )

        object_points = self._object_points(entities, kb_id, profile)
        counts = task_counts(
            operation,
            contributions,
            entities,
            relations,
            chunk_points,
            issues,
            [document_id] if operation == "IMPORT" else [],
            old_entities,
            old_relations,
        )
        counts.update(
            current_entities=graph_counts["entities"] + len(entities) - len(old_entities),
            current_relations=graph_counts["relations"] + len(relations) - len(old_relations),
            current_chunks=chunk_count + len(chunk_points) - len(old_chunks),
            current_contributions=claim_count + (len(changes) if operation == "IMPORT" else -len(changes)),
            removed_contributions=len(changes) if operation == "DELETE" else 0,
        )
        check_vector_capacity(
            counts["current_chunks"], counts["current_entities"], c.settings.maintenance_scan_limit
        )
        await self._prepare_vectors(
            object_points, old_objects, draft, profile, persist, lookup_kb=kb_id
        )
        if issues:
            warnings.append({"code": "CANDIDATE_ISSUES", "count": len(issues)})
        old_revisions = {
            row.get("entity_id", row.get("relation_id")): row["object_revision"]
            for row in old_entities + old_relations
        }
        changed_ids = {row["target_id"] for row in changes} if operation == "IMPORT" else set()
        for row in entities + relations:
            key = row.get("entity_id", row.get("relation_id"))
            if old_revisions.get(key) != row["object_revision"]:
                changed_ids.add(key)
        warnings.extend(conflict_warnings(entities, relations, changed_ids))

        chunk_ids = {point["id"] for point in chunk_points}
        evidence_chunk_ids = {row["chunk_id"] for row in contributions}
        evidence_chunk_ids.update(ev["chunk_id"] for row in contributions for ev in row["evidence"])
        activate = [document_id] if operation == "IMPORT" else []
        plan = {
            "schema_version": 4,
            "write_mode": "delta",
            "plan_id": draft["plan_id"],
            "job_id": job["job_id"],
            "kb_id": kb_id,
            "operation": operation,
            "document_ids": sorted(set(activate) | {row["document_id"] for row in contributions}),
            "model": draft["model"],
            "model_revision": draft["model_revision"],
            "embedding_profile": profile,
            "parser_settings": draft["parser_settings"],
            "entities": entities,
            "relations": relations,
            "contributions": contributions,
            "chunk_points": chunk_points,
            "object_points": object_points,
            "issues": issues,
            "issue_documents": [document_id],
            "delete_entities": delete_entities,
            "delete_relations": delete_relations,
            "delete_chunks": delete_chunks,
            "delete_objects": delete_entities,
            "activate_documents": activate,
            "delete_documents": [] if activate else [document_id],
            "cleanup_paths": cleanup,
            "cleanup_job_ids": cleanup_jobs,
            "warnings": warnings,
            "counts": counts,
            "verification": {
                "endpoint_ids": external_endpoints,
                "evidence_chunk_ids": sorted(evidence_chunk_ids - chunk_ids),
                "relation_groups": groups,
            },
        }
        await self._prepare_chunk_index(job, plan, draft, persist)
        return await self._publish(
            job,
            draft,
            plan,
            {
                "entities": old_entities,
                "relations": old_relations,
                "chunk_points": old_chunks,
                "object_points": old_objects,
            },
        )

    async def _prepare_document(
        self,
        job,
        document,
        model,
        draft,
        persist,
        reuse,
        existing_entities=(),
        existing_relations=(),
    ):
        """每份资料按解析、抽取、对齐保存检查点。"""
        c, kb_id, document_id = self.c, job["kb_id"], document["document_id"]
        entry = draft["documents"].setdefault(document_id, {})
        if "parsed" not in entry:
            try:
                entry["parsed"] = await blocking(c.files.parse_document, c.parser, document, model)
            except KGError:
                raise
            except (UnicodeError, ValueError, OSError) as exc:
                raise KGError(
                    "PARSE_FAILED", "Document could not be parsed", 422, document_id=document_id
                ) from exc
            await persist()
        parsed = entry["parsed"]
        chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
        if len(chunks) != len(parsed["chunks"]) or any(
            chunk["document_id"] != document_id for chunk in chunks.values()
        ):
            raise KGError("PARSE_FAILED", "Parser returned duplicate or cross-document chunk IDs")
        if "extracted" not in entry:
            if parsed.get("mapping_id"):
                entry["extracted"], entry["mapping_issues"] = mapped_extraction(parsed, model)
            else:
                if reuse is None:
                    reuse = await self._load_extraction_reuse(kb_id)
                    reuse.update(reusable_batches(draft["documents"]))
                entry["extracted"] = await extract_text(
                    c.language,
                    parsed,
                    model,
                    entry.setdefault("extraction_batches", {}),
                    persist,
                    reuse=reuse,
                )
                entry["mapping_issues"] = []
            await persist()
        if "contributions" not in entry:
            adopted, issues = await align(
                document,
                entry["extracted"],
                chunks,
                model,
                draft,
                job["job_id"],
                existing_entities,
                existing_relations,
                parsed.get("rule_units", []),
                graph=c.graph if job["operation"] == "IMPORT" else None,
            )
            issues.extend(
                {
                    "issue_id": new_id(),
                    "kb_id": kb_id,
                    "job_id": job["job_id"],
                    "document_id": document_id,
                    "chunk_id": None,
                    **problem,
                }
                for problem in entry["mapping_issues"]
            )
            entry["contributions"], entry["issues"] = adopted, issues
            await persist()
        return entry, reuse

    @staticmethod
    def _chunk_points(entry, kb_id, profile):
        return [
            {
                "id": chunk["chunk_id"],
                "payload": {"kb_id": kb_id, **chunk, "embedding_profile": profile},
                "vector": None,
            }
            for chunk in entry["parsed"]["chunks"]
        ]

    @staticmethod
    def _object_points(entities, kb_id, profile):
        return [
            {
                "id": entity["entity_id"],
                "payload": {
                    "kb_id": kb_id,
                    "object_kind": "entity",
                    "object_id": entity["entity_id"],
                    "entity_type": entity["entity_type"],
                    "name": entity["canonical_name"],
                    "aliases": entity["aliases"],
                    "domain_ids": entity["domain_ids"],
                    "search_text": entity["search_text"],
                    "object_revision": entity["object_revision"],
                    "embedding_profile": profile,
                },
                "vector": None,
            }
            for entity in entities
        ]

    @staticmethod
    def _source_warnings(document, model, contributions):
        if (
            model.domains
            and not document["source"].get("domain_ids")
            and not any(
                claim["field_path"] == "$exists" and claim["value"].get("domain_ids")
                for claim in contributions
            )
        ):
            return [
                {
                    "code": "DOMAIN_UNCERTAIN",
                    "document_id": document["document_id"],
                    "message": "No configured domain could be established; the full model scope was retained",
                }
            ]
        return []

    @staticmethod
    def _no_graph_facts():
        return {
            "code": "NO_GRAPH_FACTS",
            "message": "Source text remains searchable without adopted graph facts",
        }

    async def _prepare_chunk_index(self, job, plan, draft, persist):
        preparation = ChunkVectorPreparation(self.c, job, plan["embedding_profile"])
        index = await preparation.prepare(plan["chunk_points"], draft, persist, self._prepare_vectors)
        plan["chunk_index"] = index
        plan["document_ids"] = sorted(set(plan["document_ids"]) | {row["document_id"] for row in index["members"]})
        plan["counts"]["current_chunk_vectors"] = index["current_count"]
        if draft.get("chunk_decision_failed"):
            plan["warnings"].append({"code": "CHUNK_COVERAGE_UNAVAILABLE"})

    async def _publish(self, job, draft, plan, previous):
        c, operation = self.c, job["operation"]
        counts = plan["counts"]
        plan["write_set"] = build_write_set(plan, previous)
        writes = select_writes(plan)
        counts.update(
            {
                "upserted_chunks": len(writes["chunk_points"]),
                "upserted_objects": len(writes["object_points"]),
                "upserted_entities": len(writes["entities"]) if operation != "REINDEX" else 0,
                "upserted_relations": len(writes["relations"]) if operation != "REINDEX" else 0,
                "written_contributions": len(writes["contributions"]) if operation != "REINDEX" else 0,
            }
        )
        batches = [
            batch
            for entry in draft["documents"].values()
            for key, batch in entry.get("extraction_batches", {}).items()
            if key.startswith("batch/")
        ]
        if batches:
            counts.update(
                extraction_batches=len(batches),
                reused_extraction_batches=sum(batch.get("reused", False) for batch in batches),
            )
        if operation in {"IMPORT", "REBUILD"}:
            reusable = dict(reusable_batches(draft["documents"]))
            path = c.files.attempt_dir(job) / "extraction-results.json"
            await blocking(c.files.atomic, path, reusable)
        path = c.files.attempt_dir(job) / "prepared.json"
        plan_path = await blocking(c.files.save_plan, job["job_id"], plan, path=path)
        if c.settings.worker_distribution is None:
            await c.metadata.update_job(
                job["job_id"], state="PREPARED", plan_path=plan_path, plan_id=plan["plan_id"], error_json=None
            )
        elif not await c.metadata.update_preparation(
            job, state="PREPARED", plan_path=plan_path, plan_id=plan["plan_id"],
            last_step="prepared", error_json=None, next_attempt_at=0,
        ):
            raise KGError("PREPARATION_REPLACED", "Another attempt owns this preparation", 409)
        return plan

    async def _load_extraction_reuse(self, kb_id):
        """读取最近一次成功抽取任务的候选，沿用任务检查点的清理周期。"""
        c = self.c
        job_id = await c.metadata.latest_extraction_job_id(kb_id)
        if job_id is None:
            return {}
        job = await c.metadata.job(job_id)
        if not job["plan_path"]:
            return {}
        try:
            return await blocking(c.files.read_json, c.files.extraction_path(job)) or {}
        except KGError as exc:
            if exc.code != "PLAN_INVALID":
                raise
            log.warning("Ignoring invalid extraction results checkpoint for job %s", job_id)
            return {}

    async def _prepare_vectors(self, points, existing, draft, profile, persist, *, lookup_kb=None):
        """复用同配置、同输入文本的向量，再分批补齐剩余输入。"""
        previous = {point["id"]: point for point in existing}
        missing = []
        for point in points:
            text_key = "text" if "text" in point["payload"] else "search_text"
            old = previous.get(point["id"])
            if (
                point["vector"] is None
                and old
                and old["payload"].get("embedding_profile") == profile
                and old["payload"].get(text_key) == point["payload"][text_key]
            ):
                point["vector"] = old["vector"]
            # 草稿已经绑定本轮基线，其缓存可在准备恢复时直接复用。
            saved = draft["vectors"].get(point["id"])
            if point["vector"] is None and saved is not None:
                point["vector"] = saved
            if point["vector"] is None:
                missing.append((point, point["payload"][text_key]))
        if not missing:
            return

        # 新切片可能有新 ID。只复用向量数值，切片的来源和位置仍以本轮解析为准。
        wanted_texts = {text for _, text in missing}
        vectors_by_text = {}
        for old in existing:
            payload = old["payload"]
            if old["vector"] is None or payload.get("embedding_profile") != profile:
                continue
            text = payload.get("text", payload.get("search_text"))
            if isinstance(text, str) and text in wanted_texts:
                vectors_by_text.setdefault(text, old["vector"])

        if lookup_kb is not None:
            texts_to_find = sorted(wanted_texts - vectors_by_text.keys())
            if texts_to_find:
                vectors_by_text.update(
                    await self.c.vectors.reusable_vectors(lookup_kb, texts_to_find, profile)
                )

        pending = {}
        for point, text in missing:
            point["vector"] = vectors_by_text.get(text)
            if point["vector"] is None:
                pending.setdefault(text, []).append(point)

        texts = list(pending)
        for start in range(0, len(texts), profile["batch_size"]):
            batch = texts[start : start + profile["batch_size"]]
            vectors = await self.c.language.embed(batch, profile)
            if len(vectors) != len(batch):
                raise KGError(
                    "INVALID_EMBEDDING_RESPONSE", "Embedding item count does not match its input", 503
                )
            for text, vector in zip(batch, vectors, strict=True):
                if (
                    not isinstance(vector, list)
                    or len(vector) != profile["dimension"]
                    or any(
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or not math.isfinite(value)
                        for value in vector
                    )
                    or (profile["distance"] == "Cosine" and not any(vector))
                ):
                    raise KGError(
                        "INVALID_EMBEDDING_RESPONSE", "Embedding vector is incompatible with its profile", 503
                    )
                for point in pending[text]:
                    point["vector"] = vector
                    draft["vectors"][point["id"]] = vector
            await persist()

    async def apply(self, job):
        return await self.applier.apply(job)

    async def verify(self, plan):
        return await self.applier.verify(plan)
