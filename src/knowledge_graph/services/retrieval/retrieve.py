from __future__ import annotations

import asyncio
import re
from copy import deepcopy

from ...domain import canonical, valid_id
from ...domain.errors import KGError
from ...domain.model import BusinessModel, check_value, identity_value
from ...ports.resources import Resources
from ...prompts.retrieval import PLAN_SYSTEM
from ..contracts import GraphRequest, RetrievalPlan, RetrieveRequest, SearchRequest, coverage, envelope
from .evidence import EvidenceMixin
from .graph import GraphMixin
from .search import SearchMixin, public_item

UUID_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?![0-9A-Za-z])"
)


class Retrieval(SearchMixin, GraphMixin, EvidenceMixin):
    """组织自然语言规划、对象消歧、受约束图查询与来源证据。"""

    def __init__(self, resources: Resources):
        self.r = resources

    async def retrieve(self, kb_id, request: RetrieveRequest):
        kb_id = valid_id(kb_id)
        request = RetrieveRequest.model_validate(request)
        async with self.r.gate.read(kb_id) as kb:
            try:
                async with asyncio.timeout(self.r.settings.retrieval_timeout):
                    return await self._retrieve(kb, request)
            except TimeoutError as exc:
                raise KGError(
                    "TIMEOUT", "Retrieval exceeded the total query budget", 504, retryable=True
                ) from exc

    async def _resolutions(self, kb, request, model, scope):
        """校验调用方消歧锚点，并识别查询原文中的显式实体 ID。"""
        resolved = {}
        for resolution in request.resolved_entities:
            start, end = resolution.query_range
            if not 0 <= start < end <= len(request.query) or request.query[start:end] != resolution.mention:
                raise KGError("INVALID_ARGUMENT", "Resolved mention must equal its query range")
            item = await self.r.graph.get(kb["kb_id"], "entity", valid_id(resolution.entity_id))
            if item is None:
                raise KGError(
                    "NOT_FOUND",
                    "Resolved entity no longer exists in this knowledge base",
                    404,
                    entity_id=resolution.entity_id,
                )
            if scope is not None:
                item = deepcopy(scope["entity"].get(item["entity_id"]))
            if item is None or not self._matches(item, request.filters, model, graph=True):
                raise KGError(
                    "INVALID_ARGUMENT",
                    "Resolved entity is outside the requested scope",
                    entity_id=resolution.entity_id,
                )
            key = (start, end)
            if key in resolved and resolved[key]["entity_id"] != item["entity_id"]:
                raise KGError("INVALID_ARGUMENT", "One mention cannot resolve to different entities")
            resolved[key] = item
        for match in UUID_PATTERN.finditer(request.query):
            key = match.span(1)
            if key in resolved:
                continue
            object_id = valid_id(match.group(1))
            item = await self.r.graph.get(kb["kb_id"], "entity", object_id)
            if item is None:
                if match.group(1) == request.query.strip():
                    raise KGError("NOT_FOUND", "Knowledge object does not exist", 404)
                continue
            if scope is not None:
                item = deepcopy(scope["entity"].get(object_id))
            if item is not None and self._matches(item, request.filters, model, graph=True):
                resolved[key] = item
        return resolved

    @staticmethod
    def _validate_plan(plan, query, model, resolved, filters):
        """将模型计划限制在原文锚点、已配置类型和调用方确认范围内。"""
        if plan.intent in {"search", "documents"} and (plan.relation_types or plan.direction != "outgoing"):
            raise ValueError("Search and document plans cannot execute graph constraints")
        if set(plan.relation_types) - model.relation_types.keys():
            raise ValueError("Unknown planned relation type")
        anchors = set()
        for mention in plan.mentions:
            start, end = mention.query_range
            if not 0 <= start < end <= len(query) or query[start:end] != mention.mention:
                # Repair offsets only for a unique exact textual anchor; never resolve identity here.
                start = query.find(mention.mention) if mention.mention else -1
                if start < 0 or query.find(mention.mention, start + 1) >= 0:
                    raise ValueError("Invalid planned query range")
                end = start + len(mention.mention)
                if resolved and (start, end) not in resolved:
                    raise ValueError("Cannot relocate a caller-resolved mention")
                mention.query_range = (start, end)
            if (start, end) in anchors:
                raise ValueError("Duplicate planned mention range")
            anchors.add((start, end))
            if mention.entity_type is not None and mention.entity_type not in model.node_types:
                raise ValueError("Unknown planned entity type")
            if (
                mention.entity_type
                and filters.entity_types
                and mention.entity_type not in filters.entity_types
            ):
                raise ValueError("Planned entity type is outside the requested filters")
            if mention.identity:
                # 身份必须字段齐全且值出现于原文，不能把模型补全的值当成可靠身份。
                definitions = (
                    [model.node_types[mention.entity_type]]
                    if mention.entity_type
                    else list(model.node_types.values())
                )
                definitions = [
                    definition
                    for definition in definitions
                    if set(mention.identity) == set(definition.identity.fields)
                ]
                if not definitions:
                    raise ValueError("Plan does not contain a complete configured identity")
                for field, value in mention.identity.items():
                    if (
                        isinstance(value, (dict, list))
                        or value is None
                        or not _identity_literal_present(value, query)
                    ):
                        raise ValueError("Planned identity was not stated in the query")
                    if not any(
                        _valid_identity_value(value, definition.properties[field])
                        for definition in definitions
                    ):
                        raise ValueError("Planned identity has an invalid field type")
            item = resolved.get((start, end))
            if item and mention.entity_type and item["entity_type"] != mention.entity_type:
                raise ValueError("Planned mention type conflicts with the selected entity")
        if plan.intent in {"detail", "neighbors", "traverse", "paths"} and not plan.mentions:
            raise ValueError("Graph plans require anchored mentions")
        if plan.intent == "paths" and len(plan.mentions) < 2:
            raise ValueError("Path plans require two anchored objects")
        if plan.intent in {"detail", "neighbors", "traverse", "paths"} and not set(resolved).issubset(
            anchors
        ):
            raise ValueError("Plan omitted a caller-selected query anchor")

    async def _locate(self, kb, mention, request, model, scope, selected):
        """返回已确认对象或待消歧候选，同时保留候选检索的覆盖与告警。"""
        if selected is not None:
            return selected, [], [], []
        candidate_filters = request.filters.model_copy(
            update={
                "entity_types": [mention.entity_type] if mention.entity_type else request.filters.entity_types
            }
        )
        found = await self._search(
            kb,
            SearchRequest(query=mention.mention, filters=candidate_filters, limit=100, semantic=False),
            scope,
        )
        all_candidates = found["knowledge"]
        candidates = all_candidates[: request.limit]
        reliable = []
        for candidate in all_candidates:
            definition = model.node_types[candidate["entity_type"]]
            if mention.identity:
                if set(mention.identity) != set(definition.identity.fields):
                    continue
                identity = identity_value(
                    definition, mention.identity, candidate["identity"].get("document_id", "")
                )
                if canonical(candidate["identity"]) == canonical(identity):
                    reliable.append(candidate)
            elif (
                "exact_identity" in candidate["match_channels"] or "exact_name" in candidate["match_channels"]
            ):
                reliable.append(candidate)
        if mention.identity:
            types = [mention.entity_type] if mention.entity_type else list(model.node_types)
            documents = request.filters.document_ids or [""]
            for entity_type in types:
                definition = model.node_types[entity_type]
                if set(mention.identity) != set(definition.identity.fields):
                    continue
                for document_id in documents:
                    if definition.identity.scope == "document" and not document_id:
                        continue
                    identity = identity_value(definition, mention.identity, document_id)
                    object_id = await self.r.graph.find_identity(kb["kb_id"], entity_type, identity)
                    if object_id:
                        item = (
                            deepcopy(scope["entity"].get(object_id))
                            if scope is not None
                            else await self.r.graph.get(kb["kb_id"], "entity", object_id)
                        )
                        if item and self._matches(item, candidate_filters, model, graph=True):
                            reliable.append(item)
        reliable = {candidate["entity_id"]: candidate for candidate in reliable}
        exact_complete = not any(
            entry.get("reason") == "CANDIDATE_SCAN_BUDGET"
            and entry.get("budget", {}).get("channel") == "exact"
            for entry in found["coverage"]
        )
        # 精确扫描被截断时，即使当前只见一个候选，也不能断言它唯一。
        if len(reliable) == 1 and exact_complete:
            object_id = next(iter(reliable))
            item = (
                deepcopy(scope["entity"].get(object_id))
                if scope is not None
                else await self.r.graph.get(kb["kb_id"], "entity", object_id)
            )
            return item, candidates, found["coverage"], found["warnings"]
        if reliable:
            return None, candidates, found["coverage"], found["warnings"]
        # 语义相似只能补充候选，不能替调用方确认实体身份。
        semantic = await self._search(
            kb,
            SearchRequest(query=mention.mention, filters=candidate_filters, limit=100, semantic=True),
            scope,
        )
        return (
            None,
            semantic["knowledge"][: request.limit],
            found["coverage"] + semantic["coverage"],
            found["warnings"] + semantic["warnings"],
        )

    async def _retrieve(self, kb, request):
        """执行校验后的计划；任一对象尚有歧义时先返回候选。"""
        model = BusinessModel.model_validate(kb["model"])
        self.validate_filters(model, request.filters)
        scope = await self._scope(kb, request.filters)
        resolved = await self._resolutions(kb, request, model, scope)
        stripped = request.query.strip()
        direct = next(
            (
                (anchor, item)
                for anchor, item in resolved.items()
                if request.query[anchor[0] : anchor[1]] == stripped
            ),
            None,
        )
        if direct and UUID_PATTERN.fullmatch(stripped):
            anchor, item = direct
            plan = RetrievalPlan(
                intent="detail",
                mentions=[{"mention": stripped, "query_range": anchor, "entity_type": item["entity_type"]}],
            )
        else:
            try:
                raw = await self.r.language.json(
                    PLAN_SYSTEM,
                    {
                        "query": request.query,
                        "model": kb["model"],
                        "filters": request.filters.model_dump(),
                        "max_hops": request.max_hops,
                        "resolved_entities": [
                            {
                                "mention": request.query[start:end],
                                "query_range": [start, end],
                                "entity_id": item["entity_id"],
                                "entity_type": item["entity_type"],
                            }
                            for (start, end), item in resolved.items()
                        ],
                    },
                    RetrievalPlan.model_json_schema(),
                )
                plan = RetrievalPlan.model_validate(raw)
                generic = re.compile(
                    r"(?:(?:this|the|that|our|my)\s+(?:agreement|contract|document)|本(?:合同|协议|文档)|该(?:合同|协议|文档))",
                    re.IGNORECASE,
                )
                if (
                    plan.intent == "detail"
                    and not resolved
                    and plan.mentions
                    and all(
                        not mention.identity and generic.fullmatch(mention.mention.strip())
                        for mention in plan.mentions
                    )
                ):
                    plan = RetrievalPlan(
                        intent="documents", mentions=[], relation_types=[], direction="outgoing"
                    )
                self._validate_plan(plan, request.query, model, resolved, request.filters)
            except (KGError, ValueError) as exc:
                result = await self._search(
                    kb,
                    SearchRequest(
                        query=request.query,
                        kinds=["entity", "chunk"],
                        filters=request.filters,
                        limit=request.limit,
                        semantic=True,
                    ),
                    scope,
                )
                result["status"] = "partial"
                result["warnings"].append(
                    {"code": "PLAN_UNAVAILABLE", "reason": getattr(exc, "code", "INVALID_PLAN")}
                )
                result["plan_summary"] = {"intent": "search", "degraded": True, "semantic": True}
                result["unresolved_entities"] = []
                return result
        plan = plan.model_copy(update={"max_hops": min(plan.max_hops, request.max_hops)})
        if plan.intent in {"search", "documents"}:
            result = await self._search(
                kb,
                SearchRequest(
                    query=request.query,
                    kinds=["chunk"] if plan.intent == "documents" else ["entity", "chunk"],
                    filters=request.filters,
                    limit=request.limit,
                ),
                scope,
            )
            result.update(plan_summary=plan.model_dump(), unresolved_entities=[])
            return result

        # 所有锚点完成定位后再执行图查询，避免将消歧候选作为已确认端点。
        located, unresolved, candidate_coverage, candidate_warnings = [], [], [], []
        for mention in plan.mentions:
            item, candidates, entries, warnings = await self._locate(
                kb, mention, request, model, scope, resolved.get(tuple(mention.query_range))
            )
            candidate_coverage.extend(entries)
            candidate_warnings.extend(warnings)
            if item is None:
                unresolved.append(
                    {"mention": mention.mention, "query_range": mention.query_range, "candidates": candidates}
                )
            else:
                located.append(item)
        if unresolved:
            result = envelope(kb["kb_id"])
            result.update(
                status="needs_disambiguation",
                unresolved_entities=unresolved,
                candidates=[candidate for mention in unresolved for candidate in mention["candidates"]],
            )
        elif plan.intent == "detail":
            result = envelope(kb["kb_id"])
            located = list({item["entity_id"]: item for item in located}.values())
            self._check_size(located)
            result["knowledge"] = [public_item(item) for item in located]
            result["coverage"].append(
                coverage(
                    "detail",
                    "exact",
                    {
                        "entity_ids": [item["entity_id"] for item in located],
                        "filters": request.filters.model_dump(),
                    },
                    {"max_chars": self.r.settings.detail_max_chars},
                )
            )
            await self._attach_evidence(
                result, kb["kb_id"], located, request.evidence_limit, request.filters.document_ids or None
            )
            result["unresolved_entities"] = []
        else:
            if plan.intent == "paths" and len({item["entity_id"] for item in located}) < 2:
                raise KGError(
                    "INVALID_ARGUMENT", "A natural-language path query must resolve two distinct entities"
                )
            graph_request = GraphRequest(
                start_ids=[located[0]["entity_id"]]
                if plan.intent == "paths"
                else list(dict.fromkeys(item["entity_id"] for item in located)),
                end_ids=[item["entity_id"] for item in located[1:]] if plan.intent == "paths" else [],
                relation_types=plan.relation_types,
                direction=plan.direction,
                mode=plan.intent,
                filters=request.filters,
                max_hops=plan.max_hops,
                path_limit=request.limit,
                evidence_limit=request.evidence_limit,
                timeout=min(self.r.settings.graph_timeout, self.r.settings.retrieval_timeout, 15),
            )
            result = await self._graph(kb, graph_request, scope)
            result["unresolved_entities"] = []
        result["coverage"].extend(candidate_coverage)
        result["warnings"].extend(candidate_warnings)
        if result["status"] != "needs_disambiguation" and any(
            not entry["execution_complete"] for entry in candidate_coverage
        ):
            result["status"] = "partial"
        result["plan_summary"] = plan.model_dump()
        if scope is not None:
            result.update(scope="selected_documents", selected_document_ids=request.filters.document_ids)
        return result


def _valid_identity_value(value, definition):
    try:
        check_value(value, definition)
        return True
    except KGError:
        return False


def _identity_literal_present(value, query):
    """检查独立的身份字面值，避免短编码误命中较长编码的子串。"""
    literal = str(value).lower() if isinstance(value, bool) else str(value)
    if not literal:
        return False
    flags = re.IGNORECASE if isinstance(value, bool) else 0
    for match in re.finditer(re.escape(literal), query, flags):
        before = query[match.start() - 1] if match.start() else ""
        after = query[match.end()] if match.end() < len(query) else ""
        if before and before.isascii() and (before.isalnum() or before in "_-."):
            continue
        if after and after.isascii() and (after.isalnum() or after in "_-."):
            continue
        return True
    return False
