from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ..domain import StrictModel


class Filters(StrictModel):
    entity_types: list[str] = Field(default_factory=list)
    domain_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    kinds: list[Literal["entity", "chunk"]] = Field(default_factory=lambda: ["entity"], min_length=1)
    filters: Filters = Field(default_factory=Filters)
    limit: int = Field(default=20, ge=1, le=100)
    semantic: bool = True


class GraphRequest(StrictModel):
    start_ids: list[str] = Field(min_length=1, max_length=100)
    end_ids: list[str] = Field(default_factory=list, max_length=100)
    relation_types: list[str] = Field(default_factory=list)
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    mode: Literal["neighbors", "traverse", "paths"] = "neighbors"
    filters: Filters = Field(default_factory=Filters)
    max_hops: int = Field(default=2, ge=1, le=4)
    edge_limit: int = Field(default=1000, ge=1, le=1000)
    path_limit: int = Field(default=100, ge=1, le=100)
    evidence_limit: int = Field(default=100, ge=0, le=1000)
    timeout: float = Field(default=3, gt=0, le=15)

    @model_validator(mode="after")
    def check(self):
        if self.mode == "paths" and not self.end_ids:
            raise ValueError("paths requires end_ids")
        return self


# query_range 使用 Python 字符串切片的半开区间，与原始 query 的字符位置一致。
class ResolvedEntity(StrictModel):
    mention: str = Field(min_length=1)
    query_range: tuple[int, int]
    entity_id: str

    @model_validator(mode="after")
    def check_range(self):
        from ..domain import valid_id

        valid_id(self.entity_id)
        if self.query_range[0] < 0 or self.query_range[0] >= self.query_range[1]:
            raise ValueError("Invalid query range")
        return self


class RetrieveRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    filters: Filters = Field(default_factory=Filters)
    resolved_entities: list[ResolvedEntity] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
    evidence_limit: int = Field(default=100, ge=0, le=1000)
    max_hops: int = Field(default=2, ge=1, le=4)


class MentionPlan(StrictModel):
    mention: str
    query_range: tuple[int, int]
    entity_type: str | None = None
    identity: dict[str, Any] = Field(default_factory=dict)


# schema 约束帮助模型生成计划；实际执行前仍需校验原文锚点及配置语义。
class RetrievalPlan(StrictModel):
    model_config = {
        "json_schema_extra": {
            "allOf": [
                {
                    "if": {"properties": {"intent": {"enum": ["detail", "neighbors", "traverse"]}}},
                    "then": {"required": ["mentions"], "properties": {"mentions": {"minItems": 1}}},
                },
                {
                    "if": {"properties": {"intent": {"const": "paths"}}},
                    "then": {"required": ["mentions"], "properties": {"mentions": {"minItems": 2}}},
                },
                {
                    "if": {"properties": {"intent": {"enum": ["search", "documents"]}}},
                    "then": {
                        "properties": {"relation_types": {"maxItems": 0}, "direction": {"const": "outgoing"}}
                    },
                },
            ]
        }
    }

    intent: Literal["search", "detail", "neighbors", "traverse", "paths", "documents"]
    mentions: list[MentionPlan] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)
    direction: Literal["outgoing", "incoming", "both"] = "outgoing"
    max_hops: int = Field(default=2, ge=1, le=4)


def envelope(kb_id: str):
    """创建 HTTP 与 MCP 共用的检索结果容器。"""
    from ..domain import new_id

    return {
        "kb_id": kb_id,
        "request_id": new_id(),
        "status": "ok",
        "knowledge": [],
        "relations": [],
        "paths": [],
        "evidence": [],
        "candidates": [],
        "conflicts": [],
        "coverage": [],
        "warnings": [],
    }


def coverage(
    component: str,
    mode: str,
    scope: dict | None = None,
    budget: dict | None = None,
    reason: str | None = None,
):
    """记录组件范围与预算；执行完成仅针对所报告的范围。"""
    return {
        "component": component,
        "mode": mode,
        "scope": scope or {},
        "budget": budget or {},
        "execution_complete": reason is None,
        "truncated": reason is not None,
        "reason": reason,
    }


class RetrievalResult(StrictModel):
    kb_id: str
    request_id: str
    status: Literal["ok", "partial", "needs_disambiguation"]
    knowledge: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    paths: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    coverage: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    model: dict[str, Any] | None = None
    model_revision: str | None = None
    item: dict[str, Any] | None = None
    scope: str | None = None
    selected_document_ids: list[str] = Field(default_factory=list)
    declarations: list[dict[str, Any]] = Field(default_factory=list)
    plan_summary: dict[str, Any] | None = None
    unresolved_entities: list[dict[str, Any]] = Field(default_factory=list)
    excluded_endpoint_ids: list[str] = Field(default_factory=list)


class ErrorResult(StrictModel):
    request_id: str
    code: str
    message: str
    retryable: bool
    details: dict[str, Any]
