"""Local regressions for evidence ranking, candidate budgets, and source isolation."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain.source import source_evidence
from knowledge_graph.prompts.retrieval import COMPACT_RERANK_SYSTEM, DOCUMENT_RERANK_SYSTEM
from knowledge_graph.services.contracts import SearchRequest, envelope
from knowledge_graph.services.retrieval.evidence import EvidenceMixin
from knowledge_graph.services.retrieval.rerank import (
    _SCHEMA,
    Excerpt,
    _compact_payload,
    _pick_excerpts,
    compact_rerank,
)
from knowledge_graph.services.retrieval.search import SearchMixin
from knowledge_graph.services.retrieval.source_context import source_context
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


async def _check_document_selection():
    parser = Parser(SimpleNamespace(chunk_target=1200, chunk_max=2000, chunk_overlap=0))
    doc, other = str(uuid4()), str(uuid4())
    text = "Routine inspection starts on Monday. Only a supervisor may approve it."
    prefix, tail = text[:10], text[10:]
    chunks = [
        parser._chunk_text(body, {"format": "pdf", "page": page}, document, [], char_offset=start)[0]
        for body, page, document, start in (
            (text, 1, doc, 0),
            (tail, 1, doc, len(prefix)),
            (tail + " Stop.", 1, doc, len(prefix)),
            (tail, 1, other, len(prefix)),
            (tail, 2, doc, len(prefix)),
        )
    ]
    records = {item["chunk_id"]: item for item in chunks}
    ordered = list(records)
    language = FakeLanguage()

    async def rank(system, data, schema):
        assert language.json_request_chars(system, data, schema) <= language.profile.max_input_chars
        language.requests.append((system, data))
        limit = data.get("ranking_limit", data["final_limit"])
        return {"ids": [item["id"] for item in data["candidates"][:limit]]}

    language.json = rank
    runtime = SimpleNamespace(settings=Settings(), language=language)
    request = SearchRequest(query="Who approves routine and emergency inspections?", limit=4, kinds=["chunk"])
    args = (runtime, "kb", request, ordered, records, "documents", None, {})
    ranked, sent, _ = await compact_rerank(*args)
    assert ranked == [ordered[i] for i in (0, 2, 3, 4, 1)] and sent == set(ordered)
    system, data = language.requests[-1]
    assert system == DOCUMENT_RERANK_SYSTEM and data["ranking_limit"] == 5
    assert [item["location"][0] for item in data["candidates"]] == [1, 1, 1, 2, 3]

    base, _, _ = await _compact_payload(*args)
    language.profile = SimpleNamespace(
        max_input_chars=language.json_request_chars(COMPACT_RERANK_SYSTEM, base, _SCHEMA)
    )
    ranked, sent, _ = await compact_rerank(*args)
    assert ranked == ordered and sent == set(ordered)
    assert language.requests[-1] == (COMPACT_RERANK_SYSTEM, base)


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


async def _check_source_context():
    parser = Parser(SimpleNamespace(chunk_target=1200, chunk_max=2000, chunk_overlap=0))
    kb_id, doc_id, other_doc, entity_id = (str(uuid4()) for _ in range(4))
    procedure = (
        "1. Open the settings page.\n\n2. Back up the configuration.\n\n"
        "Example:\n\ncopy settings settings.old\n\n3. Apply the change.\n\n"
        "   Wait until the backup finishes.\n\n   KEEP THE BACKUP AVAILABLE"
    )
    text = f"## Upgrade\n\n{procedure}\n\n## Reset\n\n1. Remove the database.\n\n2. Start over."
    chunk = parser._chunk_text(text, {"format": "md"}, doc_id, [])[0] | {"kb_id": kb_id}
    claim_ids = [str(uuid4()), str(uuid4())]
    claims = []
    for phrase, claim_id in zip(("Open the settings page.", "Apply the change."), claim_ids, strict=True):
        start = text.index(phrase)
        claims.append(
            {
                "contribution_id": claim_id,
                "target_kind": "entity",
                "target_id": entity_id,
                "document_id": doc_id,
                "field_path": "/properties/statement",
                "evidence": [source_evidence(chunk, start, start + len(phrase))],
            }
        )

    async def contributions(kb, documents, *, contribution_ids):
        assert kb == kb_id and documents == [doc_id] and other_doc not in documents
        assert set(contribution_ids) == set(claim_ids)
        return claims

    async def read(kb, collection, ids):
        assert kb == kb_id and collection == "chunks" and ids == [chunk["chunk_id"]]
        return [{"id": chunk["chunk_id"], "payload": chunk}]

    detail = EvidenceMixin()
    detail.r = SimpleNamespace(
        metadata=SimpleNamespace(contributions=contributions), vectors=SimpleNamespace(read=read)
    )
    items = [{"entity_id": entity_id, "contribution_ids": claim_ids}]
    limited, expanded = envelope(kb_id), envelope(kb_id)
    await detail._attach_evidence(limited, kb_id, items, 2, [doc_id])
    await detail._attach_evidence(expanded, kb_id, items, 3, [doc_id])
    assert expanded["evidence"][:2] == limited["evidence"]
    assert expanded["declarations"] == limited["declarations"]
    assert len(expanded["evidence"]) == 3
    added = expanded["evidence"][-1]
    assert added["text"] == procedure and added["role"] == "context"
    assert added["document_id"] == doc_id and added["fact_ids"] == [entity_id]
    assert added["contribution_ids"] == [] and added["field_paths"] == []
    assert expanded["status"] == limited["status"] == "ok"
    for field in ("available_references", "returned_references"):
        assert expanded["coverage"][0]["budget"][field] == limited["coverage"][0]["budget"][field] == 2
    assert expanded["coverage"][0]["budget"]["returned_source_context"] == 1


def _check_source_context_boundaries():
    doc, entity = str(uuid4()), str(uuid4())
    parser = Parser(SimpleNamespace(chunk_target=1200, chunk_max=2000, chunk_overlap=0))
    text = "Enable debug mode temporarily.\n\n示例：\n\nexport DEBUG=1"
    chunk = parser._chunk_text(text, {"format": "txt"}, doc, [])[0]

    def expand(chunk, anchor):
        start = chunk["text"].index(anchor)
        reference = source_evidence(chunk, start, start + len(anchor)) | {"fact_ids": [entity]}
        return source_context([reference], {chunk["chunk_id"]: chunk}, {entity}, 1)

    assert [r["text"] for r in expand(chunk, "export DEBUG=1")] == [text]
    legacy = deepcopy(chunk)
    del legacy["block_range"]
    assert not expand(legacy, "export DEBUG=1")

    steps = "1. Shut down the motor.\n\n2. Close the supply valve.\n\n"
    truncated = parser._chunk_text(steps, {"format": "txt"}, doc, [])[0]
    truncated["parser_profile"]["block_length"] += 30
    assert not expand(truncated, "Close the supply valve.")

    oversized = parser._chunk_text(
        "1. Prepare the station.\n\n2. " + "Repeat the inspection. " * 65,
        {"format": "txt"},
        doc,
        [],
    )[0]
    assert not expand(oversized, "Prepare the station.")

    paragraph = "Each permit expires at midnight. A supervisor may extend it during an emergency."
    body = f"## Permits\n\n{paragraph}\n\n## Exceptions\n\nOther rules apply."
    complete = parser._chunk_text(body, {"format": "md"}, doc, [])[0]
    (added,) = expand(complete, "expires at midnight")
    start = body.index(paragraph)
    assert added == source_evidence(complete, start, start + len(paragraph), role="context") | {
        "text": paragraph,
        "fact_ids": [entity],
        "contribution_ids": [],
        "field_paths": [],
    }
    assert not expand(complete, paragraph)

    offset = body.index("\n")
    overlap = parser._chunk_text(body[offset:], {"format": "md"}, doc, [], char_offset=offset)[0]
    chunks = {item["chunk_id"]: item for item in (complete, overlap)}
    refs = []
    for item in chunks.values():
        pos = item["text"].index("expires at midnight")
        refs.append(source_evidence(item, pos, pos + len("expires at midnight")) | {"fact_ids": [entity]})
    assert [item["text"] for item in source_context(refs, chunks, {entity}, 2)] == [paragraph]

    left = parser._chunk_text(paragraph, {"format": "md"}, doc, [], char_offset=20)[0]
    left["block_range"] = [20, 20 + len(paragraph)]
    left["parser_profile"]["block_length"] = 20 + len(paragraph)
    right = parser._chunk_text(paragraph, {"format": "md"}, doc, [])[0]
    right["parser_profile"]["block_length"] += 20
    assert not expand(left, "expires at midnight")
    assert not expand(right, "expires at midnight")


async def check():
    await _check_candidate_payload()
    await _check_channel_budgets()
    await _check_rerank_fallback()
    await _check_document_selection()
    await _check_primary_evidence_budget()
    await _check_source_context()
    _check_source_context_boundaries()


if __name__ == "__main__":
    asyncio.run(check())
    print("evidence retrieval deterministic boundaries passed")
