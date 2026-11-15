"""用两次业务导入验证构建复查能复用已发布端点，并拒绝歧义关系。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from copy import deepcopy
from functools import partial
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import yaml

from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import KGError, canonical
from knowledge_graph.domain.model import BusinessModel
from knowledge_graph.domain.source import effective_contributions
from knowledge_graph.services.construction.alignment import align
from knowledge_graph.services.construction.extraction import extract_text
from knowledge_graph.services.construction.extraction_identity_review import (
    search_published_mentions,
    search_published_vectors,
)
from knowledge_graph.services.construction.merge import aggregate
from knowledge_graph.settings import LanguageProfile, Settings

from .construction_metrics import coverage, precision_recall
from .evidence import ROOT, write_json
from .quality_construction_loop import _MeasuredLanguage
from .quality_labeled import ExistingGraph

_CORPUS = ROOT / "evals" / "corpus" / "construction-loop"
_KB = "incremental-construction"
_AMBIGUOUS_CALL = "PGW called Sandbox Clearing Service (app_id clearing-test)"


def _citation(views, quote):
    for view in views:
        if any(quote in part["text"] for part in view["visible_parts"]):
            return [{"unit_key": view["unit_key"], "quote": quote}]
    raise AssertionError(f"Fixture quote is outside the supplied source: {quote}")


def _service(views, app_id, name=None, alias=None, *, quote=None):
    properties = {"app_id": app_id}
    if name:
        properties["name"] = name
    if alias:
        properties["aliases"] = alias
    return {
        "ref": app_id + ("/" + alias if alias else ""),
        "type": "Service",
        "properties": properties,
        "evidence": {key: _citation(views, quote or value) for key, value in properties.items()},
    }


def _mention(views, name, quote):
    return {
        "ref": name,
        "type": "Service",
        "properties": {},
        "evidence": {},
        "mention": name,
        "mention_evidence": _citation(views, quote),
    }


def _call(views, source, target, quote):
    return {
        "type": "CALLS",
        "source_ref": source,
        "target_ref": target,
        "evidence": _citation(views, quote),
    }


class _PublishedGraph(ExistingGraph):
    async def by_identity(self, kb_id, kind, keys):
        if kind == "relation":
            return [row for row in self.relations if canonical(row["identity"]) in keys]
        return await super().by_identity(kb_id, kind, keys)

    async def by_id(self, kb_id, kind, ids):
        assert kb_id == _KB and kind == "entity"
        return [item for item in self.entities if item["entity_id"] in ids]

    async def candidates(self, kb_id, text, scan_limit, entity_types=None, document_ids=None):
        assert kb_id == _KB and entity_types == ["Service"] and document_ids is None
        exact = [
            item
            for item in self.entities
            if text in {item["canonical_name"], *item["aliases"], *item["identity"]["fields"].values()}
        ]
        related = [item for item in self.entities if text.casefold() in item["search_text"].casefold()]
        return exact[:scan_limit], related[:scan_limit]


class _Language:
    profile = LanguageProfile(model="offline-incremental-test", max_input_chars=30_000)

    def __init__(self, *, registry=False, call="", forged_candidate=False):
        self.registry = registry
        self.call = call
        self.forged_candidate = forged_candidate
        self.primary = 0
        self.actions = []

    def json_request_chars(self, *_):
        return 100

    async def json(self, _system, request, _schema):
        if "task" in request:
            return self._identity(request)
        if "review_units" in request:
            return {"entities": [], "relations": []}
        assert "allowed_pairs" not in request, "Each fixture must fit one extraction window"
        self.primary += 1
        views = request["rule_units"]
        if self.registry:
            return self._registry(views)
        return {
            "entities": [
                _mention(views, "PGW-East", self.call),
                _service(views, "clearing-prod", quote=self.call),
                _mention(views, "PGW", _AMBIGUOUS_CALL),
                _service(views, "clearing-test", quote=_AMBIGUOUS_CALL),
            ],
            "relations": [
                _call(views, "PGW-East", "clearing-prod", self.call),
                _call(views, "PGW", "clearing-test", _AMBIGUOUS_CALL),
            ],
        }

    @staticmethod
    def _registry(views):
        entities = []
        for region in ("East", "West"):
            app_id = "pay-" + region.lower()
            alias = "PGW-" + region
            quote = (
                f"The {region} deployment of Payment Gateway is registered with app_id {app_id}. "
                f"Its region-specific operational alias is {alias}."
            )
            entities.append(_service(views, app_id, "Payment Gateway", alias, quote=quote))
            short_label = "On-call staff also use the short label PGW in regional channels."
            entities.append(_service(views, app_id, alias="PGW", quote=quote + " " + short_label))
        entities.extend(
            [
                _service(views, "clearing-prod", "Clearing Service"),
                _service(views, "clearing-test", "Sandbox Clearing Service"),
            ]
        )
        return {"entities": entities, "relations": []}

    def _identity(self, request):
        task = request["task"]
        mention = task["mention"]
        assert task["kind"] == "identity" and task["identity_fields"] == ["app_id"]
        histories = request["search_history"]
        if not histories:
            self.actions.append((mention, "lookup"))
            assert request["tools"]["lookup_published_entities"]["modes"] == ["keyword"]
            return {"action": "lookup_published_entities"}
        [lookup] = histories
        candidates = {item["identity"]["fields"]["app_id"]: item for item in lookup["entities"]}
        if mention == "PGW-East":
            assert set(candidates) == {"pay-east"}
            self.actions.append((mention, "use_existing"))
            return {"action": "use_existing", "candidate_id": candidates["pay-east"]["entity_id"]}
        assert mention == "PGW" and set(candidates) == {"pay-east", "pay-west"}
        if self.forged_candidate:
            return {"action": "use_existing", "candidate_id": "not-returned-by-the-tool"}
        self.actions.append((mention, "stop"))
        return {"action": "stop"}


def _document(source):
    return {
        "kb_id": _KB,
        "document_id": source,
        "raw_path": str(_CORPUS / source),
        "media_type": "md",
        "source": {"domain_ids": []},
    }


async def _import(
    model,
    doc,
    parsed,
    language,
    graph,
    *,
    limit=0,
    state=None,
    vector_search=None,
    mention_search=None,
    window_chars=2000,
):
    checkpoint, draft = ({}, {}) if state is None else state

    async def persist():
        return None

    extracted = await extract_text(
        language,
        parsed,
        model,
        checkpoint,
        persist,
        scope_policy="structure",
        structure_window_chars=window_chars,
        loop_calls=limit,
        review_document=doc,
        published_graph=graph,
        published_vector_search=vector_search,
        published_mention_search=mention_search,
    )
    rows, issues = await align(
        doc,
        extracted,
        {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]},
        model,
        draft,
        "import/" + doc["document_id"],
        graph.entities if graph is not None else [],
        graph.relations if graph is not None else [],
        parsed["rule_units"],
        graph=graph,
    )
    return rows, extracted["issues"] + issues


def _snapshot(model, rows, previous=None):
    active, suppressed = effective_contributions(rows)
    assert not suppressed, suppressed
    old_entities = previous.entities if previous else None
    old_relations = previous.relations if previous else None
    return _PublishedGraph(*aggregate(_KB, active, model, old_entities, old_relations))


def _ids(snapshot):
    return {
        item["identity"]["fields"]["app_id"]: item["entity_id"]
        for item in snapshot.entities
        if item["entity_type"] == "Service"
    }


def _assert_ambiguous_rejected(issues):
    assert any(
        issue["reason_code"] == "IDENTITY_EVIDENCE_MISSING"
        and issue.get("candidate", {}).get("mention") == "PGW"
        for issue in issues
    ), issues


def _inputs():
    labels = json.loads((_CORPUS / "annotations.json").read_text(encoding="utf-8"))
    model = BusinessModel.model_validate(yaml.safe_load((_CORPUS / labels["business_model"]).read_text()))
    parser = Parser(SimpleNamespace(chunk_target=290, chunk_max=440, chunk_overlap=0))
    documents = [_document(stage["source"]) for stage in labels["imports"]]
    return model, labels["imports"], documents, [parser.parse(doc, model) for doc in documents]


async def _check_remote_endpoints(model):
    call = "Night Counter calls Parcel Desk to quote delivery."
    declarations = (
        "Night Counter is an explicit alias for the service registered as checkout-live.",
        "Parcel Desk is an explicit alias for the service registered as delivery-live.",
    )
    text = "\n\n".join((call, "The order awaits a warehouse decision.", *declarations))
    doc = _document("cross-window")
    parser = Parser(SimpleNamespace(chunk_target=85, chunk_max=100, chunk_overlap=0))
    parsed = {"chunks": parser._chunk_text(text, {"format": "txt"}, doc["document_id"], [], overlap=False)}

    class Language(_Language):
        def __init__(self, *, forged=False):
            super().__init__()
            self.forged = forged
            self.link_requests = []

        async def json(self, system, request, schema):
            views = request["rule_units"]
            if "allowed_pairs" in request:
                self.link_requests.append((system, deepcopy(request), schema))
                directory = {
                    item["identity"]["fields"]["app_id"]: item["stable_id"]
                    for item in request["endpoint_catalog"]
                }
                context = request["endpoint_identity_evidence"]
                assert declarations[0] in context[directory["checkout-live"]]
                assert declarations[1] in context[directory["delivery-live"]]
                assert all(call in part["text"] for view in views for part in view["visible_parts"])
                quote = "Night Counter calls" if self.forged else call
                return {
                    "entities": [],
                    "relations": [
                        _call(views, directory["checkout-live"], directory["delivery-live"], quote)
                    ],
                }
            if "review_units" in request:
                return {"entities": [], "relations": []}
            entities = [
                _service(views, app_id, alias=alias, quote=quote)
                for app_id, alias, quote in zip(
                    ("checkout-live", "delivery-live"),
                    ("Night Counter", "Parcel Desk"),
                    declarations,
                    strict=True,
                )
                if any(quote in part["text"] for view in views for part in view["visible_parts"])
            ]
            return {"entities": entities, "relations": []}

    async def persist():
        return None

    link_requests = []
    for limit in (0, 8):
        checkpoint = {}
        language = Language()
        for _ in range(2):
            extracted = await extract_text(
                language,
                parsed,
                model,
                checkpoint,
                persist,
                scope_policy="structure",
                structure_window_chars=100,
                loop_calls=limit,
            )
            assert not extracted["issues"], extracted["issues"]
            [edge] = extracted["relations"]
            assert edge["source_ref"].endswith("/checkout-live/Night Counter")
            assert edge["target_ref"].endswith("/delivery-live/Parcel Desk")
            assert [item["quote"] for item in edge["evidence"]] == [call]

        # 第二次从检查点恢复，基础关系请求不再发送。
        [request] = language.link_requests
        link_requests.append(request)
        if limit == 0:
            [key] = (key for key in checkpoint if key.startswith("link/"))
            checkpoint[key.replace("remote-review/", "remote/")] = checkpoint.pop(key) | {
                "request_key": "legacy-without-endpoint-evidence",
            }
            resumed = await extract_text(
                language,
                parsed,
                model,
                checkpoint,
                persist,
                scope_policy="structure",
                structure_window_chars=100,
            )
            assert resumed["relations"] == extracted["relations"]
            assert language.link_requests == [request, request]
    assert link_requests[0] == link_requests[1]

    rejected = await extract_text(
        Language(forged=True),
        parsed,
        model,
        {},
        persist,
        scope_policy="structure",
        structure_window_chars=100,
    )
    assert not rejected["relations"]
    assert [issue["reason_code"] for issue in rejected["issues"]] == ["EVIDENCE_INVALID"]


async def _check_semantic_identity(model, published, seed_rows):
    calls = (
        "East Desk calls Production Clearing (app_id clearing-prod) for captured payments.",
        "Returns Desk (app_id returns-new) calls Production Clearing (app_id clearing-prod) for refunds.",
    )
    doc = _document("semantic-identity")
    parser = Parser(SimpleNamespace(chunk_target=290, chunk_max=440, chunk_overlap=0))
    parsed = {
        "chunks": parser._chunk_text(
            " ".join(calls), {"format": "txt"}, doc["document_id"], [], overlap=False
        )
    }
    ids = _ids(published)
    vector_queries = []
    text = "The East Payment Gateway processes captured payments for Production Clearing."

    async def exists(kb_id, kind):
        assert kb_id == _KB and kind in {"objects", "chunk_vectors"}
        return True

    async def embed(queries, profile):
        [query] = queries
        assert profile == {"test_profile": True}
        vector_queries.append(query)
        return [[0.6, 0.8]]

    async def search(kb_id, kind, vector, limit, filters, *, point_ids=None):
        assert kb_id == _KB and vector == [0.6, 0.8] and filters.entity_types == ["Service"]
        if kind == "objects":
            assert limit == 6
            return [
                {"id": ids["pay-east"], "score": 0.9},
                {"id": ids["pay-west"], "score": 0.8},
            ]
        assert kind == "chunk_vectors" and limit == 3 and point_ids is None
        return [
            {"id": key, "score": 0.9, "payload": {"representative_chunk_id": key}}
            for key in ("withdrawn", "registry")
        ]

    async def members(kb_id, *, vector_ids, active_only):
        assert kb_id == _KB and active_only and set(vector_ids) == {"registry", "withdrawn"}
        return [{"vector_id": "registry", "chunk_id": "registry"}]

    async def read(kb_id, kind, ids):
        assert kb_id == _KB and kind == "chunks" and ids == ["registry"]
        return [{"id": "registry", "payload": {"document_id": "registry.md", "text": text}}]

    resources = SimpleNamespace(
        graph=published,
        vectors=SimpleNamespace(exists=exists, search=search, read=read),
        metadata=SimpleNamespace(vector_members=members),
        language=SimpleNamespace(embed=embed),
    )
    vector_search = partial(search_published_vectors, resources, profile={"test_profile": True})

    class Language(_Language):
        async def json(self, system, request, schema):
            if "task" in request:
                mention = request["task"]["mention"]
                lookups = request.get("search_history", [])
                if not lookups and mention == "East Desk":
                    return {"action": "lookup_published_entities", "mode": "keyword"}
                if not lookups or not lookups[-1]["entities"]:
                    return {
                        "action": "lookup_published_entities",
                        "mode": "semantic",
                        "query": "East captured payments"
                        if mention == "East Desk"
                        else "Returns Desk refunds",
                    }
                assert request["tools"]["lookup_published_entities"]["modes"] == ["keyword", "semantic"]
                assert lookups[-1]["source_chunks"][0]["text"] == text
                assert {item["entity_id"] for item in lookups[-1]["entities"]} == {
                    ids["pay-east"],
                    ids["pay-west"],
                }
                if mention == "East Desk":
                    assert "pay-east" not in calls[0]
                    return {"action": "use_existing", "candidate_id": ids["pay-east"]}
                return {
                    "action": "use_new",
                    "identity_fields": {"app_id": "returns-new"},
                    "field_evidence": {"app_id": _citation(request["source_rule_units"], "returns-new")},
                }
            if "review_units" in request:
                return {"entities": [], "relations": []}
            views = request["rule_units"]
            return {
                "entities": [
                    _mention(views, "East Desk", calls[0]),
                    _mention(views, "Returns Desk", calls[1]),
                    _service(views, "clearing-prod", quote="clearing-prod"),
                ],
                "relations": [
                    _call(views, "East Desk", "clearing-prod", calls[0]),
                    _call(views, "Returns Desk", "clearing-prod", calls[1]),
                ],
            }

    state = ({}, {})
    language = Language()
    for _ in range(2):
        rows, issues = await _import(
            model, doc, parsed, language, published, limit=10, state=state, vector_search=vector_search
        )
        assert not issues, issues
        updated = _snapshot(model, seed_rows + rows)
        assert _ids(updated) == ids | {"returns-new": _ids(updated)["returns-new"]}
        sources = {
            edge["source_id"] for edge in updated.relations if edge["target_id"] == ids["clearing-prod"]
        }
        assert sources == {ids["pay-east"], _ids(updated)["returns-new"]}
        assert all(ref["document_id"] == doc["document_id"] for row in rows for ref in row["evidence"])
    assert vector_queries == ["East captured payments", "Returns Desk refunds"]


async def _check_new_without_candidates(model):
    doc = _document("new-service")
    parser = Parser(SimpleNamespace(chunk_target=290, chunk_max=440, chunk_overlap=0))
    app_id = "night-new"
    call = "Night Returns (app_id night-new) calls Clearing (app_id clearing-prod)."
    standalone = "The new Night Returns service is registered with app_id night-new."

    class UnavailableGraph(_PublishedGraph):
        async def candidates(self, *args, **kwargs):
            raise KGError("UPSTREAM_UNAVAILABLE", "Simulated search outage", 503, retryable=True)

    for graph, text, error in (
        (_PublishedGraph([], []), call, None),
        (None, standalone, None),
        (UnavailableGraph([], []), standalone, "UPSTREAM_UNAVAILABLE"),
    ):
        parsed = {
            "chunks": parser._chunk_text(text, {"format": "txt"}, doc["document_id"], [], overlap=False)
        }
        calls = []

        class Language(_Language):
            async def json(self, system, request, schema):
                if "task" in request:
                    lookups = request["search_history"]
                    calls.append(bool(lookups))
                    if graph is not None and not lookups:
                        return {"action": "lookup_published_entities"}
                    if graph is None:
                        assert request["tools"] == {}
                    else:
                        assert lookups[-1]["entities"] == [] and lookups[-1].get("error_code") == error
                    if text == standalone:
                        assert request["task"]["missing_identity_fields"] == ["app_id"]
                    return {
                        "action": "use_new",
                        "identity_fields": {"app_id": app_id},
                        "field_evidence": {"app_id": _citation(request["source_rule_units"], app_id)},
                    }
                if "review_units" in request:
                    return {"entities": [], "relations": []}
                views = request["rule_units"]
                new = _mention(views, "Night Returns", text)
                if text == standalone:
                    new["properties"]["app_id"] = None
                    return {"entities": [new], "relations": []}
                return {
                    "entities": [new, _service(views, "clearing-prod")],
                    "relations": [_call(views, "Night Returns", "clearing-prod", call)],
                }

        rows, issues = await _import(model, doc, parsed, Language(), graph, limit=6)
        graph_after = _snapshot(model, rows)
        assert app_id in _ids(graph_after) and len(graph_after.relations) == int(text == call)
        assert calls == ([False] if graph is None else [False, True])
        assert [issue["reason_code"] for issue in issues] == ([error] if error else [])


async def _check_conflict_and_learned_mention(model):
    data = model.model_dump()
    service = data["node_types"]["Service"]
    service["identity"]["fields"] = ["tenant", "app_id"]
    service["properties"]["tenant"] = {"type": "string", "required": True}
    model = BusinessModel.model_validate(data)
    parser = Parser(SimpleNamespace(chunk_target=290, chunk_max=440, chunk_overlap=0))
    registry, first, second = map(_document, ("tenant-registry", "tenant-first", "tenant-second"))
    alias = "Night Returns"
    seed_text = "EAST pay-east Payment Gateway; WEST pay-west Payment Gateway; WEST clearing-prod Clearing."
    first_text = "The WEST tenant's Night Returns calls Clearing (WEST clearing-prod)."
    second_text = "Night Returns calls Clearing (WEST clearing-prod)."

    class Language(_Language):
        def __init__(self, stage, ids=None):
            super().__init__()
            self.stage = stage
            self.ids = ids
            self.feedback = False

        async def json(self, system, request, schema):
            if "task" in request:
                history = request["search_history"]
                if not history:
                    query = "Payment Gateway" if self.stage == 1 else alias
                    return {"action": "lookup_published_entities", "query": query}
                if self.stage == 1:
                    assert {entry["entity_id"] for entry in history[0]["entities"]} == {
                        self.ids["pay-east"],
                        self.ids["pay-west"],
                    }
                    if not request["decision_feedback"]:
                        return {"action": "use_existing", "candidate_id": self.ids["pay-east"]}
                    [event] = request["decision_feedback"]
                    assert event["candidate_id"] == self.ids["pay-east"]
                    assert any(
                        entry["field"] == "tenant"
                        and entry["current_value"] == "WEST"
                        and entry["existing_value"] == "EAST"
                        for entry in event["conflicts"]
                    )
                    self.feedback = True
                else:
                    [candidate] = history[0]["entities"]
                    assert candidate["entity_id"] == self.ids["pay-west"]
                    hint = candidate["matched_mention"]
                    assert hint["mention"] == alias and hint["document_id"] == first["document_id"]
                    assert alias in hint["quote"]
                return {"action": "use_existing", "candidate_id": self.ids["pay-west"]}
            if "review_units" in request:
                return {"entities": [], "relations": []}
            views = request["rule_units"]

            def service(app_id, name, tenant, quote):
                candidate = _service(views, app_id, name, quote=quote)
                candidate["properties"]["tenant"] = tenant
                candidate["evidence"]["tenant"] = _citation(views, quote)
                return candidate

            if self.stage == 0:
                return {
                    "entities": [
                        service("pay-east", "Payment Gateway", "EAST", "EAST pay-east Payment Gateway"),
                        service("pay-west", "Payment Gateway", "WEST", "WEST pay-west Payment Gateway"),
                        service("clearing-prod", "Clearing", "WEST", "WEST clearing-prod Clearing"),
                    ],
                    "relations": [],
                }
            quote = first_text if self.stage == 1 else second_text
            mention = _mention(views, alias, quote)
            if self.stage == 1:
                mention["properties"]["tenant"] = "WEST"
                mention["evidence"]["tenant"] = _citation(views, "WEST tenant's Night Returns")
            return {
                "entities": [mention, service("clearing-prod", None, "WEST", "WEST clearing-prod")],
                "relations": [_call(views, alias, "clearing-prod", quote)],
            }

    def parsed(doc, text):
        return {"chunks": parser._chunk_text(text, {"format": "txt"}, doc["document_id"], [], overlap=False)}

    seeds, issues = await _import(
        model, registry, parsed(registry, seed_text), Language(0), _PublishedGraph([], [])
    )
    graph = _snapshot(model, seeds)
    ids = _ids(graph)
    assert not issues and len(ids) == 3
    language = Language(1, ids)
    rows, issues = await _import(model, first, parsed(first, first_text), language, graph, limit=8)
    assert language.feedback
    assert [issue["reason_code"] for issue in issues] == ["IDENTITY_CONFLICT"]
    projected = _snapshot(model, seeds + rows)
    [edge] = projected.relations
    assert edge["source_id"] == ids["pay-west"]
    assert alias not in next(
        item["aliases"] for item in projected.entities if item["entity_id"] == ids["pay-west"]
    )

    async def mentions(kb_id, query, entity_type, limit):
        assert kb_id == _KB and query == alias and entity_type == "Service"
        return [row for row in rows if row["field_path"] == "$mention"][:limit]

    metadata = SimpleNamespace(published_mention_contributions=mentions)
    mention_search = partial(search_published_mentions, SimpleNamespace(metadata=metadata, graph=projected))
    followup, issues = await _import(
        model,
        second,
        parsed(second, second_text),
        Language(2, ids),
        projected,
        limit=8,
        mention_search=mention_search,
    )
    assert not issues
    [combined] = _snapshot(model, seeds + rows + followup).relations
    assert combined["source_id"] == ids["pay-west"]
    assert set(combined["document_ids"]) == {first["document_id"], second["document_id"]}


async def _check_identity_hints_from_other_windows(model, published, seed_rows):
    doc = _document("cross-window-identity-hints")
    declaration = "PGW-East is the East deployment registered as pay-east."
    call = "PGW-East calls Clearing (app_id clearing-prod) to settle payments."
    parser = Parser(SimpleNamespace(chunk_target=72, chunk_max=90, chunk_overlap=0))
    chunks = parser._chunk_text(
        declaration + "\n\n" + call, {"format": "txt"}, doc["document_id"], [], overlap=False
    )
    ids = _ids(published)

    class Language(_Language):
        async def json(self, system, request, schema):
            if "task" in request:
                assert request["task"]["mention"] == "PGW-East"
                [hint] = request["same_document_identity_hints"]
                assert hint["identity"]["fields"] == {"app_id": "pay-east"}
                assert hint["quotes"] == [declaration]
                assert all(
                    declaration not in part["text"]
                    for unit in request["source_rule_units"]
                    for part in unit["visible_parts"]
                )
                if not request["search_history"]:
                    return {"action": "lookup_published_entities"}
                [match] = request["search_history"][-1]["entities"]
                assert match["entity_id"] == ids["pay-east"]
                assert "唯一可靠身份" not in match["context"]
                return {"action": "use_existing", "candidate_id": ids["pay-east"]}
            if "allowed_pairs" in request or "review_units" in request:
                return {"entities": [], "relations": []}
            views = request["rule_units"]
            if any(declaration in part["text"] for unit in views for part in unit["visible_parts"]):
                return {
                    "entities": [_service(views, "pay-east", alias="PGW-East", quote=declaration)],
                    "relations": [],
                }
            return {
                "entities": [_mention(views, "PGW-East", call), _service(views, "clearing-prod")],
                "relations": [_call(views, "PGW-East", "clearing-prod", call)],
            }

    rows, issues = await _import(
        model, doc, {"chunks": chunks}, Language(), published, limit=8, window_chars=80
    )
    assert not issues, issues
    [edge] = _snapshot(model, seed_rows + rows).relations
    assert (edge["source_id"], edge["target_id"]) == (ids["pay-east"], ids["clearing-prod"])


async def check():
    model, (first, second), (registry, operations), (registry_input, ops_input) = _inputs()
    await _check_remote_endpoints(model)
    await _check_new_without_candidates(model)
    await _check_conflict_and_learned_mention(model)
    seed_rows, issues = await _import(
        model, registry, registry_input, _Language(registry=True), _PublishedGraph([], [])
    )
    published = _snapshot(model, seed_rows)
    ids = _ids(published)
    assert not issues and not published.relations, issues
    assert len(published.entities) == len(ids) == len(set(ids.values())) == 4
    assert set(ids) == set(first["service_app_ids"]) == set(second["service_app_ids"])

    await _check_semantic_identity(model, published, seed_rows)
    await _check_identity_hints_from_other_windows(model, published, seed_rows)

    [gold] = second["calls"]
    baseline = _Language(call=gold["phrase"])
    baseline_rows, issues = await _import(model, operations, ops_input, baseline, published)
    assert baseline.primary == 1 and not baseline.actions
    assert not _snapshot(model, seed_rows + baseline_rows).relations
    assert any(issue["reason_code"] == "IDENTITY_EVIDENCE_MISSING" for issue in issues), issues

    state = ({}, {})
    optimized = _Language(call=gold["phrase"])
    rows, issues = await _import(model, operations, ops_input, optimized, published, limit=8, state=state)
    assert optimized.primary == 1
    assert optimized.actions == [
        ("PGW-East", "lookup"),
        ("PGW-East", "use_existing"),
        ("PGW", "lookup"),
        ("PGW", "stop"),
    ]
    _assert_ambiguous_rejected(issues)

    updated = _snapshot(model, seed_rows + rows)
    assert _ids(updated) == ids and len(updated.entities) == len(ids)
    [edge] = updated.relations
    assert edge["relation_type"] == "CALLS"
    assert (edge["source_id"], edge["target_id"]) == (
        ids[gold["source_app_id"]],
        ids[gold["target_app_id"]],
    )
    assert edge["document_ids"] == gold["contribution_sources"]
    [support] = [row for row in rows if row["field_path"] == "$relation"]
    assert gold["phrase"] in {claim["quote"] for claim in support["evidence"]}

    forged = _Language(call=gold["phrase"], forged_candidate=True)
    forged_rows, issues = await _import(model, operations, ops_input, forged, published, limit=8)
    assert any(
        issue["reason_code"] == "IDENTITY_EVIDENCE_MISSING"
        and issue.get("details", {}).get("step", "").endswith("/PGW")
        for issue in issues
    ), issues
    [valid_edge] = _snapshot(model, seed_rows + forged_rows).relations
    assert (valid_edge["source_id"], valid_edge["target_id"]) == (edge["source_id"], edge["target_id"])

    retry = _Language(call=gold["phrase"])
    retry_rows, _ = await _import(model, operations, ops_input, retry, updated, limit=8, state=state)
    assert retry.primary == 0 and not retry.actions
    assert {row["contribution_id"] for row in retry_rows} == {row["contribution_id"] for row in rows}
    retried = _snapshot(model, seed_rows + retry_rows, previous=updated)
    assert _ids(retried) == ids and retried.relations == updated.relations

    retained = [row for row in seed_rows + retry_rows if row["document_id"] != operations["document_id"]]
    withdrawn = _snapshot(model, retained)
    assert _ids(withdrawn) == ids and not withdrawn.relations


def _score(stage, graph, rows, seed_ids):
    identities = _ids(graph)
    services = [item for item in graph.entities if item["entity_type"] == "Service"]
    inverse = {item["entity_id"]: item["identity"]["fields"]["app_id"] for item in services}
    actual = Counter(
        (inverse.get(edge["source_id"]), inverse.get(edge["target_id"]))
        for edge in graph.relations
        if edge["relation_type"] == "CALLS"
    )
    expected = {(call["source_app_id"], call["target_app_id"]): call for call in stage["calls"]}
    precise = sum(
        any(
            row["field_path"] == "$relation"
            and inverse.get(row["value"]["source_id"]) == source
            and inverse.get(row["value"]["target_id"]) == target
            and row["document_id"] in gold["contribution_sources"]
            and any(gold["phrase"] in ref["quote"] for ref in row["evidence"])
            for row in rows
        )
        for (source, target), gold in expected.items()
    )
    expected_ids = set(stage["service_app_ids"])
    service_hits = len(expected_ids & identities.keys())
    relation_hits = len(actual.keys() & expected.keys())
    return {
        "metrics": {
            "entities": precision_recall(service_hits, len(services), len(expected_ids)),
            "relations": precision_recall(relation_hits, sum(actual.values()), len(expected)),
            "source_evidence": coverage(precise, len(expected)),
        },
        "services": {
            "correct": service_hits,
            "expected": len(expected_ids),
            "unexpected": sorted(identities.keys() - expected_ids),
            "duplicates": len(services) - len(identities),
            "seed_ids_preserved": sum(
                identities.get(app) == entity_id for app, entity_id in seed_ids.items()
            ),
            "seed_ids_total": len(seed_ids),
        },
        "calls": {
            "correct": relation_hits,
            "expected": len(expected),
            "unexpected": sum(count for pair, count in actual.items() if pair not in expected),
            "duplicates": sum(count - 1 for count in actual.values()),
            "forbidden": [pair for pair in stage.get("forbidden_calls", []) if tuple(pair) in actual],
            "exact_gold_evidence": precise,
        },
    }


async def _measured_import(settings, model, doc, parsed, graph, result, checkpoint, limit=0):
    language = _MeasuredLanguage(settings)
    started = perf_counter()
    result["frozen_primary_windows"] = sum(key.startswith("batch/") for key in checkpoint)
    try:
        rows, issues = await _import(model, doc, parsed, language, graph, limit=limit, state=(checkpoint, {}))
        result.update(status="completed", issues=dict(Counter(issue["reason_code"] for issue in issues)))
        return rows
    except KGError as exc:
        result.update(status="failed", reason_code=exc.code)
        return None
    finally:
        await language.close()
        responses = checkpoint.get("_review", {}).get("responses", {})
        result.update(
            model_calls=len(language.calls),
            http_attempts=sum(call["http_attempts"] for call in language.calls),
            elapsed_seconds=round(perf_counter() - started, 3),
            document_actions=dict(
                Counter(
                    response["result"]["action"]
                    for key, response in responses.items()
                    if not key.startswith("window/")
                )
            ),
            calls=language.calls,
        )


async def _live(config):
    settings = Settings.load(config)
    settings = settings.model_copy(update={"llm": settings.construction_llm or settings.llm})
    model, (first, second), (registry, operations), (registry_input, ops_input) = _inputs()
    report = {"model": settings.llm.model, "registry": {}, "modes": {}}
    graph = _PublishedGraph([], [])
    seed_rows = await _measured_import(
        settings, model, registry, registry_input, graph, report["registry"], {}
    )
    if seed_rows is None:
        return report
    published = _snapshot(model, seed_rows)
    seed_ids = _ids(published)
    report["registry"].update(_score(first, published, seed_rows, {}))
    checkpoint = {}
    for name, limit in (("baseline", 0), ("loop", 8)):
        if limit:
            checkpoint = deepcopy(
                {
                    key: value
                    for key, value in checkpoint.items()
                    if key == "_input_scopes" or key.startswith("batch/")
                }
            )
            checkpoint["_input_scopes"]["review_limit"] = limit
        result = report["modes"][name] = {}
        rows = await _measured_import(
            settings, model, operations, ops_input, published, result, checkpoint, limit
        )
        if rows is None:
            break
        current = _snapshot(model, seed_rows + rows, previous=published)
        result.update(_score(second, current, rows, seed_ids))
    return report


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="用真实模型执行连续导入 A/B。")
    parser.add_argument("--config", help="模型配置文件。")
    parser.add_argument("--output", type=Path, help="保存真实模型结果。")
    args = parser.parse_args()
    if not args.live:
        if args.config or args.output:
            parser.error("--config 和 --output 需要 --live")
        asyncio.run(check())
        print("通过连续导入、向量召回、端点复用、新建、歧义处理、重试和来源撤回检查。")
        return
    if not args.config:
        parser.error("--live 需要指定 --config")
    report = asyncio.run(_live(args.config))
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False))
    if len(report["modes"]) != 2 or any(
        result["status"] != "completed" for result in report["modes"].values()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
