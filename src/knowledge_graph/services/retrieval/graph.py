from __future__ import annotations

import asyncio
import time
from collections import deque
from copy import deepcopy

from ...domain import valid_id
from ...domain.errors import KGError
from ...domain.model import BusinessModel
from ..contracts import GraphRequest, coverage, envelope
from .search import add_warning, public_item


class GraphMixin:
    async def query_graph(self, kb_id, request: GraphRequest):
        kb_id = valid_id(kb_id)
        request = GraphRequest.model_validate(request)
        async with self.r.gate.read(kb_id) as kb:
            try:
                async with asyncio.timeout(self.r.settings.retrieval_timeout):
                    return await self._graph(kb, request)
            except TimeoutError as exc:
                raise KGError(
                    "TIMEOUT", "Graph retrieval exceeded the total query budget", 504, retryable=True
                ) from exc

    async def _graph(self, kb, request, scope=None):
        """在跳数、候选边、路径数和时间预算内枚举图结构并附证据。"""
        r, kb_id = self.r, kb["kb_id"]
        model = BusinessModel.model_validate(kb["model"])
        self.validate_filters(model, request.filters)
        if request.mode != "paths" and request.end_ids:
            raise KGError("INVALID_ARGUMENT", "end_ids is supported only in paths mode")
        if set(request.relation_types) - model.relation_types.keys():
            raise KGError("INVALID_ARGUMENT", "Unknown relation type")
        if scope is None:
            scope = await self._scope(kb, request.filters)
        starts, ends = list(dict.fromkeys(request.start_ids)), set(request.end_ids)
        nodes, excluded = {}, []
        for object_id in dict.fromkeys(starts + request.end_ids):
            item = await r.graph.get(kb_id, "entity", valid_id(object_id))
            if item is None:
                raise KGError(
                    "NOT_FOUND",
                    "Graph endpoint does not exist in this knowledge base",
                    404,
                    entity_id=object_id,
                )
            item = deepcopy(scope["entity"].get(object_id)) if scope is not None else deepcopy(item)
            if item is not None and self._matches(item, request.filters, model, graph=True):
                nodes[object_id] = item
            else:
                excluded.append(object_id)

        result = envelope(kb_id)
        hops = 1 if request.mode == "neighbors" else request.max_hops
        frontier = deque((start, [start], [], []) for start in starts if start in nodes)
        expanded, cache, relations, paths, path_keys = {}, {}, {}, [], set()
        scanned, overflow_probes, reason, started = 0, 0, None, time.monotonic()
        effective_ends = ends & nodes.keys()
        if request.mode == "paths" and not effective_ends:
            frontier.clear()
        # 指定来源时只遍历其贡献重建的边，不能回退到整库邻接查询。
        scoped_adjacency = None
        if scope is not None:
            scoped_adjacency = {}
            adjacency_ids = {}
            for index, edge in enumerate(
                sorted(scope["relation"].values(), key=lambda value: value["relation_id"])
            ):
                # 大范围内存扫描也要让出事件循环，并遵守同一时间预算。
                if index % 256 == 0:
                    await asyncio.sleep(0)
                    if time.monotonic() - started >= request.timeout:
                        reason = "TIME_BUDGET"
                        break
                if request.relation_types and edge["relation_type"] not in request.relation_types:
                    continue
                if not self._matches(edge, request.filters, model, graph=True):
                    continue
                for current, neighbor_id, direction in (
                    (edge["source_id"], edge["target_id"], "outgoing"),
                    (edge["target_id"], edge["source_id"], "incoming"),
                ):
                    if request.direction not in {"both", direction}:
                        continue
                    neighbor = scope["entity"].get(neighbor_id)
                    current_node = scope["entity"].get(current)
                    if (
                        neighbor
                        and current_node
                        and self._matches(neighbor, request.filters, model, graph=True)
                        and self._matches(current_node, request.filters, model, graph=True)
                    ):
                        pair = (neighbor, edge)
                        bucket = scoped_adjacency.setdefault(current, [])
                        seen = adjacency_ids.setdefault(current, set())
                        if edge["relation_id"] not in seen:
                            bucket.append(pair)
                            seen.add(edge["relation_id"])

        def append_path(node_path, edge_path, directions):
            nonlocal reason
            key = (tuple(node_path), tuple(edge_path), tuple(directions))
            if key in path_keys:
                return
            if len(paths) >= request.path_limit:
                reason = "PATH_BUDGET"
                return
            path_keys.add(key)
            paths.append(
                {
                    "entity_ids": node_path,
                    "relation_ids": edge_path,
                    "directions": directions,
                    "hops": len(edge_path),
                    "path_has_conflict": any(
                        bool(relations[edge_id].get("conflicts")) for edge_id in edge_path
                    ),
                }
            )

        for start in starts:
            if request.mode == "paths" and start in effective_ends:
                append_path([start], [], [])
        while frontier and reason is None:
            current, node_path, edge_path, directions = frontier.popleft()
            if len(edge_path) >= hops:
                continue
            root = node_path[0]
            # 普通遍历可剪去更长的重复到达；路径查询须保留不同路径前缀。
            if request.mode != "paths":
                key = (root, current)
                if expanded.get(key, hops + 1) <= len(edge_path):
                    continue
                expanded[key] = len(edge_path)
            remaining_time = request.timeout - (time.monotonic() - started)
            if remaining_time <= 0:
                reason = "TIME_BUDGET"
                break
            if current not in cache:
                remaining = request.edge_limit - scanned
                # 多取一条仅用于判断截断，不将探测边纳入可返回的候选预算。
                try:
                    if scoped_adjacency is not None:
                        adjacent = scoped_adjacency.get(current, [])[: remaining + 1]
                    else:
                        async with asyncio.timeout(remaining_time):
                            adjacent = await r.graph.adjacent(
                                kb_id, current, request.direction, request.relation_types, remaining + 1
                            )
                except TimeoutError:
                    reason = "TIME_BUDGET"
                    break
                cache[current] = adjacent[:remaining]
                scanned += min(len(adjacent), remaining)
                overflow = len(adjacent) > remaining
                overflow_probes += int(overflow)
            else:
                overflow = False
            for raw_neighbor, raw_edge in cache[current]:
                neighbor, edge = deepcopy(raw_neighbor), deepcopy(raw_edge)
                if not self._matches(neighbor, request.filters, model, graph=True):
                    continue
                if not self._matches(edge, request.filters, model, graph=True):
                    continue
                neighbor_id, edge_id = neighbor["entity_id"], edge["relation_id"]
                if edge["source_id"] == current == edge["target_id"] and neighbor_id == current:
                    direction = "incoming" if request.direction == "incoming" else "outgoing"
                elif edge["source_id"] == current and edge["target_id"] == neighbor_id:
                    direction = "outgoing"
                elif edge["target_id"] == current and edge["source_id"] == neighbor_id:
                    direction = "incoming"
                else:
                    raise KGError("INCONSISTENT_STORAGE", "Graph edge does not match its endpoints", 503)
                if request.direction != "both" and request.direction != direction:
                    raise KGError(
                        "INCONSISTENT_STORAGE", "Graph edge direction does not match the query", 503
                    )
                if request.mode == "paths" and neighbor_id in node_path:
                    continue
                relations[edge_id], nodes[neighbor_id] = edge, neighbor
                new_nodes, new_edges = node_path + [neighbor_id], edge_path + [edge_id]
                new_directions = directions + [direction]
                if request.mode != "paths" or neighbor_id in effective_ends:
                    append_path(new_nodes, new_edges, new_directions)
                    if reason:
                        break
                if len(new_edges) < hops and neighbor_id not in node_path:
                    frontier.append((neighbor_id, new_nodes, new_edges, new_directions))
            if reason is None and overflow:
                reason = "EDGE_BUDGET"
            if reason is None and time.monotonic() - started >= request.timeout and frontier:
                reason = "TIME_BUDGET"

        # 路径模式只返回实际组成命中路径的对象，移除探索阶段的旁支。
        if request.mode == "paths":
            used_edges = {edge_id for path in paths for edge_id in path["relation_ids"]}
            used_nodes = {node_id for path in paths for node_id in path["entity_ids"]}
            relations = {key: value for key, value in relations.items() if key in used_edges}
            nodes = {key: value for key, value in nodes.items() if key in used_nodes}
        self._check_size(list(nodes.values()) + list(relations.values()))
        result["knowledge"] = [public_item(nodes[key]) for key in sorted(nodes)]
        result["relations"] = [public_item(relations[key]) for key in sorted(relations)]
        result["paths"] = paths
        result["coverage"].append(
            coverage(
                "graph",
                "bounded_partial" if reason else "bounded_complete",
                {
                    "start_ids": starts,
                    "end_ids": request.end_ids,
                    "direction": request.direction,
                    "mode": request.mode,
                    "max_hops": hops,
                    "filters": request.filters.model_dump(),
                    "excluded_endpoint_ids": excluded,
                    "property_filter_policy": "apply_to_declaring_types",
                    "property_filter_types": {
                        field: {
                            "entity_types": [
                                name
                                for name, definition in model.node_types.items()
                                if field in definition.properties and definition.properties[field].queryable
                            ],
                            "relation_types": [
                                name
                                for name, definition in model.relation_types.items()
                                if field in definition.properties and definition.properties[field].queryable
                            ],
                        }
                        for field in request.filters.properties
                    },
                },
                {
                    "edge_limit": request.edge_limit,
                    "edges_enumerated": scanned,
                    "overflow_probes": overflow_probes,
                    "edges_observed": scanned + overflow_probes,
                    "path_limit": request.path_limit,
                    "paths_returned": len(paths),
                    "time_seconds": request.timeout,
                    "database_query_timeout_seconds": r.settings.graph_timeout,
                    "total_query_budget_seconds": r.settings.retrieval_timeout,
                    "candidate_edge_scope": "selected_documents"
                    if scope is not None
                    else "kb_direction_relation_types",
                },
                reason,
            )
        )
        await self._attach_evidence(
            result,
            kb_id,
            list(nodes.values()) + list(relations.values()),
            request.evidence_limit,
            request.filters.document_ids or None,
        )
        if reason:
            result["status"] = "partial"
        if excluded:
            result["excluded_endpoint_ids"] = excluded
        if scope is not None:
            result.update(scope="selected_documents", selected_document_ids=request.filters.document_ids)
            if scope.get("suppressed_dependencies"):
                add_warning(
                    result,
                    "IDENTITY_DEPENDENCY_OUT_OF_SCOPE",
                    component="graph",
                    scope=request.filters.model_dump(),
                )
        return result
