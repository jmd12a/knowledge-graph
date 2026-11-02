from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError

from ...domain import canonical
from ...domain.errors import KGError
from ...services.contracts import GraphRequest, RetrievalResult, RetrieveRequest, SearchRequest
from ..http.protocol import error_result, retrieval_result


class KnowledgeMCP(FastMCP):
    async def call_tool(self, name, arguments):
        """展开 SDK 异常链，将共享服务错误映射为 MCP 工具结果。"""
        try:
            return await super().call_tool(name, arguments)
        except Exception as exc:
            error, current = exc, exc
            # 官方 SDK 将参数校验与工具异常包装为 ToolError，需沿异常链还原。
            while current is not None:
                if isinstance(current, (KGError, ValidationError, TimeoutError)):
                    error = current
                    break
                current = current.__cause__
            result = error_result(error)
            data = result.model_dump(mode="json")
            return CallToolResult(
                isError=True, structuredContent=data, content=[TextContent(type="text", text=canonical(data))]
            )


def create_mcp(retrieval):
    """将同一检索服务绑定为五个只读工具，工具说明供调用方 Agent 使用。"""
    mcp = KnowledgeMCP(
        "knowledge-graph",
        instructions=(
            "Read the current configurable graph and its source evidence. Start with describe_model, search_knowledge, "
            "or retrieve when IDs are unknown. Knowledge is source-supported, scoped, and bounded; read coverage and "
            "conflicts. Resolve needs_disambiguation using the exact mention/query_range and a selected entity ID. "
            "No tool executes arbitrary Cypher, SQL, final answers, or business calculations."
        ),
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @mcp.tool(annotations=annotations, structured_output=True)
    async def describe_model(
        kb_id: str, entity_types: list[str] | None = None, domain_ids: list[str] | None = None
    ) -> RetrievalResult:
        """Read the current model's types, identity fields, properties and relation directions.

        Use this to understand legal filters and relation names before a structured query. Optional
        type/domain selections narrow the returned definitions; model coverage is exact for that scope.
        """
        return await retrieval_result(retrieval.describe_model(kb_id, entity_types, domain_ids))

    @mcp.tool(annotations=annotations, structured_output=True)
    async def search_knowledge(kb_id: str, request: SearchRequest) -> RetrievalResult:
        """Find entity or original-document candidates when IDs are unknown.

        Results provide kind, id, identity clues and match channels. Candidate ranking is Top-K,
        never a complete graph set or proof of identity. Use get_knowledge for complete values,
        or select an entity ID for query_graph. Selected-document scope uses only those sources.
        """
        return await retrieval_result(retrieval.search_knowledge(kb_id, request))

    @mcp.tool(annotations=annotations, structured_output=True)
    async def get_knowledge(
        kb_id: str, kind: Literal["entity", "relation", "chunk"], object_id: str, evidence_limit: int = 100
    ) -> RetrievalResult:
        """Read one complete current object by an ID returned by search, retrieve or query_graph.

        Rules use kind=entity. The detail includes all current source-supported values and conflicts;
        evidence_limit bounds only evidence and never truncates complete rules. Missing IDs return
        NOT_FOUND; an oversized complete object returns RESULT_TOO_LARGE.
        """
        return await retrieval_result(retrieval.get_knowledge(kb_id, kind, object_id, evidence_limit))

    @mcp.tool(annotations=annotations, structured_output=True)
    async def query_graph(kb_id: str, request: GraphRequest) -> RetrievalResult:
        """Enumerate neighbors, bounded traversal, or simple paths between confirmed entity IDs.

        Obtain IDs through search/get/retrieve first. Direction, relation types, scope, hops, candidate
        edges, paths and time budgets govern the query. Every path identifies its edge evidence and
        conflicts. bounded_complete applies only to the reported scope; vector similarity is unused.
        """
        return await retrieval_result(retrieval.query_graph(kb_id, request))

    @mcp.tool(annotations=annotations, structured_output=True)
    async def retrieve(kb_id: str, request: RetrieveRequest) -> RetrievalResult:
        """Plan and execute a source-backed natural-language knowledge lookup.

        The service validates a constrained plan and can combine object location, graph queries and
        evidence. It never computes a business outcome or writes a final answer. For needs_disambiguation,
        repeat the same query with resolved_entities containing the exact mention, query_range and chosen
        entity_id. PLAN_UNAVAILABLE yields explicitly partial keyword search. Always inspect coverage.
        """
        return await retrieval_result(retrieval.retrieve(kb_id, request))

    return mcp
