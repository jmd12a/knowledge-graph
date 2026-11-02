from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy

from ...domain import canonical, valid_id
from ...domain.errors import KGError
from ...domain.source import validate_stored_evidence
from ..contracts import coverage, envelope
from .search import public_item


def _evidence_key(reference):
    try:
        return canonical([reference["document_id"], reference["chunk_id"], reference["chunk_range"]])
    except (KeyError, TypeError, ValueError) as exc:
        raise KGError(
            "INCONSISTENT_STORAGE", "Knowledge evidence has an invalid source reference", 503
        ) from exc


def _resolved_reference_pairs(claim):
    """验证引用与目标两端均属于本贡献的事实证据。"""
    if not claim["field_path"].startswith("/atomic_groups/"):
        return []
    value = claim.get("value")
    if not isinstance(value, dict):
        raise KGError("INCONSISTENT_STORAGE", "Knowledge declaration has an invalid structure", 503)
    pairs = value.get("resolved_references", [])
    if not isinstance(pairs, list):
        raise KGError("INCONSISTENT_STORAGE", "Knowledge reference pairs have an invalid structure", 503)
    if not pairs:
        return []
    try:
        flat_references = {canonical(reference) for reference in claim["evidence"]}
        for pair in pairs:
            if not isinstance(pair, dict):
                raise ValueError("Invalid reference pair")
            for side in ("reference", "target"):
                references = pair.get(side)
                if not isinstance(references, list) or not references:
                    raise ValueError("A reference pair requires both endpoints")
                for reference in references:
                    if (
                        not isinstance(reference, dict)
                        or reference.get("role") != "fact"
                        or reference.get("document_id") != claim["document_id"]
                        or canonical(reference) not in flat_references
                    ):
                        raise ValueError("A reference endpoint is not fact evidence of its contribution")
    except (KeyError, TypeError, ValueError) as exc:
        raise KGError(
            "INCONSISTENT_STORAGE", "Knowledge reference pairs do not match their source evidence", 503
        ) from exc
    return pairs


def _declaration_evidence(claim, returned_indices):
    """将持久化引用映射为返回证据下标，保留预算外及缺失映射的计数。"""
    result = {
        key: deepcopy(claim[key])
        for key in ("contribution_id", "target_kind", "target_id", "field_path", "document_id")
    }
    wanted = {_evidence_key(reference) for reference in claim["evidence"]}
    result["evidence_indices"] = sorted(returned_indices[key] for key in wanted if key in returned_indices)
    result["evidence_complete"] = wanted.issubset(returned_indices)
    pairs = []
    for stored_pair in _resolved_reference_pairs(claim):
        pair = {}
        for side in ("reference", "target"):
            keys = list(dict.fromkeys(_evidence_key(reference) for reference in stored_pair[side]))
            indices = [returned_indices[key] for key in keys if key in returned_indices]
            pair[side + "_evidence_indices"] = indices
            pair["missing_" + side + "_count"] = len(keys) - len(indices)
        pair["evidence_complete"] = not (pair["missing_reference_count"] or pair["missing_target_count"])
        pairs.append(pair)
    recorded_count = len(pairs)
    if claim["field_path"].startswith("/atomic_groups/"):
        check = claim["value"].get("completeness_check", {})
        if not isinstance(check, dict):
            raise KGError(
                "INCONSISTENT_STORAGE", "Knowledge completeness metadata has an invalid structure", 503
            )
        recorded_count = check.get("resolved_reference_count", recorded_count)
        if isinstance(recorded_count, bool) or not isinstance(recorded_count, int) or recorded_count < 0:
            raise KGError("INCONSISTENT_STORAGE", "Knowledge reference count is invalid", 503)
    unavailable = max(recorded_count - len(pairs), 0)
    result["resolved_references"] = pairs
    result["resolved_references_unavailable_count"] = unavailable
    result["resolved_references_complete"] = not unavailable and all(
        pair["evidence_complete"] for pair in pairs
    )
    return result


class EvidenceMixin:
    def _check_size(self, items):
        """完整知识对象超限时拒绝返回，避免截短规则改变含义。"""
        for item in items:
            if len(canonical(public_item(item))) > self.r.settings.detail_max_chars:
                raise KGError(
                    "RESULT_TOO_LARGE",
                    "A complete knowledge object exceeds the response budget",
                    422,
                    max_chars=self.r.settings.detail_max_chars,
                    suggestion="Request fewer objects; complete rule statements cannot be shortened",
                )

    async def _evidence(self, kb_id, items, limit, document_ids=None):
        """按来源坐标去重并校验证据，返回证据、声明映射、截断标记和总数。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000:
            raise KGError("INVALID_ARGUMENT", "Evidence limit must be between 0 and 1000")
        wanted = {claim_id for item in items for claim_id in item.get("contribution_ids", [])}
        if not wanted:
            return [], [], False, 0
        claims = await self.r.metadata.contributions(kb_id, document_ids, contribution_ids=sorted(wanted))
        if {claim["contribution_id"] for claim in claims} != wanted:
            raise KGError(
                "INCONSISTENT_STORAGE",
                "Knowledge evidence is not consistently available",
                503,
                retryable=True,
            )
        references = {}
        for claim in claims:
            for reference in claim["evidence"]:
                if reference["document_id"] != claim["document_id"]:
                    raise KGError("INCONSISTENT_STORAGE", "Knowledge evidence has an invalid source", 503)
                key = _evidence_key(reference)
                group = references.get(key)
                if group is None:
                    group = {
                        "preferred": deepcopy(reference),
                        "references": [],
                        "contribution_ids": [],
                        "fact_ids": [],
                        "field_paths": [],
                    }
                    references[key] = group
                # 同坐标的引用仍须逐一校验，不能让上下文引用遮蔽事实证据。
                group["references"].append(reference)
                if group["preferred"].get("role") == "context" and reference.get("role") != "context":
                    group["preferred"] = deepcopy(reference)
                group["contribution_ids"].append(claim["contribution_id"])
                group["fact_ids"].append(claim["target_id"])
                group["field_paths"].append(claim["field_path"])

        # 先对全部贡献建立引用集合，再按去重后的引用数施加证据预算。
        selected_keys = sorted(references)[:limit]
        chunks = {
            point["id"]: point["payload"]
            for point in await self.r.vectors.read(
                kb_id, "chunks", sorted({references[key]["preferred"]["chunk_id"] for key in selected_keys})
            )
        }
        selected = []
        for key in selected_keys:
            group = references[key]
            item = deepcopy(group["preferred"])
            chunk = chunks.get(item["chunk_id"])
            if not chunk or chunk.get("kb_id") != kb_id or chunk.get("document_id") != item["document_id"]:
                raise KGError(
                    "INCONSISTENT_STORAGE",
                    "Knowledge evidence is not consistently available",
                    503,
                    retryable=True,
                )
            try:
                for reference in group["references"]:
                    text = validate_stored_evidence(reference, chunk)
            except KGError as exc:
                raise KGError(
                    "INCONSISTENT_STORAGE",
                    "Knowledge evidence no longer matches its complete source mapping",
                    503,
                ) from exc
            item.update(text=text, locator=deepcopy(chunk["locator"]))
            for field in ("contribution_ids", "fact_ids", "field_paths"):
                item[field] = sorted(set(group[field]))
            roles = {reference.get("role", "fact") for reference in group["references"]}
            if len(roles) > 1:
                item["evidence_roles"] = sorted(roles)
            selected.append(item)
        returned_indices = {_evidence_key(reference): index for index, reference in enumerate(selected)}
        declarations = [
            _declaration_evidence(claim, returned_indices)
            for claim in sorted(claims, key=lambda value: value["contribution_id"])
        ]
        return selected, declarations, len(references) > limit, len(references)

    async def _attach_evidence(self, result, kb_id, items, limit, document_ids=None):
        """将统一证据下标关联到声明、关系与路径，并报告证据覆盖。"""
        evidence, declarations, truncated, total = await self._evidence(kb_id, items, limit, document_ids)
        result.update(evidence=evidence, declarations=declarations)
        unavailable = [
            declaration
            for declaration in declarations
            if declaration["resolved_references_unavailable_count"]
        ]
        reason = "EVIDENCE_BUDGET" if truncated else "REFERENCE_MAPPING_UNAVAILABLE" if unavailable else None
        result["coverage"].append(
            coverage(
                "evidence",
                "bounded_partial" if reason else "exact",
                {
                    "document_ids": document_ids or [],
                    "fact_ids": sorted({item.get("entity_id", item.get("relation_id")) for item in items}),
                },
                {
                    "limit": limit,
                    "available_references": total,
                    "returned_references": len(evidence),
                    "unavailable_resolved_references": sum(
                        declaration["resolved_references_unavailable_count"] for declaration in unavailable
                    ),
                },
                reason,
            )
        )
        if unavailable:
            result["warnings"].append(
                {
                    "code": "REFERENCE_MAPPING_UNAVAILABLE",
                    "contribution_ids": [declaration["contribution_id"] for declaration in unavailable],
                }
            )
        if reason and result["status"] != "needs_disambiguation":
            result["status"] = "partial"
        by_fact = defaultdict(list)
        for index, reference in enumerate(evidence):
            for fact_id in reference["fact_ids"]:
                by_fact[fact_id].append(index)
        for relation in result["relations"]:
            relation["evidence_indices"] = by_fact[relation["relation_id"]]
        for path in result["paths"]:
            path["edge_evidence"] = [
                {"relation_id": relation_id, "evidence_indices": by_fact[relation_id]}
                for relation_id in path["relation_ids"]
            ]
        result["conflicts"] = [
            dict(deepcopy(conflict), fact_id=item.get("entity_id", item.get("relation_id")))
            for item in items
            for conflict in item.get("conflicts", [])
        ]

    async def get_knowledge(self, kb_id, kind, object_id, evidence_limit=100):
        kb_id, object_id = valid_id(kb_id), valid_id(object_id)
        if kind not in {"entity", "relation", "chunk"}:
            raise KGError("INVALID_ARGUMENT", "Unknown object kind")
        if (
            isinstance(evidence_limit, bool)
            or not isinstance(evidence_limit, int)
            or not 0 <= evidence_limit <= 1000
        ):
            raise KGError("INVALID_ARGUMENT", "Evidence limit must be between 0 and 1000")
        async with self.r.gate.read(kb_id):
            try:
                async with asyncio.timeout(self.r.settings.retrieval_timeout):
                    result = envelope(kb_id)
                    if kind == "chunk":
                        points = await self.r.vectors.read(kb_id, "chunks", [object_id])
                        if not points:
                            raise KGError("NOT_FOUND", "Chunk does not exist", 404)
                        item = {
                            key: deepcopy(value)
                            for key, value in points[0]["payload"].items()
                            if key != "embedding_profile"
                        }
                        item.update(kind="chunk", origin="document", id=object_id, chunk_id=object_id)
                        self._check_size([item])
                    else:
                        item = await self.r.graph.get(kb_id, kind, object_id)
                        if not item:
                            raise KGError("NOT_FOUND", "Knowledge object does not exist", 404)
                        self._check_size([item])
                        await self._attach_evidence(result, kb_id, [item], evidence_limit)
                    result["item"] = public_item(item)
                    result["coverage"].append(
                        coverage(
                            "detail",
                            "exact",
                            {"kind": kind, "id": object_id},
                            {"max_chars": self.r.settings.detail_max_chars},
                        )
                    )
                    return result
            except TimeoutError as exc:
                raise KGError(
                    "TIMEOUT", "Detail retrieval exceeded the total query budget", 504, retryable=True
                ) from exc
