"""Deterministic source-fidelity regressions; no model or storage service required."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import KGError, canonical
from knowledge_graph.services.construction.extraction import build_rule_context, extract_text
from knowledge_graph.services.construction.literal import resolve_literal


def _resolve(chunks, selected):
    catalog = build_rule_context({"chunks": chunks})
    parts = []
    for phrase in selected:
        unit = next(unit for unit in catalog if phrase in unit["text"])
        start = unit["text"].index(phrase)
        parts.append({"unit_key": unit["unit_key"], "start": start, "end": start + len(phrase)})
    result = resolve_literal(
        parts,
        {unit["unit_key"]: unit for unit in catalog},
        {chunk["chunk_id"]: chunk for chunk in chunks},
        [unit["unit_key"] for unit in catalog],
    )
    return result, parts, catalog


def run():
    parser = Parser(SimpleNamespace(chunk_max=50, chunk_target=30, chunk_overlap=10))
    text = (
        "First rule: retain the prefix, conditional wording, and the exception.\n\nSecond rule: pay promptly."
    )
    text_chunks = parser._chunk_text(text, {"format": "txt"}, "doc-text", [])
    value, parts, catalog = _resolve(text_chunks, ["First rule:", "exception."])
    assert value["value"] == text[: text.index("exception.") + len("exception.")]
    assert value["fidelity"] == "source_exact" and not value["synthetic_parts"]
    second, _, _ = _resolve(text_chunks, ["Second rule: pay promptly."])
    assert second["value"] == "Second rule: pay promptly."
    assert all(part["source_spans"] for part in value["source_parts"])

    def rejects(parts_, chunks_, code):
        try:
            resolve_literal(
                parts_,
                {unit["unit_key"]: unit for unit in catalog},
                {chunk["chunk_id"]: chunk for chunk in chunks_},
                [unit["unit_key"] for unit in catalog],
            )
        except KGError as exc:
            assert exc.code == code, (exc.code, code)
        else:
            raise AssertionError("Invalid literal range was accepted")

    bad = deepcopy(parts)
    bad[0]["end"] = len(catalog[0]["text"]) + 1
    rejects(bad, text_chunks, "LITERAL_RANGE_INVALID")
    _, later_parts, _ = _resolve(text_chunks, ["First rule:", "pay promptly."])
    rejects(later_parts, text_chunks[:1], "EVIDENCE_NOT_FOUND")

    # Word paragraphs carry parser-inserted separators, which never become fact citations.
    from knowledge_graph.adapters.parsers.word import _word_block

    block = _word_block(
        [
            ("First term", {"format": "docx", "paragraph": 1}),
            ("if approved", {"format": "docx", "paragraph": 2}),
        ],
        {"format": "docx"},
        [],
        separator="\n\n",
        overlap=True,
    )
    word_chunks = parser._chunk_text(
        block.text, block.locator, "doc-word", [], spans=block.spans, block_order=0
    )
    word, _, _ = _resolve(word_chunks, ["First term", "if approved"])
    assert word["value"] == "First term\n\nif approved"
    assert word["fidelity"] == "parser_normalized"
    assert [item["text"] for item in word["synthetic_parts"]] == ["\n\n"]
    assert all("\n" not in item["quote"] for item in word["evidence"])

    between = _word_block(
        [
            ("middle", {"format": "docx", "table": 1, "row": 1, "column": 1}),
            ("condition", {"format": "docx", "table": 1, "row": 1, "column": 2}),
        ],
        {"format": "docx", "table": 1, "row": 1},
        [],
        separator="\t",
        overlap=False,
    )
    last = _word_block(
        [("final exception", {"format": "docx", "paragraph": 3})],
        {"format": "docx"},
        [],
        separator="\n\n",
        overlap=True,
    )
    word_blocks = [
        parser._chunk_text(item.text, item.locator, "doc-word-2", [], spans=item.spans, block_order=index)
        for index, item in enumerate((block, between, last))
    ]
    cross_word, _, _ = _resolve(sum(word_blocks, []), ["First term", "final exception"])
    assert cross_word["value"] == "First term\n\nif approved\n\nmiddle\tcondition\n\nfinal exception"
    assert [item["text"] for item in cross_word["synthetic_parts"]] == ["\n\n", "\n\n", "\t", "\n\n"]
    assert all("\t" not in item["quote"] and "\n" not in item["quote"] for item in cross_word["evidence"])

    page_one = parser._chunk_text(
        "Page one condition", {"format": "pdf", "page": 1}, "doc-pdf", [], block_order=0
    )
    page_two = parser._chunk_text(
        "Page two exception", {"format": "pdf", "page": 2}, "doc-pdf", [], block_order=1
    )
    pdf, _, _ = _resolve(page_one + page_two, ["Page one", "exception"])
    assert pdf["value"] == "Page one condition\n\nPage two exception"
    assert pdf["fidelity"] == "parser_normalized"
    assert [item["text"] for item in pdf["synthetic_parts"]] == ["\n\n"]
    import asyncio

    from .quality_structure import check as check_structure

    asyncio.run(check_structure())
    asyncio.run(_check_extraction_cache(parser))
    asyncio.run(_check_identity_projection(parser))
    asyncio.run(_check_batch_links())
    from .quality_retrieval_evidence import check as check_retrieval_evidence
    from .quality_review_regression import check as check_review_regressions

    asyncio.run(check_review_regressions())
    asyncio.run(check_retrieval_evidence())
    _check_plan_schema()
    print("quality deterministic boundaries passed")


async def _check_extraction_cache(parser):
    from knowledge_graph.domain.model import BusinessModel

    model = BusinessModel.model_validate(
        {
            "name": "Literal regression",
            "node_types": {
                "Clause": {
                    "identity": {"namespace": "clause", "fields": ["heading"], "scope": "document"},
                    "properties": {
                        "heading": {"type": "string", "required": True},
                        "definition": {"type": "string", "required": True, "source_mode": "literal"},
                    },
                    "atomic_groups": [{"name": "full_statement", "fields": ["definition"]}],
                }
            },
        }
    )
    text = "Payment: due in thirty days, unless disputed."
    parsed = {"chunks": parser._chunk_text(text, {"format": "txt"}, "doc-cache", [])}
    response = {
        "entities": [
            {
                "ref": "e1",
                "type": "Clause",
                "properties": {"heading": "Payment"},
                "evidence": {"heading": [{"unit_key": "unit/0", "quote": "Payment"}]},
                "atomic_groups": {
                    "full_statement": {
                        "fields": {},
                        "evidence": {},
                        "literal_parts": {
                            "definition": [{"unit_key": "unit/0", "start": 0, "end": len(text)}]
                        },
                        "section_key": "unit/0",
                        "rule_units": ["unit/0"],
                        "complete": True,
                        "references_resolved": True,
                    }
                },
            }
        ],
        "relations": [],
    }

    class Language:
        profile = SimpleNamespace(max_input_chars=100000, model_dump=lambda **_: {"model": "test"})

        def json_request_chars(self, *_):
            return 100

        async def json(self, *_):
            return deepcopy(response)

    async def persist():
        return None

    checkpoint = {}
    result = await extract_text(Language(), parsed, model, checkpoint, persist)
    declaration = result["entities"][0]["atomic_groups"]["full_statement"]
    assert declaration["fields"]["definition"] == text
    assert declaration["literal_provenance"]["definition"]["fidelity"] == "source_exact"
    assert checkpoint["batch/0"]["reusable"] is True
    fallback_response = deepcopy(response)
    declaration = fallback_response["entities"][0]["atomic_groups"]["full_statement"]
    declaration["fields"] = {"definition": text}
    declaration.pop("literal_parts")

    class FallbackLanguage(Language):
        async def json(self, *_):
            return deepcopy(fallback_response)

    fallback_checkpoint = {}
    fallback = await extract_text(
        FallbackLanguage(), {"chunks": parsed["chunks"]}, model, fallback_checkpoint, persist
    )
    fallback_declaration = fallback["entities"][0]["atomic_groups"]["full_statement"]
    assert fallback_declaration["fields"]["definition"] == text
    assert (
        fallback_declaration["literal_provenance"]["definition"]["selection_method"] == "unique_visible_match"
    )
    assert fallback_checkpoint["batch/0"]["reusable"] is True

    from knowledge_graph.services.construction.literal import _unique_visible_match

    repeated = parser._chunk_text("same same", {"format": "txt"}, "doc-repeat", [])
    units = build_rule_context({"chunks": repeated})
    try:
        _unique_visible_match(
            "same",
            {unit["unit_key"]: unit for unit in units},
            {chunk["chunk_id"]: chunk for chunk in repeated},
            [units[0]["unit_key"]],
        )
    except KGError as exc:
        assert exc.code == "LITERAL_RANGE_AMBIGUOUS"
    else:
        raise AssertionError("An ambiguous literal match was accepted")
    try:
        _unique_visible_match(
            "paraphrased",
            {unit["unit_key"]: unit for unit in units},
            {chunk["chunk_id"]: chunk for chunk in repeated},
            [units[0]["unit_key"]],
        )
    except KGError as exc:
        assert exc.code == "LITERAL_RANGE_MISSING"
    else:
        raise AssertionError("A rewritten literal value was accepted")

    checkpoint["batch/0"]["request_key"] = "stale"
    try:
        await extract_text(Language(), parsed, model, checkpoint, persist)
    except KGError as exc:
        assert exc.code == "PREPARATION_SCOPE_UNAVAILABLE"
    else:
        raise AssertionError("A checkpoint with a stale request key was reused")


async def _check_identity_projection(parser):
    from knowledge_graph.domain.model import BusinessModel
    from knowledge_graph.services.construction.alignment import align
    from knowledge_graph.services.construction.merge import aggregate, effective_contributions

    model = BusinessModel.model_validate(
        {
            "name": "Identity regression",
            "node_types": {
                "Organization": {
                    "identity": {"namespace": "organization", "fields": ["legal_name"]},
                    "identity_match": {"stable_fields": ["registration_id"], "explicit_alias": True},
                    "properties": {
                        "legal_name": {"type": "string", "required": True},
                        "registration_id": {"type": "string", "queryable": True},
                        "note": {"type": "string"},
                    },
                }
            },
            "relation_types": {"LINKS": {"source_types": ["Organization"], "target_types": ["Organization"]}},
        }
    )
    anchor_text = "Acme Corporation has registration R-17."
    alias_text = "A.C.M.E. with registration R-17 links to Beta Limited. Alias note applies."
    parser = Parser(SimpleNamespace(chunk_max=500, chunk_target=300, chunk_overlap=20))
    anchor_chunk = parser._chunk_text(anchor_text, {"format": "txt"}, "anchor", [])[0]
    alias_chunk = parser._chunk_text(alias_text, {"format": "txt"}, "alias", [])[0]

    def citation(chunk, phrase):
        start = chunk["text"].index(phrase)
        return [{"chunk_id": chunk["chunk_id"], "start": start, "end": start + len(phrase), "quote": phrase}]

    def document(document_id):
        return {"kb_id": "kb", "document_id": document_id, "source": {"domain_ids": []}}

    anchor = {
        "entities": [
            {
                "ref": "original",
                "type": "Organization",
                "properties": {"legal_name": "Acme Corporation", "registration_id": "R-17"},
                "evidence": {
                    "legal_name": citation(anchor_chunk, "Acme Corporation"),
                    "registration_id": citation(anchor_chunk, "R-17"),
                },
            }
        ],
        "relations": [],
    }
    anchor_rows, anchor_issues = await align(
        document("anchor"),
        anchor,
        {anchor_chunk["chunk_id"]: anchor_chunk},
        model,
        {},
        "job-anchor",
        [],
        [],
    )
    assert not anchor_issues
    graph_entity = aggregate("kb", anchor_rows, model)[0][0]

    class Graph:
        async def by_identity(self, _kb, kind, keys):
            if kind == "entity" and ("Organization", canonical(graph_entity["identity"])) in keys:
                return [graph_entity]
            return []

        async def by_property_value(self, _kb, _type, field, value, document_ids=None):
            if document_ids and "anchor" not in document_ids:
                return []
            if field == "registration_id" and value == "R-17":
                return [graph_entity]
            return []

    alias_candidate = {
        "ref": "alias",
        "type": "Organization",
        "mention": "A.C.M.E.",
        "mention_evidence": citation(alias_chunk, "A.C.M.E."),
        "properties": {"registration_id": "R-17", "note": "Alias note"},
        "evidence": {
            "registration_id": citation(alias_chunk, "R-17"),
            "note": citation(alias_chunk, "Alias note"),
        },
    }
    beta_candidate = {
        "ref": "beta",
        "type": "Organization",
        "properties": {"legal_name": "Beta Limited"},
        "evidence": {"legal_name": citation(alias_chunk, "Beta Limited")},
    }
    extracted = {
        "entities": [alias_candidate, beta_candidate],
        "relations": [
            {
                "type": "LINKS",
                "source_ref": "alias",
                "target_ref": "beta",
                "properties": {},
                "evidence": citation(alias_chunk, "links to Beta Limited"),
            }
        ],
    }
    alias_rows, alias_issues = await align(
        document("alias"),
        extracted,
        {alias_chunk["chunk_id"]: alias_chunk},
        model,
        {},
        "job-alias",
        [],
        [],
        graph=Graph(),
    )
    assert not alias_issues, alias_issues
    assert not any(
        row["field_path"] == "$exists" and row["target_id"] == graph_entity["entity_id"] for row in alias_rows
    )
    assert any(row["field_path"] == "$mention" for row in alias_rows)
    assert all(
        row["claim"].get("identity_dependencies")
        for row in alias_rows
        if row["target_id"] == graph_entity["entity_id"] or row["target_kind"] == "relation"
    )
    active, suppressed = effective_contributions(anchor_rows + alias_rows)
    assert not suppressed and len(active) == len(anchor_rows + alias_rows)
    whole_entities, whole_relations = aggregate("kb", active, model)
    assert len(whole_entities) == 2 and len(whole_relations) == 1
    scoped, hidden = effective_contributions(alias_rows)
    assert hidden and not any(row["target_id"] == graph_entity["entity_id"] for row in scoped)
    scoped_entities, scoped_relations = aggregate("kb", scoped, model)
    assert len(scoped_entities) == 1 and not scoped_relations
    replacement_anchor = [
        row | {"document_id": "replacement", "contribution_id": "replacement-" + row["contribution_id"]}
        for row in anchor_rows
    ]
    retained, hidden = effective_contributions(replacement_anchor + alias_rows)
    assert not hidden and len(aggregate("kb", retained, model)[1]) == 1
    orphan = deepcopy(alias_rows[0])
    orphan["claim"].pop("identity_dependencies")
    try:
        aggregate("kb", [orphan], model)
    except KGError as exc:
        assert exc.code == "INVALID_CONTRIBUTION"
    else:
        raise AssertionError("An unexpected orphan contribution was hidden")

    unsupported = deepcopy(alias_candidate)
    unsupported["properties"].pop("registration_id")
    unsupported["evidence"].pop("registration_id")
    rejected_rows, rejected_issues = await align(
        document("alias"),
        {"entities": [unsupported], "relations": []},
        {alias_chunk["chunk_id"]: alias_chunk},
        model,
        {},
        "job-reject",
        [],
        [],
        graph=Graph(),
    )
    assert not rejected_rows and rejected_issues[0]["reason_code"] == "IDENTITY_EVIDENCE_MISSING"

    class AmbiguousGraph(Graph):
        async def by_property_value(self, *_args, **_kwargs):
            return [graph_entity, graph_entity | {"entity_id": "other-entity"}]

    rejected_rows, rejected_issues = await align(
        document("alias"),
        {"entities": [alias_candidate], "relations": []},
        {alias_chunk["chunk_id"]: alias_chunk},
        model,
        {},
        "job-ambiguous",
        [],
        [],
        graph=AmbiguousGraph(),
    )
    assert not rejected_rows and rejected_issues[0]["reason_code"] == "IDENTITY_AMBIGUITY"


async def _check_batch_links():
    from knowledge_graph.domain.model import BusinessModel
    from knowledge_graph.services.construction.extraction import _plan_scopes

    model = BusinessModel.model_validate(
        {
            "name": "Batch regression",
            "node_types": {
                "Person": {
                    "identity": {"namespace": "person", "fields": ["name"], "scope": "document"},
                    "properties": {
                        "name": {"type": "string", "required": True},
                        "aliases": {"type": "string", "merge": "union"},
                    },
                }
            },
            "relation_types": {"LINKS": {"source_types": ["Person"], "target_types": ["Person"]}},
        }
    )
    parser = Parser(SimpleNamespace(chunk_max=500, chunk_target=300, chunk_overlap=0))
    first_text = "Alice works with Bob.\n\n"
    second_text = "Robert is also known as Bob and is an employee."
    first = parser._chunk_text(first_text, {"format": "txt"}, "doc-batch", [], overlap=False)[0]
    second = parser._chunk_text(
        second_text,
        {"format": "txt"},
        "doc-batch",
        [],
        overlap=False,
        char_offset=len(first_text),
    )[0]
    parsed = {"chunks": [first, second]}
    context = build_rule_context(parsed)

    class Language:
        def __init__(self):
            self.calls = []
            self.profile = SimpleNamespace(max_input_chars=100000, model_dump=lambda **_: {"model": "test"})

        def json_request_chars(self, *_):
            return 100

        async def json(self, system, request, _schema):
            self.calls.append(request)
            if request.get("allowed_pairs"):
                directory = {entry["name"]: entry["stable_id"] for entry in request["endpoint_catalog"]}
                assert "Bob" in next(
                    entry["surface_forms"]
                    for entry in request["endpoint_catalog"]
                    if entry["name"] == "Robert"
                )
                quote = "Alice works with Bob"
                return {
                    "entities": [],
                    "relations": [
                        {
                            "type": "LINKS",
                            "source_ref": directory["Alice"],
                            "target_ref": directory["Robert"],
                            "properties": {},
                            "evidence": [
                                {"chunk_id": "chunk/0", "start": 0, "end": len(quote), "quote": quote}
                            ],
                        }
                    ],
                }
            name = "Alice" if "Alice" in request["chunks"][0]["text"] else "Robert"
            properties = {"name": name}
            evidence = {
                "name": [
                    {
                        "chunk_id": "chunk/0",
                        "start": request["chunks"][0]["text"].index(name),
                        "end": request["chunks"][0]["text"].index(name) + len(name),
                        "quote": name,
                    }
                ]
            }
            if name == "Robert":
                alias_start = request["chunks"][0]["text"].index("Bob")
                properties["aliases"] = "Bob"
                evidence["aliases"] = [
                    {"chunk_id": "chunk/0", "start": alias_start, "end": alias_start + 3, "quote": "Bob"}
                ]
            return {
                "entities": [
                    {
                        "ref": "e1",
                        "type": "Person",
                        "properties": properties,
                        "evidence": evidence,
                    }
                ],
                "relations": [],
            }

    class OneChunkLanguage(Language):
        def json_request_chars(self, _system, request, _schema):
            return 100 if len(request.get("chunks", [])) <= 1 else self.profile.max_input_chars + 1

    language = OneChunkLanguage()
    from knowledge_graph.services.construction.extraction import Extraction

    chunks = {item["chunk_id"]: item for item in parsed["chunks"]}
    scopes = _plan_scopes(language, chunks, context, model.model_dump(), Extraction.model_json_schema())
    assert len(scopes) == 2 and all(
        view["fully_supplied"] for scope in scopes for view in scope["unit_views"]
    )
    whole = _plan_scopes(Language(), chunks, context, model.model_dump(), Extraction.model_json_schema())
    assert len(whole) == 1
    # Force a source-unit boundary across chunks without changing its original coordinates.
    partial_parser = Parser(SimpleNamespace(chunk_max=40, chunk_target=30, chunk_overlap=0))
    long_chunks = partial_parser._chunk_text(
        "One long rule " + "with conditions " * 8 + "and an exception.",
        {"format": "txt"},
        "doc-partial",
        [],
        overlap=False,
    )
    partial_context = build_rule_context({"chunks": long_chunks})
    partial_scopes = _plan_scopes(
        language,
        {item["chunk_id"]: item for item in long_chunks},
        partial_context,
        model.model_dump(),
        Extraction.model_json_schema(),
    )
    assert len(partial_scopes) > 1 and any(
        not view["fully_supplied"] for scope in partial_scopes for view in scope["unit_views"]
    )

    class TooSmall(Language):
        def json_request_chars(self, *_):
            return self.profile.max_input_chars + 1

    try:
        _plan_scopes(
            TooSmall(),
            {first["chunk_id"]: first},
            context,
            model.model_dump(),
            Extraction.model_json_schema(),
        )
    except KGError as exc:
        assert exc.code == "MODEL_INPUT_TOO_LARGE"
    else:
        raise AssertionError("One oversized source chunk was silently skipped")

    async def persist():
        return None

    checkpoint = {}
    result = await extract_text(
        language,
        parsed,
        model,
        checkpoint,
        persist,
    )
    assert not result["issues"] and len(result["entities"]) == 2 and len(result["relations"]) == 1
    relation = result["relations"][0]
    assert relation["source_ref"].startswith("batch/0/")
    assert relation["target_ref"].startswith("batch/1/")
    assert len(language.calls) == 3 and "link/0" in checkpoint
    await extract_text(
        language,
        parsed,
        model,
        checkpoint,
        persist,
    )
    assert len(language.calls) == 3
    checkpoint["link/0"]["input_scope"]["input_chunk_ids"] = []
    try:
        await extract_text(
            language,
            parsed,
            model,
            checkpoint,
            persist,
        )
    except KGError as exc:
        assert exc.code == "PREPARATION_SCOPE_UNAVAILABLE"
    else:
        raise AssertionError("Cross-batch cache exceeded its saved input scope")


def _check_plan_schema():
    import json
    from uuid import uuid4

    from knowledge_graph.domain.jobs import validate_prepared_mutation
    from knowledge_graph.domain.model import BusinessModel, EmbeddingProfile

    model = BusinessModel.model_validate(
        {
            "name": "Plan regression",
            "node_types": {
                "Thing": {
                    "identity": {"namespace": "thing", "fields": ["name"]},
                    "properties": {"name": {"type": "string"}},
                }
            },
        }
    )
    plan = {
        "schema_version": 5,
        "chunk_index": {"points": [], "members": [], "write_ids": [], "delete_ids": [], "current_count": 0},
        "verification": None,
        "write_mode": "full",
        "write_set": {
            "entity_ids": [],
            "relation_ids": [],
            "chunk_ids": [],
            "object_ids": [],
            "contribution_document_ids": [],
        },
        "plan_id": str(uuid4()),
        "job_id": str(uuid4()),
        "kb_id": str(uuid4()),
        "operation": "REINDEX",
        "document_ids": [],
        "model": model.model_dump(),
        "model_revision": str(uuid4()),
        "embedding_profile": EmbeddingProfile().model_dump(),
        "parser_settings": {},
        "entities": [],
        "relations": [],
        "contributions": [],
        "identity_dependency_targets": [],
        "identity_dependency_projection": {"active": [], "suppressed": []},
        "chunk_points": [],
        "object_points": [],
        "issues": [],
        "issue_documents": [],
        "delete_entities": [],
        "delete_relations": [],
        "delete_chunks": [],
        "delete_objects": [],
        "activate_documents": [],
        "delete_documents": [],
        "cleanup_paths": [],
        "cleanup_job_ids": [],
        "warnings": [],
        "counts": {},
    }
    validate_prepared_mutation(plan)
    restored = json.loads(json.dumps(plan))
    validate_prepared_mutation(restored)
    altered = deepcopy(restored)
    altered["identity_dependency_projection"]["active"] = [str(uuid4())]
    try:
        validate_prepared_mutation(altered)
    except KGError as exc:
        assert exc.code == "PLAN_INVALID"
    else:
        raise AssertionError("A tampered projection survived plan validation")
    altered = deepcopy(restored)
    del altered["identity_dependency_targets"]
    try:
        validate_prepared_mutation(altered)
    except KGError as exc:
        assert exc.code == "PLAN_INVALID"
    else:
        raise AssertionError("A plan missing its dependency target scope was accepted")


if __name__ == "__main__":
    run()
