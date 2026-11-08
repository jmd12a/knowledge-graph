"""Focused regressions for identity dependency and scoped graph review findings."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import KGError, canonical
from knowledge_graph.domain.model import BusinessModel
from knowledge_graph.domain.source import effective_contributions
from knowledge_graph.services.construction.extraction import (
    _assemble_rules,
    _mark_partial_source,
    build_rule_context,
    extract_text,
)
from knowledge_graph.services.construction.literal import resolve_literal
from knowledge_graph.services.construction.pipeline import (
    dependency_target_closure,
    retry_extraction_checkpoints,
)
from knowledge_graph.services.contracts import GraphRequest
from knowledge_graph.services.retrieval.graph import GraphMixin


def _model(stable_field):
    return {
        "name": "identity discriminators",
        "node_types": {
            "Service": {
                "identity": {"namespace": "service", "fields": ["app_id"]},
                "identity_match": {"stable_fields": [stable_field]},
                "properties": {
                    "app_id": {"type": "string", "required": True, "queryable": True},
                    "name": {"type": "string", "queryable": True},
                    "aliases": {"type": "string", "queryable": True},
                },
            }
        },
    }


def _check_discriminator():
    BusinessModel.model_validate(_model("app_id"))
    for display in ("name", "aliases"):
        try:
            BusinessModel.model_validate(_model(display))
        except ValueError:
            pass
        else:
            raise AssertionError(f"Display field {display} became an identity discriminator")


def _check_retry_extraction():
    baseline = {"job_id": "job", "operation": "IMPORT", "model": {"revision": 1}}
    settings = SimpleNamespace(extraction_scope_policy="whole")
    saved = {
        "_input_scopes": {"version": 2},
        "batch/0": {"request_key": "frozen", "result": {"entities": [], "relations": []}},
        "evidence-repair/batch/0": {"entities": [], "relations": []},
    }
    draft = baseline | {
        "scoped": True,
        "documents": {"doc": {"extraction_batches": saved}},
    }
    reused = retry_extraction_checkpoints(draft, baseline, "doc", settings)
    assert reused == {"frozen": {"entities": [], "relations": []}}
    assert retry_extraction_checkpoints(draft, baseline | {"model": {"revision": 2}}, "doc", settings) == {}
    assert (
        retry_extraction_checkpoints(
            draft,
            baseline,
            "doc",
            SimpleNamespace(extraction_scope_policy="structure"),
        )
        == {}
    )


async def _check_retry_response_binding():
    model = BusinessModel.model_validate(
        {
            "name": "retry",
            "node_types": {
                "Clause": {
                    "identity": {"namespace": "clause", "fields": ["heading"]},
                    "properties": {"heading": {"type": "string", "required": True}},
                }
            },
        }
    )
    parser = Parser(SimpleNamespace(chunk_target=100, chunk_max=150, chunk_overlap=10))

    def source():
        return {
            "chunks": parser._chunk_text(
                "The supplier provides the service.", {"format": "txt"}, str(uuid4()), []
            ),
        }

    class Language:
        def __init__(self):
            self.calls = 0
            self.profile = SimpleNamespace(max_input_chars=100000, model_dump=lambda **_: {"model": "fixed"})

        def json_request_chars(self, *_):
            return 100

        async def json(self, _system, request, _schema):
            self.calls += 1
            return {
                "entities": [
                    {
                        "ref": "e1",
                        "type": "Clause",
                        "properties": {"heading": "supplier"},
                        "evidence": {
                            "heading": [
                                {"unit_key": request["rule_units"][0]["unit_key"], "quote": "supplier"}
                            ]
                        },
                    }
                ],
                "relations": [],
            }

    async def persist():
        return None

    first, checkpoint = Language(), {}
    first_source = source()
    await extract_text(first, first_source, model, checkpoint, persist)
    assert first.calls == 1
    draft = {
        "job_id": "job",
        "operation": "IMPORT",
        "model": model.model_dump(),
        "scoped": True,
        "documents": {"doc": {"extraction_batches": checkpoint}},
    }
    settings = SimpleNamespace(extraction_scope_policy="whole")
    reuse = retry_extraction_checkpoints(
        draft, {key: draft[key] for key in ("job_id", "operation", "model")}, "doc", settings
    )
    second, replay = Language(), {}
    second_source = source()
    extracted = await extract_text(second, second_source, model, replay, persist, reuse=reuse)
    assert second.calls == 0 and replay["batch/0"]["reused"] is True
    assert first_source["chunks"][0]["chunk_id"] != second_source["chunks"][0]["chunk_id"]
    assert extracted["entities"][0]["input_chunk_ids"] == [second_source["chunks"][0]["chunk_id"]]
    assert extracted["entities"][0]["evidence"]["heading"] == [
        {"unit_key": second_source["rule_units"][0]["unit_key"], "quote": "supplier"}
    ]


async def _check_dependency_closure():
    def anchor(name, document):
        identity = {"namespace": "service", "fields": {"app_id": name}}
        return {
            "contribution_id": f"anchor-{name}-{document}",
            "document_id": document,
            "target_kind": "entity",
            "target_id": name,
            "field_path": "$exists",
            "value": {"identity": identity},
            "claim": {},
        }

    a, b = anchor("A", "doc-a"), anchor("B", "doc-b")
    dependencies = [
        {"target_id": row["target_id"], "identity_key": canonical(row["value"]["identity"])} for row in (a, b)
    ]
    old = {
        "contribution_id": "old-relation",
        "document_id": "doc-delete",
        "target_kind": "relation",
        "target_id": "R",
        "field_path": "$relation",
        "claim": {},
    }
    dependent = old | {
        "contribution_id": "dependent-relation",
        "document_id": "doc-dependent",
        "claim": {"identity_dependencies": dependencies},
    }

    class Metadata:
        def __init__(self, rows):
            self.rows = rows

        async def target_contributions(self, _kb_id, targets, *, active_only):
            assert active_only
            return [row for row in self.rows if (row["target_kind"], row["target_id"]) in targets]

    selected, rows = await dependency_target_closure(
        Metadata([old, dependent, a, b]),
        "kb",
        {("relation", "R")},
        [old],
        "doc-delete",
        20,
    )
    assert selected == {("relation", "R"), ("entity", "A"), ("entity", "B")}
    active, suppressed = effective_contributions([row for row in rows if row["document_id"] != "doc-delete"])
    assert dependent in active and not suppressed

    # The last A anchor goes away; B remains in the fixed scope, but cannot keep R alive alone.
    selected, rows = await dependency_target_closure(
        Metadata([dependent, a, b]),
        "kb",
        {("entity", "A"), ("relation", "R")},
        [a],
        "doc-a",
        20,
    )
    assert ("entity", "B") in selected
    active, suppressed = effective_contributions([row for row in rows if row["document_id"] != "doc-a"])
    assert dependent in suppressed and dependent not in active

    replacement = anchor("A", "replacement")
    selected, rows = await dependency_target_closure(
        Metadata([dependent, a, b, replacement]),
        "kb",
        {("entity", "A"), ("relation", "R")},
        [a],
        "doc-a",
        20,
    )
    active, suppressed = effective_contributions([row for row in rows if row["document_id"] != "doc-a"])
    assert dependent in active and not suppressed

    invalid = deepcopy(a)
    invalid["claim"] = {"identity_dependencies": dependencies}
    try:
        effective_contributions([invalid, b])
    except KGError as exc:
        assert exc.code == "INVALID_CONTRIBUTION"
    else:
        raise AssertionError("An identity anchor depended on another identity")


async def _check_scoped_graph_warning():
    kb_id, entity_id = str(uuid4()), str(uuid4())
    model = BusinessModel.model_validate(_model("app_id"))

    class Probe(GraphMixin):
        def __init__(self):
            self.r = SimpleNamespace(
                graph=SimpleNamespace(get=self._get),
                settings=SimpleNamespace(graph_timeout=3.0, retrieval_timeout=5.0),
            )

        async def _get(self, *_):
            return {"entity_id": entity_id}

        def validate_filters(self, *_):
            return None

        def _check_size(self, *_):
            return None

        async def _attach_evidence(self, *_):
            return None

    request = GraphRequest.model_validate(
        {"start_ids": [entity_id], "filters": {"document_ids": [str(uuid4())]}}
    )
    result = await Probe()._graph(
        {"kb_id": kb_id, "model": model.model_dump()},
        request,
        scope={"entity": {}, "relation": {}, "suppressed_dependencies": 1},
    )
    assert any(item["code"] == "IDENTITY_DEPENDENCY_OUT_OF_SCOPE" for item in result["warnings"])


async def _check_partial_atomic_assembly():
    model_data = {
        "name": "partial rule",
        "node_types": {
            "Clause": {
                "identity": {"namespace": "clause", "fields": ["heading"], "scope": "document"},
                "properties": {
                    "heading": {"type": "string", "required": True},
                    "definition": {"type": "string", "source_mode": "literal"},
                },
                "atomic_groups": [{"name": "full_statement", "fields": ["definition"]}],
            }
        },
    }
    model = BusinessModel.model_validate(model_data)
    parser = Parser(SimpleNamespace(chunk_target=60, chunk_max=80, chunk_overlap=10))
    chunks = parser._chunk_text(
        "Termination clause: either party may terminate upon thirty days' written notice.",
        {"format": "txt"},
        str(uuid4()),
        [],
    )
    parsed = {"chunks": chunks}
    parsed["rule_units"] = build_rule_context(parsed)
    unit = parsed["rule_units"][0]["unit_key"]
    text = parsed["rule_units"][0]["text"]
    start = text.index("either party")
    end = text.index("notice.") + len("notice.")
    try:
        resolve_literal(
            [{"unit_key": unit, "start": start, "end": text.index("written") + 3}],
            {unit: parsed["rule_units"][0]},
            {chunk["chunk_id"]: chunk for chunk in chunks},
            [unit],
        )
    except KGError as exc:
        assert exc.code == "LITERAL_BOUNDARY_UNRESOLVED"
    else:
        raise AssertionError("A range ending inside a source word was accepted")
    original = {
        "ref": "batch/0/e1",
        "type": "Clause",
        "properties": {"heading": "Termination clause"},
        "evidence": {"heading": []},
        "atomic_groups": {
            "full_statement": {
                "fields": {"definition": "either party may terminate upon thirty days' written"},
                "evidence": {"definition": []},
                "complete": True,
                "segment_complete": False,
                "section_key": "Termination heading",
                "rule_units": [unit],
                "literal_parts": {"definition": [{"unit_key": unit, "start": start, "end": end - 8}]},
            }
        },
    }
    marked = _mark_partial_source(
        original,
        {"unit_views": [{"unit_key": unit, "fully_supplied": False}]},
    )
    assert original["atomic_groups"]["full_statement"]["complete"] is True
    assert marked["atomic_groups"]["full_statement"]["complete"] is False

    class Language:
        def __init__(self, complete, *, invalid=False, extra_unit=False):
            self.complete, self.invalid, self.extra_unit, self.calls = complete, invalid, extra_unit, 0
            self.profile = SimpleNamespace(max_input_chars=100000, model_dump=lambda **_: {"model": "test"})

        def json_request_chars(self, *_):
            return 100

        async def json(self, _system, request, _schema):
            self.calls += 1
            assert len(request["chunks"]) == len(chunks)
            if not self.complete:
                return {"entities": [], "relations": []}
            return {
                "entities": [
                    {
                        "ref": "assembled",
                        "type": "Clause",
                        "properties": {"heading": "Termination clause"},
                        "evidence": {},
                        "atomic_groups": {
                            "full_statement": {
                                "fields": {
                                    "definition": "either party may terminate upon thirty days' written notice."
                                },
                                "evidence": {"definition": []},
                                "complete": True,
                                "section_key": unit,
                                "rule_units": [unit, "rule-unit/999"] if self.extra_unit else [unit],
                                "literal_parts": {
                                    "definition": [
                                        {
                                            "unit_key": unit,
                                            "start": start,
                                            "end": end + 1 if self.invalid else end,
                                        }
                                    ]
                                },
                            }
                        },
                    }
                ],
                "relations": [],
            }

    async def persist():
        return None

    for complete, invalid, extra_unit in (
        (True, False, False),
        (False, False, False),
        (True, True, False),
        (True, False, True),
    ):
        language = Language(complete, invalid=invalid, extra_unit=extra_unit)
        extracted = {"entities": [deepcopy(marked)], "relations": []}
        await _assemble_rules(language, parsed, model, extracted, {}, persist)
        assert language.calls == 1
        assert extracted["entities"][0]["atomic_groups"]["full_statement"]["complete"] is (
            complete and not invalid and not extra_unit
        ), (complete, invalid, extra_unit, extracted.get("issues"), extracted["entities"][0]["atomic_groups"])
        assert any(
            issue["reason_code"] == "RULE_ASSEMBLY_UNRESOLVED" for issue in extracted.get("issues", [])
        ) is not (complete and not invalid and not extra_unit)

    # Same heading and source unit can contain two separate clauses. One model
    # output must never overwrite both declarations.
    first = deepcopy(marked)
    second = deepcopy(marked)
    second["ref"] = "batch/1/e1"
    second["atomic_groups"]["full_statement"]["literal_parts"] = {
        "definition": [{"unit_key": unit, "start": end - 9, "end": end}]
    }
    extracted = {"entities": [first, second], "relations": []}
    await _assemble_rules(Language(True), parsed, model, extracted, {}, persist)
    assert all(
        not candidate["atomic_groups"]["full_statement"]["complete"] for candidate in extracted["entities"]
    )
    assert extracted["issues"][0]["reason_code"] == "RULE_ASSEMBLY_UNRESOLVED"

    another = deepcopy(parsed["rule_units"][0])
    another["unit_key"], another["order"] = "rule-unit/1", 1
    two_units = {"chunks": chunks, "rule_units": [parsed["rule_units"][0], another]}
    spanning = deepcopy(marked)
    spanning["atomic_groups"]["full_statement"]["rule_units"] = [unit, another["unit_key"]]
    spanning["source_incomplete_unit_keys"] = [unit, another["unit_key"]]
    language = Language(True)
    await _assemble_rules(language, two_units, model, {"entities": [spanning], "relations": []}, {}, persist)
    assert language.calls == 1


async def check():
    _check_discriminator()
    _check_retry_extraction()
    await _check_retry_response_binding()
    await _check_dependency_closure()
    await _check_scoped_graph_warning()
    await _check_partial_atomic_assembly()
