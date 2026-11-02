"""管理图对象与文档切片两类向量集合，提供有预算的读写与扫描。"""

from __future__ import annotations

import os
import math
import re
from collections import Counter
from functools import wraps

import httpx
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from qdrant_client import AsyncQdrantClient, models

from ...domain import EmbeddingProfile, KGError, valid_id


def storage_request(function):
    """将向量存储与 HTTP 客户端异常转换为统一的可重试存储错误。"""

    @wraps(function)
    async def call(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (UnexpectedResponse, ResponseHandlingException, httpx.HTTPError) as exc:
            raise KGError(
                "UPSTREAM_UNAVAILABLE", "Vector storage request could not complete", 503, retryable=True
            ) from exc

    return call


class Vectors:
    def __init__(self, settings):
        self.settings = settings
        self.client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=os.getenv(settings.qdrant_api_key_env),
            timeout=30,
            check_compatibility=False,
        )

    async def close(self):
        await self.client.close()

    @storage_request
    async def ready(self):
        await self.client.get_collections()
        return True

    @staticmethod
    def collection(kb_id, kind):
        if kind not in {"objects", "chunks"}:
            raise KGError("INVALID_ARGUMENT", "Unknown vector collection kind")
        return "kg_" + valid_id(kb_id).replace("-", "_") + "_" + kind

    @storage_request
    async def exists(self, kb_id, kind):
        return await self.client.collection_exists(self.collection(kb_id, kind))

    @storage_request
    async def ensure(self, kb_id, profile_data, recreate=False):
        """按有效向量配置创建或重建两类集合，并核对已有集合的向量空间。"""
        profile = EmbeddingProfile.model_validate(profile_data)
        for kind in ("objects", "chunks"):
            name = self.collection(kb_id, kind)
            exists = await self.client.collection_exists(name)
            if exists and recreate:
                await self.client.delete_collection(name)
                exists = False
            if not exists:
                await self.client.create_collection(
                    name,
                    vectors_config=models.VectorParams(
                        size=profile.dimension, distance=models.Distance(profile.distance)
                    ),
                )
                for field in ("kb_id", "document_id", "entity_type", "domain_ids", "object_id"):
                    await self.client.create_payload_index(
                        name, field, models.PayloadSchemaType.KEYWORD, wait=True
                    )
                await self.client.create_payload_index(
                    name,
                    "text" if kind == "chunks" else "search_text",
                    models.TextIndexParams(type="text", tokenizer=models.TokenizerType.MULTILINGUAL),
                    wait=True,
                )

            info = await self.client.get_collection(name)
            config = info.config.params.vectors
            if (
                not isinstance(config, models.VectorParams)
                or config.size != profile.dimension
                or config.distance != models.Distance(profile.distance)
            ):
                raise KGError(
                    "INDEX_PROFILE_MISMATCH", "Collection does not match the effective embedding profile", 503
                )

    @staticmethod
    def filter(kb_id, *, document_ids=None, entity_types=None, domain_ids=None):
        must = [models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id))]
        for name, values in (
            ("document_id", document_ids),
            ("entity_type", entity_types),
            ("domain_ids", domain_ids),
        ):
            if values:
                must.append(models.FieldCondition(key=name, match=models.MatchAny(any=values)))
        return models.Filter(must=must)

    @storage_request
    async def scan(self, kb_id, kind, *, limit, offset=None, with_vectors=False, document_ids=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise KGError("INVALID_ARGUMENT", "Vector scan limit must be between 1 and 10000")
        if not await self.exists(kb_id, kind):
            return {"items": [], "next_offset": None, "complete": True, "scanned": 0}
        points, next_offset = await self.client.scroll(
            self.collection(kb_id, kind),
            scroll_filter=self.filter(kb_id, document_ids=document_ids),
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        items = [{"id": str(point.id), "payload": point.payload, "vector": point.vector} for point in points]
        return {
            "items": items,
            "next_offset": str(next_offset) if next_offset is not None else None,
            "complete": next_offset is None,
            "scanned": len(items),
        }

    @storage_request
    async def all(self, kb_id, kind, with_vectors=True, document_ids=None, *, max_points):
        """在维护预算内读取完整集合，达到边界时额外探测以避免误报完整。"""
        if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1:
            raise KGError("INVALID_ARGUMENT", "A positive complete-scan point budget is required")
        result, offset = [], None
        while len(result) < max_points:
            page = await self.scan(
                kb_id,
                kind,
                limit=min(256, max_points - len(result)),
                offset=offset,
                with_vectors=with_vectors,
                document_ids=document_ids,
            )
            result.extend(page["items"])
            if page["complete"]:
                return result
            offset = page["next_offset"]

        # 正好达到上限不代表集合已读完，探测下一条后才能宣告完整。
        probe = await self.scan(
            kb_id, kind, limit=1, offset=offset, with_vectors=False, document_ids=document_ids
        )
        if not probe["items"] and probe["complete"]:
            return result
        raise KGError(
            "SCAN_BUDGET_EXCEEDED",
            "A complete vector scan exceeds the configured maintenance budget",
            422,
            collection=kind,
            max_points=max_points,
            scanned=len(result),
            execution_complete=False,
        )

    @storage_request
    async def read(self, kb_id, kind, ids):
        if not ids or not await self.exists(kb_id, kind):
            return []
        points = []
        for object_id in ids:
            valid_id(object_id)
        for start in range(0, len(ids), 256):
            points.extend(
                await self.client.retrieve(
                    self.collection(kb_id, kind),
                    ids=ids[start : start + 256],
                    with_payload=True,
                    with_vectors=False,
                )
            )
        return [{"id": str(p.id), "payload": p.payload} for p in points if p.payload.get("kb_id") == kb_id]

    @storage_request
    async def upsert(self, kb_id, kind, points):
        """先校验全部点的知识库与维度，再分批写入并确认每批完成。"""
        if not points:
            return
        info = await self.client.get_collection(self.collection(kb_id, kind))
        dimension = info.config.params.vectors.size
        for point in points:
            valid_id(point["id"])
            if point["payload"].get("kb_id") != kb_id:
                raise KGError("INVALID_ARGUMENT", "Index point belongs to another knowledge base")
            profile = EmbeddingProfile.model_validate(point["payload"].get("embedding_profile"))
            if (
                profile.dimension != dimension
                or len(point["vector"]) != dimension
                or any(
                    isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
                    for value in point["vector"]
                )
            ):
                raise KGError("INVALID_ARGUMENT", "Index vector does not match the collection dimension")

        # 全量校验通过后才开始副作用；每批都要求服务端确认完成。
        for start in range(0, len(points), 100):
            batch = points[start : start + 100]
            ack = await self.client.upsert(
                self.collection(kb_id, kind), [models.PointStruct(**p) for p in batch], wait=True
            )
            if ack.status != models.UpdateStatus.COMPLETED:
                raise KGError("UPSTREAM_UNAVAILABLE", "Qdrant did not confirm the write", 503, retryable=True)

    @storage_request
    async def delete(self, kb_id, kind, ids=None, document_ids=None, all_points=False):
        """按显式 ID 或范围删除，并要求存储确认删除完成。"""
        if not await self.exists(kb_id, kind):
            return
        if ids is not None:
            if not ids:
                return
            selector = models.PointIdsList(points=ids)
        else:
            if not document_ids and not all_points:
                return
            selector = models.FilterSelector(filter=self.filter(kb_id, document_ids=document_ids))

        ack = await self.client.delete(self.collection(kb_id, kind), selector, wait=True)
        if ack.status != models.UpdateStatus.COMPLETED:
            raise KGError("UPSTREAM_UNAVAILABLE", "Qdrant did not confirm deletion", 503, retryable=True)

    @storage_request
    async def search(self, kb_id, kind, vector, limit, filters):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise KGError("INVALID_ARGUMENT", "Invalid vector candidate budget")
        if any(
            isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
            for value in vector
        ):
            raise KGError("INVALID_ARGUMENT", "Query vector must contain finite numbers")
        if not await self.exists(kb_id, kind):
            return []
        info = await self.client.get_collection(self.collection(kb_id, kind))
        if len(vector) != info.config.params.vectors.size:
            raise KGError("INDEX_PROFILE_MISMATCH", "Query vector dimension differs from the collection", 503)
        result = await self.client.query_points(
            self.collection(kb_id, kind),
            query=vector,
            query_filter=self.filter(
                kb_id,
                document_ids=filters.document_ids if kind == "chunks" else None,
                entity_types=filters.entity_types if kind == "objects" else None,
                domain_ids=filters.domain_ids,
            ),
            limit=limit,
            with_payload=True,
        )
        return [
            {"id": str(point.id), "payload": point.payload, "score": point.score} for point in result.points
        ]

    @storage_request
    async def keyword(self, kb_id, query, limit, filters):
        """按全文条件读取切片，并额外读取一条判断是否被预算截断。"""
        if not await self.exists(kb_id, "chunks"):
            return [], False
        selection = self.filter(kb_id, document_ids=filters.document_ids, domain_ids=filters.domain_ids)
        stop = set(
            "a an the this that these those is are was were be been being of to in on at for from by with and or as it its any all what which who how when where does do did under about please find show me provide contains contain states state clause clauses contract agreement document".split()
        )
        terms = list(
            dict.fromkeys(t for t in re.findall(r"\w+", query.casefold()) if len(t) > 1 and t not in stop)
        )[:32]
        if not terms:
            terms = [query]
        selection.should = [
            models.FieldCondition(key="text", match=models.MatchText(text=term)) for term in terms
        ]
        points, offset = await self.client.scroll(
            self.collection(kb_id, "chunks"), scroll_filter=selection, limit=limit + 1, with_payload=True
        )
        truncated = len(points) > limit or offset is not None
        points = points[:limit]
        counts = [Counter(re.findall(r"\w+", p.payload.get("text", "").casefold())) for p in points]
        frequencies = {term: sum(term in count for count in counts) for term in terms}
        document_lengths = [sum(count.values()) for count in counts]
        average = sum(document_lengths) / max(len(counts), 1)
        inverse_document_frequency = {
            term: math.log(1 + (len(points) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            for term in terms
        }
        ranked = []
        for point, count, length in zip(points, counts, document_lengths):
            score = 0.0
            for term in terms:
                tf = count[term]
                if tf:
                    idf = inverse_document_frequency[term]
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(average, 1)))
            ranked.append({"id": str(point.id), "payload": point.payload, "score": score})
        ranked.sort(key=lambda hit: (-hit["score"], hit["id"]))
        return ranked, truncated
