from __future__ import annotations

import asyncio
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy

from ...domain import valid_id
from ...domain.errors import KGError
from ...domain.model import BusinessModel, check_value
from ..contracts import Filters, SearchRequest, coverage, envelope
from .rerank import compact_rerank


def scalar_equal(left, right):
    """按业务标量类型比较，避免 Python 将布尔值与数字视为相等。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _public_atomic_declaration(variant):
    result = deepcopy(variant)
    for declaration in result.get("source_declarations", []):
        declaration.pop("field_evidence", None)
        declaration.pop("resolved_references", None)
    return result


def public_item(item):
    """保留完整规则值；内嵌证据统一经独立证据预算返回。"""
    result = deepcopy(item)
    result.pop("search_text", None)
    for groups in (result.get("atomic_groups", {}), result.get("matched_atomic_groups", {})):
        for group in groups.values():
            group["declarations"] = [
                _public_atomic_declaration(variant) for variant in group.get("declarations", [])
            ]
    return result


def add_warning(result, code, *, component, reason=None, scope=None, budget=None):
    result["warnings"].append({"code": code, **({"reason": reason} if reason else {})})
    if result["status"] != "needs_disambiguation":
        result["status"] = "partial"
    result["coverage"].append(coverage(component, "top_k", scope, budget, code))


def _scoped_keyword_candidates(items, query):
    """Rank the actual selected-source declarations, not whole-KB descriptions."""
    stop = set(
        "a an the this that these those is are was were be been being of to in on at for from by with and or as it its any all what which who how when where does do did under about please find show me provide highlight parts related should reviewed lawyer details contract agreement document".split()
    )
    terms = set(re.findall(r"\w+", query.casefold())) - stop
    terms = {term for term in terms if len(term) > 1}
    if not terms:
        return []
    counts = [Counter(re.findall(r"\w+", item.get("search_text", "").casefold())) for item in items]
    frequency = {term: sum(term in count for count in counts) for term in terms}
    document_lengths = [sum(count.values()) for count in counts]
    average = sum(document_lengths) / max(len(counts), 1)
    inverse_document_frequency = {
        term: math.log(1 + (len(items) - frequency[term] + 0.5) / (frequency[term] + 0.5)) for term in terms
    }
    scores = []
    for item, count, length in zip(items, counts, document_lengths):
        score = sum(
            inverse_document_frequency[term]
            * count[term]
            * 2.2
            / (count[term] + 1.2 * (0.25 + 0.75 * length / max(average, 1)))
            for term in terms
            if count[term]
        )
        if score:
            scores.append((score, item))
    scores.sort(key=lambda pair: (-pair[0], pair[1]["entity_id"]))
    return [item for _, item in scores]


class SearchMixin:
    def validate_filters(self, model, filters):
        if set(filters.entity_types) - model.node_types.keys():
            raise KGError("INVALID_ARGUMENT", "Unknown entity type filter")
        if set(filters.domain_ids) - {domain["id"] for domain in model.domains}:
            raise KGError("INVALID_ARGUMENT", "Unknown domain filter")
        definitions = list(model.node_types.values()) + list(model.relation_types.values())
        for field, value in filters.properties.items():
            allowed = [
                definition.properties[field]
                for definition in definitions
                if field in definition.properties and definition.properties[field].queryable
            ]
            if not allowed or value is None or isinstance(value, (dict, list)):
                raise KGError(
                    "INVALID_ARGUMENT", "Property filters require queryable scalar equality", field=field
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise KGError("INVALID_ARGUMENT", "Property filter must be finite", field=field)
            for definition in allowed:
                try:
                    check_value(value, definition)
                    break
                except KGError:
                    continue
            else:
                raise KGError(
                    "INVALID_ARGUMENT", "Property filter has an incompatible type or value", field=field
                )
        for document_id in filters.document_ids:
            valid_id(document_id)

    async def _validate_documents(self, kb_id, document_ids):
        documents = {}
        for document_id in document_ids:
            document = await self.r.metadata.document(kb_id, document_id)
            if document.get("deleted_at"):
                raise KGError("NOT_FOUND", "Selected document has been deleted", 404, document_id=document_id)
            title = document.get("source", {}).get("title")
            documents[document_id] = str(title or document.get("original_name") or "")[:200]
        return documents

    async def _scope(self, kb, filters):
        """仅用所选资料的贡献重聚合知识；未指定资料时返回 None。"""
        model = BusinessModel.model_validate(kb["model"])
        self.validate_filters(model, filters)
        if not filters.document_ids:
            return None
        documents = await self._validate_documents(kb["kb_id"], filters.document_ids)
        from ..construction.merge import aggregate, effective_contributions

        # 整库聚合可能包含其他来源的值，不能仅按 document_ids 过滤后复用。
        contributions = await self.r.metadata.contributions(kb["kb_id"], filters.document_ids)
        active, suppressed = effective_contributions(contributions)
        entities, relations = aggregate(kb["kb_id"], active, model)
        for item in entities + relations:
            item["scope"] = "selected_documents"
            item["selected_document_ids"] = list(filters.document_ids)
            # 整库描述与修订标识不适用于按来源重聚合的临时视图。
            item.pop("description", None)
            item.pop("object_revision", None)
        return {
            "entity": {item["entity_id"]: item for item in entities},
            "relation": {item["relation_id"]: item for item in relations},
            "contributions": active,
            "documents": documents,
            "suppressed_dependencies": len(suppressed),
        }

    @staticmethod
    def _matches(item, filters, model=None, *, graph=False):
        """判断过滤条件，并原地更新匹配属性和原子声明详情。"""
        item.pop("matched_properties", None)
        item.pop("matched_atomic_groups", None)
        if (
            item.get("kind") == "entity"
            and filters.entity_types
            and item.get("entity_type") not in filters.entity_types
        ):
            return False
        if filters.domain_ids and not set(item.get("domain_ids", [])) & set(filters.domain_ids):
            return False
        if filters.document_ids and not set(item.get("document_ids", [])) & set(filters.document_ids):
            return False
        definition = None
        if model is not None:
            definition = (
                model.node_types.get(item.get("entity_type"))
                if item.get("kind") == "entity"
                else model.relation_types.get(item.get("relation_type"))
            )
        field_groups = {
            field: group.name for group in getattr(definition, "atomic_groups", []) for field in group.fields
        }
        if definition is None:
            field_groups = {
                field: name
                for name, group in item.get("atomic_groups", {}).items()
                for variant in group.get("declarations", [])
                for field in variant.get("value", {}).get("fields", {})
            }
        # 图查询仅向声明该属性的类型应用条件，允许路径经过其他类型。
        matched, atomic_conditions, matched_groups = {}, defaultdict(dict), {}
        for field, value in filters.properties.items():
            if (
                graph
                and definition is not None
                and (field not in definition.properties or not definition.properties[field].queryable)
            ):
                continue
            if definition is not None:
                prop_definition = definition.properties.get(field)
                if prop_definition is None or not prop_definition.queryable:
                    return False
                try:
                    check_value(value, prop_definition)
                except KGError:
                    return False
            if field in field_groups:
                atomic_conditions[field_groups[field]][field] = value
                continue
            prop = item.get("properties", {}).get(field, {})
            values = [
                variant["value"]
                for variant in prop.get("values", [])
                if scalar_equal(variant["value"], value)
            ]
            if not values:
                return False
            matched[field] = {"values": values, "conflicted": prop.get("state") == "conflicted"}

        # 同一原子组的条件必须由同一条完整声明满足，不能拼接不同声明的字段。
        for name, conditions in atomic_conditions.items():
            group = item.get("atomic_groups", {}).get(name, {})
            declarations = []
            for variant in group.get("declarations", []):
                statement = variant.get("value", {})
                fields = statement.get("fields", {})
                if all(
                    field in fields and scalar_equal(fields[field], value)
                    for field, value in conditions.items()
                ):
                    declarations.append(_public_atomic_declaration(variant))
            if not declarations:
                return False
            contribution_ids = sorted(
                {
                    claim_id
                    for declaration in declarations
                    for claim_id in declaration.get("contribution_ids", [])
                }
            )
            conflicted = group.get("state") == "conflicted"
            path = "/atomic_groups/" + name
            matched_groups[name] = {
                "conditions": deepcopy(conditions),
                "state": group.get("state"),
                "conflicted": conflicted,
                "contribution_ids": contribution_ids,
                "declarations": declarations,
                "conflicts": [
                    deepcopy(conflict)
                    for conflict in item.get("conflicts", [])
                    if conflict.get("field_path") == path
                    or conflict.get("field_path", "").startswith(path + "/")
                ],
            }
            for field in conditions:
                matched[field] = {
                    "values": [
                        deepcopy(declaration["value"]["fields"][field]) for declaration in declarations
                    ],
                    "atomic_group": name,
                    "contribution_ids": contribution_ids,
                    "conflicted": conflicted,
                }
        if matched:
            item["matched_properties"] = matched
        if matched_groups:
            item["matched_atomic_groups"] = matched_groups
        return True

    @staticmethod
    def _summary(item, channels, score):
        names = (
            "kb_id",
            "kind",
            "origin",
            "entity_id",
            "entity_type",
            "identity",
            "canonical_name",
            "aliases",
            "domain_ids",
            "document_ids",
            "completeness",
            "missing_fields",
            "conflicts",
            "scope",
            "selected_document_ids",
            "matched_properties",
            "matched_atomic_groups",
        )
        result = {key: deepcopy(item[key]) for key in names if key in item}
        text = item.get("search_text", "")
        return result | {
            "id": item["entity_id"],
            "match_channels": sorted(channels),
            "score": score,
            "summary": text[:500],
            "summary_truncated": len(text) > 500,
        }

    @staticmethod
    def _exact_channels(item, query):
        channels = set()
        if item.get("canonical_name") == query or query in item.get("aliases", []):
            channels.add("exact_name")
        fields = item.get("identity", {}).get("fields", {})
        if len(fields) == 1 and any(str(value) == query for value in fields.values()):
            channels.add("exact_identity")
        if channels:
            channels.add("exact")
        return channels

    async def describe_model(self, kb_id, entity_types=None, domain_ids=None):
        kb_id = valid_id(kb_id)
        async with self.r.gate.read(kb_id) as kb:
            model = BusinessModel.model_validate(kb["model"])
            filters = Filters(entity_types=entity_types or [], domain_ids=domain_ids or [])
            self.validate_filters(model, filters)
            value = model.model_dump()
            if entity_types:
                value["node_types"] = {
                    key: val for key, val in value["node_types"].items() if key in entity_types
                }
                value["relation_types"] = {
                    key: val
                    for key, val in value["relation_types"].items()
                    if set(val["source_types"] + val["target_types"]) & set(entity_types)
                }
            if domain_ids:
                value["domains"] = [domain for domain in value["domains"] if domain["id"] in domain_ids]
            result = envelope(kb_id)
            result.update(model=value, model_revision=kb["model_revision"])
            result["coverage"].append(coverage("model", "exact", filters.model_dump()))
            return result

    async def search_knowledge(self, kb_id, request: SearchRequest):
        kb_id = valid_id(kb_id)
        request = SearchRequest.model_validate(request)
        async with self.r.gate.read(kb_id) as kb:
            try:
                async with asyncio.timeout(self.r.settings.retrieval_timeout):
                    return await self._search(kb, request)
            except TimeoutError as exc:
                raise KGError(
                    "TIMEOUT", "Search exceeded the total query budget", 504, retryable=True
                ) from exc

    async def _rank_search(
        self, kb_id, request, ordered, records, result, component, scope, documents, *, has_exact=False
    ):
        settings = self.r.settings
        if not request.semantic or not settings.rerank_enabled or len(ordered) <= 1 or has_exact:
            return ordered, set()
        try:
            order, reranked, metrics = await compact_rerank(
                self.r, kb_id, request, ordered, records, component, scope, documents
            )
        except (KGError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            add_warning(
                result,
                "RERANK_UNAVAILABLE",
                component=component,
                reason="TIMEOUT"
                if isinstance(exc, TimeoutError)
                else getattr(exc, "code", "INVALID_RANKING"),
                scope=request.filters.model_dump(),
            )
            return ordered, set()
        if invalid_ids := metrics.get("invalid_rerank_ids"):
            result["warnings"].append({"code": "RERANK_IDS_IGNORED", "count": invalid_ids})
        if invalid_evidence := metrics["invalid_rerank_evidence"]:
            result["warnings"].append(
                {"code": "RERANK_EVIDENCE_IGNORED", "stored": invalid_evidence, "model": 0}
            )
        result["coverage"].append(
            coverage(component, "top_k", request.filters.model_dump(), metrics | {"limit": request.limit})
        )
        return order, reranked

    async def _search_chunk_vectors(self, kb_id, vector, limit, filters):
        r = self.r
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

    async def _search(self, kb, request, scope=None):
        """合并精确、关键词与语义候选，分别报告对象和文档的覆盖范围。"""
        r, kb_id = self.r, kb["kb_id"]
        model = BusinessModel.model_validate(kb["model"])
        self.validate_filters(model, request.filters)
        documents = {}
        if scope is None and request.filters.document_ids:
            if "entity" in request.kinds:
                scope = await self._scope(kb, request.filters)
            else:
                documents = await self._validate_documents(kb_id, request.filters.document_ids)
        if scope is not None:
            documents = scope.get("documents", {})
        result, scan = envelope(kb_id), r.settings.candidate_scan_limit
        actual_scope = request.filters.model_dump()
        records, channels, ranks = {}, defaultdict(set), defaultdict(float)
        candidate_budget = {"limit": request.limit, "scan_limit": scan}
        if scope and scope.get("suppressed_dependencies"):
            add_warning(
                result,
                "IDENTITY_DEPENDENCY_OUT_OF_SCOPE",
                component="object_candidates",
                scope=actual_scope,
            )

        def admit(item, channel, rank, *, ranked=True):
            item = deepcopy(item)
            if not self._matches(item, request.filters, model):
                return
            object_id = item["entity_id"]
            records[object_id] = item
            already_ranked = channel in channels[object_id]
            channels[object_id].add(channel)
            if channel == "exact":
                channels[object_id].update(self._exact_channels(item, request.query))
            if ranked and not already_ranked:
                # A source-vector hit may reach the same entity through many chunks;
                # sorted hits contribute only their first (best) rank for this channel.
                # 按各通道名次融合，避免直接比较不同检索器的原始分值。
                ranks[object_id] += 1 / (60 + rank)

        if "entity" in request.kinds:
            if scope is not None:
                ordered_scope = sorted(scope["entity"].values(), key=lambda item: item["entity_id"])
                exact = [item for item in ordered_scope if self._exact_channels(item, request.query)]
                keyword = _scoped_keyword_candidates(ordered_scope, request.query)
                fulltext = []
            else:
                selectors = {
                    "entity_types": request.filters.entity_types,
                    "domain_ids": request.filters.domain_ids,
                    "document_ids": request.filters.document_ids,
                }
                exact, keyword = await r.graph.candidates(
                    kb_id,
                    request.query,
                    scan,
                    **selectors,
                )
                try:
                    fulltext = await r.graph.fulltext(kb_id, request.query, scan, **selectors)
                except KGError as exc:
                    fulltext = []
                    add_warning(
                        result,
                        "FULLTEXT_UNAVAILABLE",
                        component="object_candidates",
                        reason=exc.code,
                        scope=actual_scope,
                        budget=candidate_budget | {"channel": "fulltext"},
                    )
            for channel, items in (("exact", exact), ("keyword", keyword), ("fulltext", fulltext)):
                if len(items) > scan:
                    add_warning(
                        result,
                        "CANDIDATE_SCAN_BUDGET",
                        component="object_candidates",
                        scope=actual_scope,
                        budget=candidate_budget | {"channel": channel},
                    )
                for rank, item in enumerate(items[:scan], 1):
                    admit(item, channel, rank, ranked=channel != "keyword" or scope is not None)

        vector = None
        collection = "chunk_vectors" if scope is not None or "entity" not in request.kinds else "objects"
        collections = {collection} | ({"chunk_vectors"} if "chunk" in request.kinds else set())
        if request.semantic and any([await r.vectors.exists(kb_id, kind) for kind in sorted(collections)]):
            try:
                vector = (await r.language.embed([request.query], kb["embedding_profile"]))[0]
            except KGError as exc:
                for component in (["object_candidates"] if "entity" in request.kinds else []) + (
                    ["documents"] if "chunk" in request.kinds else []
                ):
                    add_warning(
                        result,
                        "SEMANTIC_UNAVAILABLE",
                        component=component,
                        reason=exc.code,
                        scope=actual_scope,
                        budget=candidate_budget | {"channel": "vector"},
                    )
        fetch_limit = min(
            scan,
            max(
                request.limit * 5,
                request.limit,
                r.settings.rerank_candidate_limit if r.settings.rerank_enabled else 0,
            ),
        )
        source_hits = None
        if vector is not None and "entity" in request.kinds:
            if scope is not None:
                hits = await self._search_chunk_vectors(kb_id, vector, fetch_limit, request.filters)
            else:
                hits = await r.vectors.search(kb_id, collection, vector, fetch_limit, request.filters)
            if scope is not None:
                source_hits = hits
                # 所选来源视图通过切片证据找对象，避免使用整库对象向量引入其他来源。
                by_chunk = defaultdict(set)
                for claim in scope["contributions"]:
                    if claim["target_kind"] == "entity":
                        for reference in claim["evidence"]:
                            by_chunk[reference["chunk_id"]].add(claim["target_id"])
                for rank, hit in enumerate(hits, 1):
                    for object_id in sorted(set().union(*(by_chunk[key] for key in hit["member_chunk_ids"]))):
                        item = scope["entity"].get(object_id)
                        if item:
                            admit(item, "source_vector", rank)
            else:
                items = {
                    item["entity_id"]: item
                    for item in await r.graph.by_id(kb_id, "entity", [hit["id"] for hit in hits])
                }
                for rank, hit in enumerate(hits, 1):
                    if item := items.get(hit["id"]):
                        admit(item, "vector", rank)
            if (
                len(hits) >= fetch_limit
                and (request.filters.properties or scope is not None)
                and len(records) < request.limit
            ):
                add_warning(
                    result,
                    "FILTERED_CANDIDATE_BUDGET",
                    component="object_candidates",
                    scope=actual_scope,
                    budget=candidate_budget | {"vector_candidates": fetch_limit},
                )
        if "entity" in request.kinds:
            selected = sorted(
                records,
                key=lambda object_id: (
                    "exact_identity" not in channels[object_id],
                    "exact_name" not in channels[object_id],
                    -ranks[object_id],
                    records[object_id].get("canonical_name", ""),
                    object_id,
                ),
            )
            selected, reranked = await self._rank_search(
                kb_id,
                request,
                selected,
                records,
                result,
                "object_candidates",
                scope,
                documents,
                has_exact=any(values & {"exact_identity", "exact_name"} for values in channels.values()),
            )
            selected.sort(
                key=lambda key: ("exact_identity" not in channels[key], "exact_name" not in channels[key])
            )
            for rank, key in enumerate(selected, 1):
                if key in reranked:
                    channels[key].add("rerank")
                    ranks[key] = 1 / rank
            selected = selected[: request.limit]
            summaries = [self._summary(records[key], channels[key], ranks[key]) for key in selected]
            self._check_size(summaries)
            result["knowledge"].extend(summaries)
            used_channels = sorted({name for values in channels.values() for name in values})
            result["coverage"].append(
                coverage(
                    "object_candidates",
                    "top_k",
                    actual_scope,
                    candidate_budget | {"channels": used_channels, "fused_candidates": len(records)},
                )
            )
        if "chunk" in request.kinds:
            document_scope = {
                "document_ids": request.filters.document_ids,
                "domain_ids": request.filters.domain_ids,
            }
            keyword_hits, truncated = await r.vectors.keyword(kb_id, request.query, scan, request.filters)
            chunks, chunk_channels, scores = {}, defaultdict(set), defaultdict(float)
            for rank, hit in enumerate(keyword_hits, 1):
                chunks[hit["id"]] = hit["payload"]
                chunk_channels[hit["id"]].add("keyword")
                scores[hit["id"]] += 1 / (60 + rank)
            if truncated:
                add_warning(
                    result,
                    "CANDIDATE_SCAN_BUDGET",
                    component="documents",
                    scope=document_scope,
                    budget=candidate_budget | {"channel": "keyword"},
                )
            if vector is not None:
                hits = (
                    source_hits
                    if source_hits is not None
                    else await self._search_chunk_vectors(kb_id, vector, fetch_limit, request.filters)
                )
                for rank, hit in enumerate(hits, 1):
                    chunks[hit["id"]] = hit["payload"]
                    chunk_channels[hit["id"]].add("vector")
                    scores[hit["id"]] += 1 / (60 + rank)
            ordered = sorted(chunks, key=lambda key: (-scores[key], key))
            ordered, reranked = await self._rank_search(
                kb_id, request, ordered, chunks, result, "documents", scope, documents
            )
            for rank, chunk_id in enumerate(ordered, 1):
                if chunk_id in reranked:
                    chunk_channels[chunk_id].add("rerank")
                    scores[chunk_id] = 1 / rank
            for chunk_id in ordered[: request.limit]:
                payload = {
                    key: deepcopy(value)
                    for key, value in chunks[chunk_id].items()
                    if key != "embedding_profile"
                }
                result["knowledge"].append(
                    payload
                    | {
                        "kind": "chunk",
                        "origin": "document",
                        "id": chunk_id,
                        "chunk_id": chunk_id,
                        "match_channels": sorted(chunk_channels[chunk_id]),
                        "score": scores[chunk_id],
                    }
                )
            result["coverage"].append(
                coverage(
                    "documents",
                    "top_k",
                    document_scope,
                    candidate_budget
                    | {
                        "channels": ["keyword"]
                        + (["vector"] if vector is not None else [])
                        + (["rerank"] if reranked else []),
                        "entity_filters_apply_to": "object_candidates",
                        "fused_candidates": len(chunks),
                    },
                )
            )
        result["conflicts"] = [
            dict(conflict, fact_id=item.get("entity_id", item.get("id")))
            for item in result["knowledge"]
            for conflict in item.get("conflicts", [])
        ]
        if request.filters.document_ids:
            result.update(scope="selected_documents", selected_document_ids=request.filters.document_ids)
        return result
