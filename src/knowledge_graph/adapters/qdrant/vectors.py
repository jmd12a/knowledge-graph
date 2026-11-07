"""管理图对象与文档切片两类向量集合，提供有预算的读写与扫描。"""

from __future__ import annotations

import os
from hashlib import sha256
import math
import re
from collections import Counter
from functools import wraps

import httpx
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from qdrant_client import AsyncQdrantClient, models

from ...domain import EmbeddingProfile, KGError, canonical, valid_id


_REUSE_FIELD = "_embedding_key"


def _embedding_key(text, profile):
    return sha256(canonical([text, profile]).encode("utf-8")).hexdigest()


def _business_payload(payload):
    return {key: value for key, value in payload.items() if key != _REUSE_FIELD}


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
        if kind not in {"objects", "chunks", "chunk_vectors"}:
            raise KGError("INVALID_ARGUMENT", "Unknown vector collection kind")
        return "kg_" + valid_id(kb_id).replace("-", "_") + "_" + kind

    @storage_request
    async def exists(self, kb_id, kind):
        return await self.client.collection_exists(self.collection(kb_id, kind))

    @storage_request
    async def ensure(self, kb_id, profile_data, recreate=False):
        """正文点与代表向量分开建集合，证据切片仍按原 ID 读取。"""
        profile = EmbeddingProfile.model_validate(profile_data)
        for kind in ("objects", "chunks", "chunk_vectors"):
            name = self.collection(kb_id, kind)
            exists = await self.client.collection_exists(name)
            if exists and recreate:
                await self.client.delete_collection(name)
                exists = False
            vectors = {} if kind == "chunks" else models.VectorParams(
                size=profile.dimension, distance=models.Distance(profile.distance)
            )
            if not exists:
                await self.client.create_collection(name, vectors_config=vectors)
                fields = ("kb_id", "document_id", "domain_ids", _REUSE_FIELD) if kind == "chunks" else (
                    ("kb_id", "entity_type", "domain_ids", "object_id", _REUSE_FIELD)
                    if kind == "objects" else ("kb_id", _REUSE_FIELD)
                )
                for field in fields:
                    await self.client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD, wait=True)
                if kind == "chunks":
                    await self.client.create_payload_index(
                        name, "text", models.TextIndexParams(type="text", tokenizer=models.TokenizerType.MULTILINGUAL),
                        wait=True,
                    )

            info = await self.client.get_collection(name)
            config = info.config.params.vectors
            matches = config == {} if kind == "chunks" else (
                isinstance(config, models.VectorParams)
                and config.size == profile.dimension
                and config.distance == models.Distance(profile.distance)
            )
            if not matches:
                raise KGError("INDEX_PROFILE_MISMATCH", "Collection does not match the effective embedding profile", 503)

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
        name = self.collection(kb_id, kind)
        if not await self.client.collection_exists(name):
            return {"items": [], "next_offset": None, "complete": True, "scanned": 0}
        selection = self.filter(kb_id, document_ids=document_ids)
        return await self._scan_page(name, kind, selection, limit, offset, with_vectors)

    async def _scan_page(self, name, kind, selection, limit, offset, with_vectors):
        points, next_offset = await self.client.scroll(
            name,
            scroll_filter=selection,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        items = [
            {"id": str(point.id), "payload": _business_payload(point.payload), "vector": None if kind == "chunks" else point.vector}
            for point in points
        ]
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
        name = self.collection(kb_id, kind)
        if not await self.client.collection_exists(name):
            return []
        selection = self.filter(kb_id, document_ids=document_ids)
        result, offset = [], None
        while len(result) < max_points:
            page = await self._scan_page(
                name, kind, selection, min(256, max_points - len(result)), offset, with_vectors
            )
            result.extend(page["items"])
            if page["complete"]:
                return result
            offset = page["next_offset"]

        # 正好达到上限不代表集合已读完，探测下一条后才能宣告完整。
        probe = await self._scan_page(name, kind, selection, 1, offset, False)
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
    async def read(self, kb_id, kind, ids, *, with_vectors=False):
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
                    with_vectors=with_vectors,
                )
            )
        return [
            {
                "id": str(p.id),
                "payload": _business_payload(p.payload),
                **({"vector": None if kind == "chunks" else p.vector} if with_vectors else {}),
            }
            for p in points
            if p.payload.get("kb_id") == kb_id
        ]

    @storage_request
    async def count(self, kb_id, kind, *, document_ids=None):
        if not await self.exists(kb_id, kind):
            return 0
        return (
            await self.client.count(
                self.collection(kb_id, kind),
                count_filter=self.filter(kb_id, document_ids=document_ids),
                exact=True,
            )
        ).count

    async def _exact_vectors(self, kb_id, kind, texts, profile):
        if not texts or not await self.exists(kb_id, kind):
            return {}
        found = {}
        text_field = "search_text" if kind == "objects" else "text"
        dense = kind != "chunks"
        for start in range(0, len(texts), 64):
            batch = texts[start:start + 64]
            requests = [
                models.QueryRequest(
                    filter=models.Filter(must=[
                        models.FieldCondition(key="kb_id", match=models.MatchValue(value=kb_id)),
                        models.FieldCondition(
                            key=_REUSE_FIELD, match=models.MatchValue(value=_embedding_key(text, profile))
                        ),
                    ]),
                    limit=1, with_vector=dense, with_payload=True,
                )
                for text in batch
            ]
            responses = await self.client.query_batch_points(self.collection(kb_id, kind), requests=requests)
            for text, response in zip(batch, responses, strict=True):
                for point in response.points:
                    if (
                        point.payload.get(text_field) == text
                        and point.payload.get("embedding_profile") == profile
                        and (not dense or point.vector is not None)
                    ):
                        found[text] = {
                            "id": str(point.id), "payload": _business_payload(point.payload), "vector": point.vector,
                        }
        return found

    @storage_request
    async def exact_chunk_vectors(self, kb_id, texts, profile):
        return await self._exact_vectors(kb_id, "chunk_vectors", texts, profile)

    @storage_request
    async def exact_source_chunks(self, kb_id, texts, profile):
        return await self._exact_vectors(kb_id, "chunks", texts, profile)

    @storage_request
    async def reusable_vectors(self, kb_id, texts, profile):
        chunks = await self._exact_vectors(kb_id, "chunk_vectors", texts, profile)
        objects = await self._exact_vectors(kb_id, "objects", [text for text in texts if text not in chunks], profile)
        return {text: point["vector"] for text, point in (chunks | objects).items()}

    @storage_request
    async def near_chunk_vectors(self, kb_id, points, threshold):
        if not points or not await self.exists(kb_id, "chunk_vectors"):
            return {}
        found = {}
        for start in range(0, len(points), 64):
            batch = points[start:start + 64]
            requests = [
                models.QueryRequest(
                    query=point["vector"], filter=self.filter(kb_id), score_threshold=threshold,
                    limit=3, with_payload=True, with_vector=True,
                )
                for point in batch
            ]
            responses = await self.client.query_batch_points(self.collection(kb_id, "chunk_vectors"), requests=requests)
            for point, response in zip(batch, responses, strict=True):
                found[point["id"]] = [
                    {"id": str(hit.id), "payload": _business_payload(hit.payload), "vector": hit.vector}
                    for hit in response.points
                ]
        return found

    @storage_request
    async def upsert(self, kb_id, kind, points):
        """正文点写载荷，向量点写已校验的向量与复用键。"""
        if not points:
            return
        name = self.collection(kb_id, kind)
        dense = kind != "chunks"
        dimension = None
        if dense:
            info = await self.client.get_collection(name)
            dimension = info.config.params.vectors.size
        text_field = "search_text" if kind == "objects" else "text"
        batch = []
        for point in points:
            valid_id(point["id"])
            payload = point["payload"]
            if payload.get("kb_id") != kb_id:
                raise KGError("INVALID_ARGUMENT", "Index point belongs to another knowledge base")
            vector = point["vector"]
            if dense:
                profile = EmbeddingProfile.model_validate(payload["embedding_profile"])
                if (
                    not isinstance(vector, list) or profile.dimension != dimension or len(vector) != dimension
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)
                ):
                    raise KGError("INVALID_ARGUMENT", "Index vector does not match the collection dimension")
            elif vector is not None:
                raise KGError("INVALID_ARGUMENT", "Source chunks store payload only")
            payload = payload | {_REUSE_FIELD: _embedding_key(payload[text_field], payload["embedding_profile"])}
            batch.append(models.PointStruct(id=point["id"], vector=vector if dense else {}, payload=payload))

        for start in range(0, len(batch), 100):
            ack = await self.client.upsert(name, batch[start:start + 100], wait=True)
            if ack.status != models.UpdateStatus.COMPLETED:
                raise KGError("UPSTREAM_UNAVAILABLE", "Qdrant did not confirm the write", 503, retryable=True)

    @storage_request
    async def delete(self, kb_id, kind, ids=None, document_ids=None, all_points=False):
        """按显式 ID 或范围删除，并要求存储确认删除完成。"""
        name = self.collection(kb_id, kind)
        if ids is not None:
            if not ids:
                return
            selector = models.PointIdsList(points=ids)
        else:
            if not document_ids and not all_points:
                return
            selector = models.FilterSelector(filter=self.filter(kb_id, document_ids=document_ids))

        if not await self.client.collection_exists(name):
            return
        ack = await self.client.delete(name, selector, wait=True)
        if ack.status != models.UpdateStatus.COMPLETED:
            raise KGError("UPSTREAM_UNAVAILABLE", "Qdrant did not confirm deletion", 503, retryable=True)

    @storage_request
    async def search(self, kb_id, kind, vector, limit, filters, *, point_ids=None):
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
        scope = self.filter(
            kb_id,
            entity_types=filters.entity_types if kind == "objects" else None,
            domain_ids=filters.domain_ids if kind == "objects" else None,
        )
        if point_ids is not None:
            if not point_ids:
                return []
            scope.must.append(models.HasIdCondition(has_id=point_ids))
        result = await self.client.query_points(
            self.collection(kb_id, kind), query=vector, query_filter=scope, limit=limit, with_payload=True,
        )
        return [
            {"id": str(point.id), "payload": _business_payload(point.payload), "score": point.score}
            for point in result.points
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
            ranked.append({"id": str(point.id), "payload": _business_payload(point.payload), "score": score})
        ranked.sort(key=lambda hit: (-hit["score"], hit["id"]))
        return ranked, truncated
