"""Local regressions for evidence ranking, candidate budgets, and source isolation."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain.source import source_evidence
from knowledge_graph.services.contracts import SearchRequest, envelope
from knowledge_graph.services.retrieval.rerank import (
    Excerpt,
    _compact_payload,
    _pick_excerpts,
    compact_rerank,
)
from knowledge_graph.services.retrieval.search import SearchMixin
from knowledge_graph.settings import Settings


class FakeLanguage:
    profile = SimpleNamespace(max_input_chars=60000)

    def __init__(self):
        self.requests = []

    def json_request_chars(self, system, data, schema):
        return len(system) + len(json.dumps(data, ensure_ascii=False)) + len(json.dumps(schema))

    async def json(self, system, data, schema):
        assert self.json_request_chars(system, data, schema) <= self.profile.max_input_chars
        self.requests.append(data)
        return {"ids": [item["id"] for item in data["candidates"][: data["final_limit"]]]}


async def _check_candidate_payload():
    parser = Parser(SimpleNamespace(chunk_target=1600, chunk_max=2000, chunk_overlap=0))
    a, b, outside = map(str, (uuid4(), uuid4(), uuid4()))

    def chunk(doc, text, order):
        return parser._chunk_text(text, {"format": "txt"}, doc, [], block_order=order)[0]

    source_a = [
        chunk(a, "East Manual: inspection policy requires unloading dock cranes overnight.", 0),
        *[
            chunk(a, f"Inspection policy {i} requires unloading dock cranes overnight.", i)
            for i in range(1, 7)
        ],
    ]
    source_b = chunk(b, "Dock cranes must remain loaded during inspection in the West terminal.", 0)
    external = chunk(outside, "Outside source has a distractor inspection policy.", 0)
    chunks = {item["chunk_id"]: item for item in [*source_a, source_b, external]}
    requested_chunks = set()

    async def read(kb_id, collection, ids):
        assert collection == "chunks"
        requested_chunks.update(ids)
        return [{"id": key, "payload": chunks[key]} for key in ids]

    def claim(key, source, *, doc=None, field="/atomic_groups/inspection/action"):
        return {
            "target_kind": "entity",
            "target_id": key,
            "document_id": doc or source["document_id"],
            "field_path": field,
            "evidence": [source_evidence(source, 0, len(source["text"]), role="fact")],
        }

    contributions = [claim("rule", item) for item in [*source_a, source_b]]
    contributions += [
        claim("duplicate", source_a[0], field="/properties/policy"),
        claim("wrong", external, doc=a),
    ]
    language = FakeLanguage()
    settings = Settings(rerank_candidate_limit=3)
    runtime = SimpleNamespace(settings=settings, language=language, vectors=SimpleNamespace(read=read))
    request = SearchRequest(
        query="Compare the dock inspection policy in East Manual and West Manual",
        limit=2,
        filters={"document_ids": [a, b]},
    )
    records = {key: {"canonical_name": key} for key in ("rule", "duplicate", "wrong", "not_shortlisted")}
    order, sent, counts = await compact_rerank(
        runtime,
        "kb",
        request,
        list(records),
        records,
        "object_candidates",
        {"contributions": contributions},
        {a: "East Manual", b: "West Manual"},
    )
    assert order == list(records)
    assert sent == {"rule", "duplicate", "wrong"} and counts["rerank_shortlist"] == 3
    assert counts["invalid_rerank_evidence"] == 1 and counts["rerank_without_evidence"] == 1
    assert counts["rerank_payload_skipped"] == 0 and counts["rerank_selected"] == 2
    assert len(requested_chunks) <= 7 and source_b["chunk_id"] in requested_chunks
    assert external["chunk_id"] not in requested_chunks
    data = language.requests[-1]
    assert data["comparison"] and set(data["sources"].values()) == {"East Manual", "West Manual"}
    snippets = data["snippets"]
    first, duplicate, wrong = [set(c["snippets"]) for c in data["candidates"]]
    assert not wrong
    assert {data["sources"][snippets[key][1]] for key in first} == {"East Manual", "West Manual"}
    assert not (set(data["sources"]) & {a, b}) and any("remain loaded" in snippets[key][0] for key in first)
    assert duplicate <= first

    bad = deepcopy(contributions[0])
    bad["evidence"][0]["quote"] = "fabricated exact quotation"
    _, sent, counts = await compact_rerank(
        runtime,
        "kb",
        request,
        ["rule"],
        records,
        "object_candidates",
        {"contributions": [bad]},
        {},
    )
    assert not sent and counts["invalid_rerank_evidence"] == 1

    long = [
        chunk(a, f"Inspection alternative {i}. " + "A long handling procedure for dock cranes. " * 30, i)
        for i in range(10, 18)
    ]
    language.profile = SimpleNamespace(max_input_chars=5100)
    settings.rerank_candidate_limit = 5
    rows = {item["chunk_id"]: item for item in long}
    request = SearchRequest(query="inspection procedures", limit=2, kinds=["chunk"])
    order, sent, counts = await compact_rerank(
        runtime, "kb", request, list(rows), rows, "documents", None, {}
    )
    assert len(order) == len(rows) and 1 < len(sent) < 5
    assert counts["rerank_payload_skipped"] == 5 - len(sent)
    assert len(language.requests[-1]["candidates"]) == len(sent)
    assert all(
        len(excerpt[0]) <= settings.retrieval_rerank_snippet_chars
        for excerpt in language.requests[-1]["snippets"].values()
    )


async def _check_channel_budgets():
    settings = Settings(candidate_scan_limit=30, rerank_enabled=False)
    kb = {
        "kb_id": "kb",
        "model": {
            "name": "Test policy",
            "node_types": {
                "Rule": {
                    "identity": {"namespace": "rule", "fields": ["code"]},
                    "properties": {"code": {"type": "string"}},
                }
            },
        },
        "embedding_profile": {},
    }
    calls = []

    async def keyword(kb_id, text, scan, filters):
        calls.append(("keyword_scan", scan))
        return [{"id": key, "payload": {"text": key}} for key in ("late_relevant", "early_weak")][
            :scan
        ], False

    async def exists(kb_id, collection):
        calls.append(("vector_collection", collection))
        return False

    async def candidates(kb_id, text, scan, **filters):
        calls.append(("exact_scan", scan))
        exact = {"entity_id": "exact", "canonical_name": text}
        return [exact], [{"entity_id": "similar", "canonical_name": "similar name"}]

    async def fulltext(kb_id, text, limit, **filters):
        calls.append(("fulltext_limit", limit))
        return []

    class Search(SearchMixin):
        def _check_size(self, summaries):
            assert len(summaries) == 1

    search = Search()
    search.r = SimpleNamespace(
        settings=settings,
        vectors=SimpleNamespace(keyword=keyword, exists=exists),
        graph=SimpleNamespace(candidates=candidates, fulltext=fulltext),
    )
    result = await search._search(kb, SearchRequest(query="EXACT-42", kinds=["entity", "chunk"], limit=1))
    assert [item["id"] for item in result["knowledge"]] == ["exact", "late_relevant"]
    assert "exact_name" in result["knowledge"][0]["match_channels"]
    assert ("exact_scan", 30) in calls
    assert ("fulltext_limit", 30) in calls and ("keyword_scan", 30) in calls
    assert ("vector_collection", "objects") in calls and ("vector_collection", "chunk_vectors") in calls
    assert result["coverage"][-1]["budget"]["fused_candidates"] == 2

    settings.rerank_enabled = True
    order, sent = await search._rank_search(
        "kb",
        SearchRequest(query="EXACT-42"),
        ["exact", "similar"],
        {},
        envelope("kb"),
        "object_candidates",
        None,
        {},
        has_exact=True,
    )
    assert order == ["exact", "similar"] and not sent


async def _check_rerank_fallback():
    doc = str(uuid4())
    parser = Parser(SimpleNamespace(chunk_target=1600, chunk_max=2000, chunk_overlap=0))
    records = {
        chunk["chunk_id"]: chunk
        for text in (
            "Routine checks begin on Monday.",
            "Inspect the emergency valves.",
            "Record the pressure.",
        )
        for chunk in parser._chunk_text(text, {"format": "txt"}, doc, [])
    }
    ordered = list(records)
    language = FakeLanguage()

    async def invalid_ids(system, data, schema):
        return {"ids": ["unknown", "2", "2"]}

    language.json = invalid_ids
    search = SearchMixin()
    search.r = SimpleNamespace(settings=Settings(), language=language)
    request = SearchRequest(query="When are the inspections?", limit=2, kinds=["chunk"])
    result = envelope("kb")
    ranked, sent = await search._rank_search("kb", request, ordered, records, result, "documents", None, {})
    assert ranked == [ordered[1], ordered[0], ordered[2]] and sent == set(ordered)
    assert result["warnings"] == [{"code": "RERANK_IDS_IGNORED", "count": 2}]

    async def timeout(system, data, schema):
        raise TimeoutError

    language.json = timeout
    result = envelope("kb")
    ranked, sent = await search._rank_search("kb", request, ordered, records, result, "documents", None, {})
    assert ranked == ordered and not sent
    assert result["status"] == "partial" and result["warnings"][0]["code"] == "RERANK_UNAVAILABLE"

    async def slow_source_read(*args, **kwargs):
        await asyncio.sleep(5)
        raise AssertionError("Evidence loading exceeded the rerank budget")

    search.r.settings.rerank_timeout = 0.01
    search.r.metadata = SimpleNamespace(target_contributions=slow_source_read)
    result = envelope("kb")
    ranked, sent = await search._rank_search(
        "kb", SearchRequest(query=request.query), ordered, records, result, "object_candidates", None, {}
    )
    assert ranked == ordered and not sent
    assert result["status"] == "partial" and result["warnings"][0]["code"] == "RERANK_UNAVAILABLE"


async def _check_primary_evidence_budget():
    doc = str(uuid4())
    parser = Parser(SimpleNamespace(chunk_target=1600, chunk_max=2000, chunk_overlap=0))
    statements = (
        "Routine inspection verifies that the conveyor safety rail is secure. ",
        "This procedure covers a separate maintenance shutdown and permit review. ",
        "An exception permits operation during emergency evacuation only. ",
        "The applicable supervisor must keep all the incident review records. ",
        "Conveyor service resumes after the independent operator approves it. ",
        "This instruction applies to every approved servicing window. ",
    )
    chunks = {}
    claims = []
    rows = {}
    for index, statement in enumerate(statements):
        chunk = parser._chunk_text(
            statement * 5,
            {"format": "md"},
            doc,
            [],
            char_offset=1000 * index,
            section_path=["Operating manual", "Safety inspections"],
        )[0]
        chunks[chunk["chunk_id"]] = chunk
        target = str(index // 2)
        rows[target] = {"canonical_name": "Procedure " + target}
        claims.append(
            {
                "document_id": doc,
                "target_kind": "entity",
                "target_id": target,
                "field_path": "/properties/procedure",
                "evidence": [source_evidence(chunk, 0, len(chunk["text"]), role="fact")],
            }
        )

    async def read(kb_id, collection, ids):
        return [{"id": key, "payload": chunks[key]} for key in ids]

    language = FakeLanguage()
    language.profile = SimpleNamespace(max_input_chars=2800)
    runtime = SimpleNamespace(settings=Settings(), language=language, vectors=SimpleNamespace(read=read))
    request = SearchRequest(query="When can inspection be suspended?", limit=2)
    data, sent, counts = await _compact_payload(
        runtime, "kb", request, list(rows), rows, "object_candidates", {"contributions": claims}, {}
    )
    assert counts["rerank_candidates"] == 3
    assert list(sent.values()) == list(rows) and all(
        candidate["snippets"] for candidate in data["candidates"]
    )
    assert counts["rerank_supplemental_skipped"] > 0

    first = next(iter(chunks))
    data, _, _ = await _compact_payload(runtime, "kb", request, [first], chunks, "documents", None, {})
    assert data["candidates"][0]["section"] == "Operating manual > Safety inspections"
    assert all("Safety inspections" not in snippet[0] for snippet in data["snippets"].values())

    excerpts = [
        Excerpt("A", "page", 0, "Inspection must finish before a shift starts."),
        Excerpt("A", "page", 50, "Inspection follows the posted schedule."),
        Excerpt("B", "page", 0, "Inspection may be postponed."),
        Excerpt("C", "page", 0, "Only registered personnel may operate equipment."),
    ]
    assert _pick_excerpts(excerpts, {"inspection"}, set()) == excerpts[:3]


async def check():
    await _check_candidate_payload()
    await _check_channel_budgets()
    await _check_rerank_fallback()
    await _check_primary_evidence_budget()


if __name__ == "__main__":
    asyncio.run(check())
    print("evidence retrieval deterministic boundaries passed")
