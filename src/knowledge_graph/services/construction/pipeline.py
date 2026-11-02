from __future__ import annotations

import math

from ...domain import BusinessModel, EmbeddingProfile, KGError, new_id
from ...ports.resources import Resources
from ...runtime import blocking
from .alignment import align
from .apply import MutationApplier
from .extraction import extract_text, mapped_extraction
from .maintenance import check_vector_capacity, conflict_warnings, deletion_cleanup, task_counts
from .merge import aggregate


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
        active = await c.metadata.active_documents(kb_id)
        document_ids = sorted(document["document_id"] for document in active)

        draft_path = c.files.job_dir(job["job_id"]) / "preparation.json"
        try:
            draft = await blocking(c.files.read_json, draft_path)
        except KGError as exc:
            if exc.code != "PLAN_INVALID":
                raise
            # PREPARING cannot have published knowledge writes. A broken intermediate may be regenerated.
            draft = None
        # A failed task leaves the queue. Its explicit retry receives a new queue_seq and a new baseline.
        if draft is None or draft.get("queue_seq") != job["queue_seq"]:
            draft = {
                "job_id": job["job_id"],
                "kb_id": kb_id,
                "queue_seq": job["queue_seq"],
                "operation": operation,
                "plan_id": new_id(),
                "model": model_data,
                "model_revision": new_id() if operation == "REBUILD" else kb["model_revision"],
                "model_revision_baseline": kb["model_revision"],
                "embedding_profile": profile,
                "baseline_embedding_profile": kb["embedding_profile"],
                "baseline_document_ids": document_ids,
                "parser_settings": parser_settings,
                "documents": {},
                "entity_ids": {},
                "relation_ids": {},
                "contribution_ids": {},
                "vectors": {},
            }
        elif (
            draft.get("job_id") != job["job_id"]
            or draft.get("kb_id") != kb_id
            or draft.get("operation") != operation
            or draft.get("model") != model_data
            or draft.get("embedding_profile") != profile
            or draft.get("model_revision_baseline") != kb["model_revision"]
            or draft.get("baseline_embedding_profile") != kb["embedding_profile"]
            or draft.get("baseline_document_ids") != document_ids
            or draft.get("parser_settings") != parser_settings
        ):
            raise KGError(
                "PREPARATION_BASELINE_CHANGED", "Preparation no longer matches its persisted baseline", 503
            )

        async def persist():
            await blocking(c.files.atomic, draft_path, draft)

        await persist()

        # 读取当前知识快照，后续各操作只在内存和准备检查点中推导目标状态。
        old_entities, old_relations = await c.graph.all(kb_id)
        old_chunks = await c.vectors.all(kb_id, "chunks", max_points=c.settings.maintenance_scan_limit)
        old_objects = await c.vectors.all(kb_id, "objects", max_points=c.settings.maintenance_scan_limit)
        contributions = await c.metadata.contributions(kb_id, document_ids)
        previous_contributions = list(contributions)
        issues, warnings, activate, delete_documents, cleanup, cleanup_job_ids = [], [], [], [], [], []
        processed_documents = []

        if operation in {"IMPORT", "REBUILD"}:
            if operation == "IMPORT":
                document = await c.metadata.document(kb_id, job["document_id"])
                if document["deleted_at"]:
                    raise KGError("INVALID_JOB_STATE", "A deleted document cannot be imported", 409)
                if document["active"]:
                    raise KGError(
                        "INVALID_JOB_STATE", "An already active source cannot be imported again", 409
                    )
                docs, activate, chunk_points = [document], [document["document_id"]], list(old_chunks)
                aligned_entities, aligned_relations = old_entities, old_relations
            else:
                docs, contributions, chunk_points = active, [], []
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

            # 每份资料按解析、抽取、对齐依次保存结果，恢复时复用已完成阶段。
            for document in docs:
                document_id = document["document_id"]
                processed_documents.append(document_id)
                entry = draft["documents"].setdefault(document_id, {})
                if "parsed" not in entry:
                    try:
                        entry["parsed"] = await blocking(c.parser.parse, document, model)
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
                        entry["extracted"] = await extract_text(
                            c.language, parsed, model, entry.setdefault("extraction_batches", {}), persist
                        )
                        entry["mapping_issues"] = []
                    await persist()

                if "contributions" not in entry:
                    adopted, candidate_issues = align(
                        document,
                        entry["extracted"],
                        chunks,
                        model,
                        draft,
                        job["job_id"],
                        aligned_entities,
                        aligned_relations,
                        parsed.get("rule_units", []),
                    )
                    for problem in entry["mapping_issues"]:
                        candidate_issues.append(
                            {
                                "issue_id": new_id(),
                                "kb_id": kb_id,
                                "job_id": job["job_id"],
                                "document_id": document_id,
                                "chunk_id": None,
                                **problem,
                            }
                        )
                    entry["contributions"], entry["issues"] = adopted, candidate_issues
                    await persist()
                contributions.extend(entry["contributions"])
                issues.extend(entry["issues"])
                for chunk in parsed["chunks"]:
                    chunk_points.append(
                        {
                            "id": chunk["chunk_id"],
                            "payload": {"kb_id": kb_id, **chunk, "embedding_profile": profile},
                            "vector": None,
                        }
                    )
                if (
                    model.domains
                    and not document["source"].get("domain_ids")
                    and not any(
                        claim["field_path"] == "$exists" and claim["value"].get("domain_ids")
                        for claim in entry["contributions"]
                    )
                ):
                    warnings.append(
                        {
                            "code": "DOMAIN_UNCERTAIN",
                            "document_id": document_id,
                            "message": "No configured domain could be established; the full model scope was retained",
                        }
                    )
            new_claims = [
                claim
                for document in draft["documents"].values()
                for claim in document.get("contributions", [])
            ]
            if docs and not any(claim["field_path"] in {"$exists", "$relation"} for claim in new_claims):
                warnings.append(
                    {
                        "code": "NO_GRAPH_FACTS",
                        "message": "Source text remains searchable without adopted graph facts",
                    }
                )
        elif operation == "DELETE":
            document = await c.metadata.document(kb_id, job["document_id"])
            delete_documents = [document["document_id"]]
            contributions = [claim for claim in contributions if claim["document_id"] not in delete_documents]
            chunk_points = [
                point for point in old_chunks if point["payload"]["document_id"] not in delete_documents
            ]
            cleanup, cleanup_job_ids = await deletion_cleanup(c, job, document)
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
                    kb_id, contributions, model, old_entities, old_relations
                )
            await persist()
        entities, relations = draft["final_entities"], draft["final_relations"]
        check_vector_capacity(len(chunk_points), len(entities), c.settings.maintenance_scan_limit)
        object_points = []
        for entity in entities:
            payload = {
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
            }
            object_points.append({"id": entity["entity_id"], "payload": payload, "vector": None})
        await self._prepare_vectors(
            chunk_points + object_points, old_chunks + old_objects, draft, profile, persist
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
            "schema_version": 1,
            "plan_id": draft["plan_id"],
            "job_id": job["job_id"],
            "kb_id": kb_id,
            "operation": operation,
            "document_ids": sorted(set(document_ids + activate) - set(delete_documents)),
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
            "issue_documents": sorted(set(processed_documents) | set(delete_documents)),
            "delete_entities": sorted({item["entity_id"] for item in old_entities} - final_entity_ids),
            "delete_relations": sorted({item["relation_id"] for item in old_relations} - final_relation_ids),
            "delete_chunks": sorted({point["id"] for point in old_chunks} - final_chunk_ids),
            "delete_objects": sorted({point["id"] for point in old_objects} - final_entity_ids),
            "activate_documents": activate,
            "delete_documents": delete_documents,
            "cleanup_paths": cleanup,
            "cleanup_job_ids": cleanup_job_ids,
            "warnings": warnings,
            "counts": counts,
        }
        plan_path = await blocking(c.files.save_plan, job["job_id"], plan)
        await c.metadata.update_job(
            job["job_id"], state="PREPARED", plan_path=plan_path, plan_id=plan["plan_id"], error_json=None
        )
        return plan

    async def _prepare_vectors(self, points, existing, draft, profile, persist):
        """依次复用同配置同文本的旧向量、草稿缓存，再分批补齐并保存。"""
        previous = {point["id"]: point for point in existing}
        missing, texts = [], []
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
                missing.append(point)
                texts.append(point["payload"][text_key])

        for start in range(0, len(missing), profile["batch_size"]):
            batch = missing[start : start + profile["batch_size"]]
            vectors = await self.c.language.embed(texts[start : start + profile["batch_size"]], profile)
            if len(vectors) != len(batch):
                raise KGError(
                    "INVALID_EMBEDDING_RESPONSE", "Embedding item count does not match its input", 503
                )
            for point, vector in zip(batch, vectors, strict=True):
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
                point["vector"] = vector
                draft["vectors"][point["id"]] = vector
            await persist()

    async def apply(self, job):
        return await self.applier.apply(job)

    async def verify(self, plan):
        return await self.applier.verify(plan)
