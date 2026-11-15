from __future__ import annotations

from collections import defaultdict

from ..ports.resources import Resources
from .contracts import Filters


async def search_chunk_vectors(
    r: Resources, kb_id: str, vector: list[float], limit: int, filters: Filters
) -> list[dict]:
    """从有效向量成员中读取查询命中的原文切片。"""
    scoped = bool(filters.document_ids or filters.domain_ids)
    members = None
    ids = None
    if scoped:
        members = await r.metadata.vector_members(
            kb_id,
            document_ids=filters.document_ids or None,
            domain_ids=filters.domain_ids or None,
            active_only=True,
        )
        ids = sorted({row["vector_id"] for row in members})
        if not ids:
            return []
    hits = await r.vectors.search(kb_id, "chunk_vectors", vector, limit, filters, point_ids=ids)
    if not hits:
        return []
    hit_ids = {hit["id"] for hit in hits}
    if members is None:
        members = await r.metadata.vector_members(kb_id, vector_ids=sorted(hit_ids), active_only=True)
    by_vector = defaultdict(list)
    for row in members:
        if row["vector_id"] in hit_ids:
            by_vector[row["vector_id"]].append(row["chunk_id"])
    selected = []
    for hit in hits:
        group = by_vector[hit["id"]]
        if not group:
            continue
        representative = hit["payload"]["representative_chunk_id"]
        chunk_id = representative if representative in group else group[0]
        selected.append((hit, chunk_id, group))
    chunks = {
        point["id"]: point["payload"]
        for point in await r.vectors.read(kb_id, "chunks", [key for _, key, _ in selected])
    }
    return [
        {"id": key, "payload": chunks[key], "score": hit["score"], "member_chunk_ids": group}
        for hit, key, group in selected
        if key in chunks
    ]
