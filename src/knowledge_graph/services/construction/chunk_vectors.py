"""准备切片归属和代表向量。来源正文与证据继续使用独立的切片 ID。"""

from __future__ import annotations

import logging
import math
from collections import defaultdict

from ...domain import KGError, new_id

log = logging.getLogger(__name__)


def _representative(kb_id, point, profile, vector_id=None):
    payload = point["payload"]
    return {
        "id": vector_id or new_id(),
        "payload": {
            "kb_id": kb_id,
            "representative_chunk_id": point["id"],
            "text": payload["text"],
            "embedding_profile": profile,
        },
        "vector": point["vector"],
    }


def _membership(point, vector_id):
    return {
        "kb_id": point["payload"]["kb_id"],
        "document_id": point["payload"]["document_id"],
        "chunk_id": point["id"],
        "vector_id": vector_id,
    }


def _cosine(left, right):
    norm = math.hypot(*left) * math.hypot(*right)
    return sum(a * b for a, b in zip(left, right, strict=True)) / norm if norm else 0


class ChunkVectorPreparation:
    def __init__(self, resources, job, profile):
        self.c = resources
        self.kb_id = job["kb_id"]
        self.job = job
        self.profile = profile
        self.decision_failed = False
        self.documents = {}

    async def prepare(self, chunks, draft, persist, embed):
        if "chunk_index" not in draft:
            operation = self.job["operation"]
            if operation == "REINDEX":
                points, members = await self._reindex(chunks, draft, persist, embed)
                writes, deleted, count = set(points), [], len(points)
            elif operation == "DELETE":
                points, members, writes, deleted = await self._delete(draft, persist, embed)
                count = await self.c.vectors.count(self.kb_id, "chunk_vectors") + len(writes) - len(deleted)
            else:
                importing = operation == "IMPORT"
                points, members, writes = await self._group(
                    chunks, draft, persist, embed, importing=importing
                )
                count = len(points)
                if importing:
                    previous = await self.c.vectors.count(self.kb_id, "chunk_vectors")
                    existing_ids = await self.c.vectors.read(self.kb_id, "chunk_vectors", sorted(writes))
                    count = previous + len(writes) - len(existing_ids)
                deleted = []
            draft["chunk_index"] = {
                "points": sorted(points.values(), key=lambda point: point["id"]),
                "members": sorted(members, key=lambda row: row["chunk_id"]),
                "write_ids": sorted(writes),
                "delete_ids": sorted(deleted),
                "current_count": count,
            }
            draft["chunk_decision_failed"] = self.decision_failed
            await persist()
        return draft["chunk_index"]

    async def _embed(self, chunks, draft, persist, embed):
        pending = [{"id": chunk["id"], "payload": chunk["payload"], "vector": None} for chunk in chunks]
        await embed(pending, [], draft, self.profile, persist, lookup_kb=self.kb_id)
        return pending

    async def _group(self, chunks, draft, persist, embed, *, importing=False, compare_local=False):
        c, kb_id = self.c, self.kb_id
        first_by_text = {}
        for chunk in chunks:
            first_by_text.setdefault(chunk["payload"]["text"], chunk)
        exact = await self._exact_matches(first_by_text) if importing else {}
        pending = await self._embed(
            [chunk for text, chunk in first_by_text.items() if text not in exact], draft, persist, embed
        )
        can_compare = self.profile["distance"] == "Cosine" and bool(c.settings.chunk_decision_model)
        neighbors = (
            await c.vectors.near_chunk_vectors(kb_id, pending, c.settings.chunk_similarity_threshold)
            if importing and can_compare
            else {}
        )
        assignments = {text: point["id"] for text, point in exact.items()}
        points = {point["id"]: point for point in exact.values()}
        previous_ids, writes = set(points), set()
        new_sources = {chunk["id"]: chunk["payload"] for chunk in chunks}
        neighbor_sources = {
            hit["payload"]["representative_chunk_id"] for hits in neighbors.values() for hit in hits
        } - new_sources.keys()
        sources = new_sources | {
            point["id"]: point["payload"]
            for point in await c.vectors.read(kb_id, "chunks", sorted(neighbor_sources))
        }

        for chunk in pending:
            text = chunk["payload"]["text"]
            candidates = neighbors.get(chunk["id"], [])
            if compare_local and can_compare:
                candidates = sorted(
                    points.values(), key=lambda point: _cosine(point["vector"], chunk["vector"]), reverse=True
                )[:3]
            match = None
            for hit in candidates:
                old = points.get(hit["id"], hit)
                if old["payload"]["embedding_profile"] != self.profile:
                    continue
                if _cosine(old["vector"], chunk["vector"]) < c.settings.chunk_similarity_threshold:
                    continue
                representative_source = sources.get(old["payload"]["representative_chunk_id"])
                if representative_source is None:
                    raise KGError("SOURCE_UNAVAILABLE", "A representative source chunk is missing", 503)
                covers, covered_by_new = await self._judge(representative_source, chunk["payload"])
                if covers or covered_by_new:
                    match = old["id"]
                    if covered_by_new and not covers:
                        points[match] = _representative(kb_id, chunk, self.profile, match)
                        writes.add(match)
                    else:
                        points.setdefault(match, old)
                    if importing:
                        previous_ids.add(match)
                    break
            if match is None:
                point = _representative(kb_id, chunk, self.profile)
                match = point["id"]
                points[match] = point
                writes.add(match)
            assignments[text] = match

        members = (
            await c.metadata.vector_members(kb_id, vector_ids=sorted(previous_ids)) if previous_ids else []
        )
        members.extend(_membership(chunk, assignments[chunk["payload"]["text"]]) for chunk in chunks)
        return points, members, writes

    async def _exact_matches(self, first_by_text):
        c, kb_id = self.c, self.kb_id
        exact = await c.vectors.exact_chunk_vectors(kb_id, list(first_by_text), self.profile)
        missing = [text for text in first_by_text if text not in exact]
        sources = await c.vectors.exact_source_chunks(kb_id, missing, self.profile)
        compatible = {
            text: point
            for text, point in sources.items()
            if set(point["payload"]["domain_ids"]) == set(first_by_text[text]["payload"]["domain_ids"])
        }
        if not compatible:
            return exact
        members = await c.metadata.vector_members(
            kb_id, chunk_ids=[point["id"] for point in compatible.values()], active_only=True
        )
        by_chunk = {row["chunk_id"]: row["vector_id"] for row in members}
        ids = sorted(set(by_chunk.values()))
        points = await c.vectors.read(kb_id, "chunk_vectors", ids, with_vectors=True)
        by_id = {
            point["id"]: point for point in points if point["payload"]["embedding_profile"] == self.profile
        }
        for text, point in compatible.items():
            representative = by_id.get(by_chunk.get(point["id"]))
            if representative is not None:
                exact[text] = representative
        return exact

    async def _judge(self, old, new):
        if self.decision_failed or set(old["domain_ids"]) != set(new["domain_ids"]):
            return False, False

        async def state(chunk):
            document_id = chunk["document_id"]
            if document_id not in self.documents:
                self.documents[document_id] = await self.c.metadata.document(self.kb_id, document_id)
            document = self.documents[document_id]
            source = document["source"]
            return {
                "text": chunk["text"],
                "section": chunk.get("section_path", []),
                "document": document["original_name"],
                "source": {
                    key: source[key]
                    for key in ("published_at", "effective_at", "source_type")
                    if key in source
                },
                "ingested_at": document["created_at"],
            }

        old_state, new_state = await state(old), await state(new)
        try:
            return await self.c.language.chunk_coverage(
                old_state, new_state, self.c.settings.chunk_decision_model
            )
        except KGError as exc:
            self.decision_failed = True
            log.warning("Chunk coverage unavailable for job %s (%s)", self.job["job_id"], exc.code)
            return False, False

    async def _delete(self, draft, persist, embed):
        kb_id, document_id = self.kb_id, self.job["document_id"]
        removed = await self.c.metadata.vector_members(kb_id, document_ids=[document_id])
        vector_ids = sorted({row["vector_id"] for row in removed})
        old_points = await self.c.vectors.read(kb_id, "chunk_vectors", vector_ids, with_vectors=True)
        if {point["id"] for point in old_points} != set(vector_ids):
            raise KGError("SOURCE_UNAVAILABLE", "A source vector group is missing", 503)
        remaining = defaultdict(list)
        for member in await self.c.metadata.vector_members(kb_id, vector_ids=vector_ids):
            if member["document_id"] != document_id:
                remaining[member["vector_id"]].append(member)

        points, members, writes, deleted = {}, [], set(), []
        for old in old_points:
            group_id = old["id"]
            survivors = remaining[group_id]
            if any(row["chunk_id"] == old["payload"]["representative_chunk_id"] for row in survivors):
                points[group_id] = old
                members.extend(survivors)
                continue
            deleted.append(group_id)
            if survivors:
                ids = sorted(row["chunk_id"] for row in survivors)
                chunks = await self.c.vectors.read(kb_id, "chunks", ids)
                if {point["id"] for point in chunks} != set(ids):
                    raise KGError("SOURCE_UNAVAILABLE", "A surviving source chunk is missing", 503)
                regrouped, reassigned, new_writes = await self._group(
                    chunks, draft, persist, embed, compare_local=True
                )
                points.update(regrouped)
                members.extend(reassigned)
                writes.update(new_writes)
        return points, members, writes, deleted

    async def _reindex(self, chunks, draft, persist, embed):
        kb_id = self.kb_id
        members = await self.c.metadata.vector_members(kb_id)
        old_points = await self.c.vectors.all(
            kb_id, "chunk_vectors", with_vectors=False, max_points=self.c.settings.maintenance_scan_limit
        )
        source_chunks = {chunk["id"]: chunk for chunk in chunks}
        if {row["chunk_id"] for row in members} != source_chunks.keys() or {
            row["vector_id"] for row in members
        } != {point["id"] for point in old_points}:
            raise KGError("SOURCE_UNAVAILABLE", "Chunk vector membership is incomplete", 503)
        representatives = await self._embed(
            [source_chunks[point["payload"]["representative_chunk_id"]] for point in old_points],
            draft,
            persist,
            embed,
        )
        points = {
            point["id"]: _representative(kb_id, rep, self.profile, point["id"])
            for point, rep in zip(old_points, representatives, strict=True)
        }
        return points, members
