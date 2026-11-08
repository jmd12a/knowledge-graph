"""离线验证结构窗口、证据补丁和恢复。"""

from __future__ import annotations

import json
from copy import deepcopy
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from knowledge_graph.adapters.models.http import Language as HttpLanguage
from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import KGError
from knowledge_graph.domain.model import BusinessModel
from knowledge_graph.services.construction.alignment import align
from knowledge_graph.services.construction.extraction import _plan_scopes, build_rule_context, extract_text
from knowledge_graph.services.construction.extraction_windows import visible_source_chars
from knowledge_graph.settings import Settings


async def _check_structure_extraction():
    parser = Parser(SimpleNamespace(chunk_max=50, chunk_target=30, chunk_overlap=10))
    assert Settings(extraction_scope_policy="structure").extraction_scope_policy == "structure"
    text = (
        "8. CONFIDENTIALITY. The parties keep records private. "
        "9. INDEPENDENT CONTRACTORS. Neither party is the other's agent. "
        "10. MISCELLANEOUS a. Insurance. Each party carries insurance."
    )
    chunks = parser._chunk_text(text, {"format": "txt"}, "doc-structure", [])
    model = BusinessModel.model_validate(
        {
            "name": "Structure regression",
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

    class Language:
        profile = SimpleNamespace(max_input_chars=100000, model_dump=lambda **_: {"model": "test"})
        calls = 0

        def json_request_chars(self, *_):
            return 100

        async def json(self, _, request, _schema):
            self.calls += 1
            assert "chunks" not in request
            views = request["rule_units"]
            assert len(views) == 4 and all(view["fully_supplied"] for view in views)
            assert [view["visible_parts"][0]["text"].split(". ", 1)[0] for view in views] == [
                "8",
                "9",
                "10",
                "a",
            ]
            key = views[1]["unit_key"]
            return {
                "entities": [
                    {
                        "ref": "e1",
                        "type": "Clause",
                        "properties": {"heading": "INDEPENDENT CONTRACTORS"},
                        "evidence": {"heading": [{"unit_key": key, "quote": "INDEPENDENT CONTRACTORS"}]},
                        "atomic_groups": {
                            "full_statement": {
                                "fields": {},
                                "evidence": {},
                                "literal_parts": {"definition": [{"unit_key": key}]},
                                "section_key": key,
                                "rule_units": [key],
                                "complete": True,
                            }
                        },
                    }
                ],
                "relations": [],
            }

    async def persist():
        return None

    language, checkpoint = Language(), {}
    result = await extract_text(
        language, {"chunks": chunks}, model, checkpoint, persist, scope_policy="structure"
    )
    declaration = result["entities"][0]["atomic_groups"]["full_statement"]
    assert declaration["fields"]["definition"] == (
        "9. INDEPENDENT CONTRACTORS. Neither party is the other's agent. "
    )
    assert declaration["literal_provenance"]["definition"]["fidelity"] == "source_exact"
    assert declaration["complete"] and language.calls == 1


def _clause(ref, name, quote, *, heading_evidence=True):
    return {
        "ref": ref,
        "type": "Clause",
        "properties": {"heading": name},
        "evidence": {"heading": [{"unit_key": "unit/0", "quote": name}]} if heading_evidence else {},
        "atomic_groups": {
            "full_statement": {
                "fields": {},
                "evidence": {},
                "literal_parts": {"definition": [{"unit_key": "unit/0", "quote": quote}]},
                "section_key": "unit/0",
                "rule_units": ["unit/0"],
                "complete": True,
            }
        },
    }


async def _check_recovery_and_patches():
    model = BusinessModel.model_validate(
        {
            "name": "Structure patches",
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
    complete = "Records must be retained."
    partial = "Payment is due on Fri"
    exception = "day. Except during a declared outage."
    beginning = complete.ljust(100 - len(partial)) + partial
    text = beginning + exception + " " + "Another unrelated statement. " * 3
    parser = Parser(SimpleNamespace(chunk_max=100, chunk_target=60, chunk_overlap=15))
    chunks = parser._chunk_text(text, {"format": "txt"}, "doc-patches", [])
    assert len(chunks) == 3
    empty = {"entities": [], "relations": []}

    class Language:
        profile = SimpleNamespace(max_input_chars=1000, model_dump=lambda **_: {"model": "test"})

        def __init__(self, *, shift=False, interrupt=False):
            self.calls = []
            self.shift = shift
            self.interrupt = interrupt

        def json_request_chars(self, *_):
            return 10

        async def json(self, system, request, _schema):
            view = request["rule_units"][0]
            visible = view["visible_parts"][0]["text"]
            if system.startswith("Correct the evidence"):
                self.calls.append("repair")
                heading, body = request["tasks"]
                assert heading["field"] == "entity keep.heading" and heading["value"] == "Records"
                assert body["field"] == "entity keep.full_statement.definition" and body["literal"]
                return {
                    "patches": [
                        {"task_id": 0, "selections": [{"unit_key": "unit/0", "quote": "Records"}]},
                        {"task_id": 1, "selections": [{"unit_key": "unit/0", "quote": complete}]},
                        {"task_id": 9, "selections": [{"unit_key": "unit/0", "quote": "Payment"}]},
                    ]
                }
            if system.startswith("Complete only"):
                self.calls.append("completion")
                assert exception in visible and len(visible) < len(text)
                assert len(request["tasks"]) == 2
                selected = partial + exception if not self.shift else exception.strip()
                return {
                    "patches": [
                        {
                            "task_id": i,
                            "complete": True,
                            "selections": [{"unit_key": "unit/0", "quote": selected}],
                        }
                        for i in range(2)
                    ]
                }
            self.calls.append("primary")
            if len(visible) > 100:
                raise KGError("MODEL_OUTPUT_TOO_LARGE", "Test truncation")
            if visible.startswith(complete):
                entities = [
                    _clause("keep", "Records", "unsupported paraphrase", heading_evidence=False),
                    _clause("extend", "Payment", partial),
                    _clause("duplicate", "Payment", partial),
                ]
                for entity in entities[1:]:
                    entity["atomic_groups"]["full_statement"]["resolved_references"] = [
                        {
                            "reference": [{"unit_key": "unit/0", "quote": "Payment"}],
                            "target": [{"unit_key": "unit/0", "quote": "declared outage"}],
                        }
                    ]
                return {"entities": entities, "relations": []}
            if self.interrupt:
                raise KGError("TIMEOUT", "Test interruption", retryable=True)
            return deepcopy(empty)

    checkpoint = {}
    parsed = {"chunks": chunks}
    persisted = []

    async def persist():
        persisted.append(deepcopy(checkpoint))

    async def run(language, *, target=1000):
        return await extract_text(
            language,
            parsed,
            model,
            checkpoint,
            persist,
            scope_policy="structure",
            structure_window_chars=target,
        )

    interrupted = Language(interrupt=True)
    try:
        await run(interrupted)
    except KGError as exc:
        assert exc.code == "TIMEOUT"
    else:
        raise AssertionError("A transient failure was treated as successful extraction")
    assert interrupted.calls == ["primary", "primary", "repair", "primary", "primary"], interrupted.calls
    assert any(
        [scope["key"] for scope in item["_input_scopes"]["batches"]] == ["batch/0.0", "batch/0.1"]
        for item in persisted
    )

    saved = json.loads(json.dumps({"parsed": parsed, "extraction_batches": checkpoint}))
    parsed = saved["parsed"]
    checkpoint = saved["extraction_batches"]
    resumed = Language()
    with patch(
        "knowledge_graph.services.construction.extraction.build_rule_context",
        side_effect=AssertionError("Recovery must keep the persisted source units"),
    ):
        output = await run(resumed, target=1)
    assert resumed.calls == ["primary", "primary", "completion"], resumed.calls
    declarations = [candidate["atomic_groups"]["full_statement"] for candidate in output["entities"]]
    assert all(declaration["complete"] for declaration in declarations)
    assert [declaration["fields"]["definition"] for declaration in declarations] == [
        complete,
        partial + exception,
        partial + exception,
    ]
    assert output["entities"][0]["evidence"]["heading"][0]["quote"] == "Records"
    assert output["entities"][0]["properties"] == {"heading": "Records"}
    assert not output["issues"]

    contributions, issues = await align(
        {"document_id": "doc-patches", "kb_id": "kb", "source": {}},
        deepcopy(output),
        {chunk["chunk_id"]: chunk for chunk in chunks},
        model,
        {},
        "job-patches",
        [],
        [],
        build_rule_context({"chunks": chunks}, structured=True),
    )
    assert not issues
    statements = {
        row["value"]["fields"]["definition"]: row["value"]
        for row in contributions
        if row["field_path"] == "/atomic_groups/full_statement"
    }
    assert set(statements) == {complete, partial + exception}
    assert sum(row["field_path"] == "/atomic_groups/full_statement" for row in contributions) == 2
    extended = statements[partial + exception]
    assert not extended["resolved_references"]
    assert extended["completeness_check"]["rejected_optional_reference_count"] == 1

    replay = Language()
    assert await run(replay) == output
    assert not replay.calls

    checkpoint.clear()
    invalid = Language(shift=True)
    output = await run(invalid)
    declarations = [candidate["atomic_groups"]["full_statement"] for candidate in output["entities"]]
    assert declarations[0]["complete"]
    assert all(not declaration["complete"] for declaration in declarations[1:])
    assert [declaration["fields"]["definition"] for declaration in declarations[1:]] == [partial, partial]
    assert [issue["reason_code"] for issue in output["issues"]] == ["RULE_ASSEMBLY_UNRESOLVED"] * 2


class WindowBudget:
    def __init__(self, hard_limit: int):
        self.profile = SimpleNamespace(max_input_chars=hard_limit)

    def json_request_chars(self, _, request, _schema):
        return visible_source_chars({"unit_views": request["rule_units"]})


def _structure_plan(parsed, units, target, hard_limit=1000):
    chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
    scopes = _plan_scopes(
        WindowBudget(hard_limit),
        chunks,
        units,
        {},
        {},
        scope_policy="structure",
        structure_window_chars=target,
    )
    assert {key for scope in scopes for key in scope["input_chunk_ids"]} == chunks.keys()
    visible = {unit["unit_key"]: set() for unit in units}
    for scope in scopes:
        assert visible_source_chars(scope) <= hard_limit
        for view in scope["unit_views"]:
            for part in view["visible_parts"]:
                visible[view["unit_key"]].update(range(*part["unit_range"]))
    assert all(visible[unit["unit_key"]] == set(range(len(unit["text"]))) for unit in units)
    return scopes


def _write_text_pdf(path: Path, text: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=1000, height=150)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def _check_format_boundaries():
    paragraph = (
        "The limit is 3.14 V. Consult U.S. guidance. "
        "2.1. STARTUP, STOP; AND RESET: Apply power. "
        "(b) Recovery Procedure. Restart carefully. "
        "c. Operators restart after inspection."
    )
    expected_starts = (
        "The limit is",
        "2.1. STARTUP, STOP; AND RESET:",
        "(b) Recovery Procedure.",
    )
    parser = Parser(SimpleNamespace(chunk_max=92, chunk_target=72, chunk_overlap=13))
    model = BusinessModel.model_validate(
        {
            "name": "Window test",
            "node_types": {
                "Item": {
                    "identity": {"namespace": "item", "fields": ["name"]},
                    "properties": {"name": {"type": "string"}},
                }
            },
        }
    )

    with TemporaryDirectory() as directory:
        root = Path(directory)
        markdown = (
            "# Operating Manual\nLead-in\n## Start and Stop\n"
            "Use the documented controls.\n## Emergency Procedure\nCall support.\n"
        )
        (root / "fixture.md").write_text(markdown, encoding="utf-8")
        word = Document()
        word.add_heading("Operating Manual", 1)
        word.add_heading("Start and Stop", 2)
        word.add_paragraph(paragraph)
        word.add_heading("Except when delegated", 2)
        word.add_paragraph("Escalate promptly.")
        cells = word.add_table(rows=1, cols=2).rows[0].cells
        cells[0].text = "Limits"
        cells[1].text = "See notes. b. Label Example. Sample value."
        word.save(root / "fixture.docx")
        _write_text_pdf(root / "fixture.pdf", paragraph)
        (root / "fixture.txt").write_text(paragraph, encoding="utf-8")

        for media in ("txt", "md", "docx", "pdf"):
            parsed = parser.parse(
                {
                    "raw_path": str(root / f"fixture.{media}"),
                    "media_type": media,
                    "source": {},
                    "document_id": f"doc-{media}",
                },
                model,
            )
            chunks = deepcopy(parsed["chunks"])
            units = build_rule_context(parsed, structured=True)
            _structure_plan(parsed, units, target=100)
            assert parsed["chunks"] == chunks
            bodies = [unit["text"] for unit in units]
            if media == "md":
                assert "".join(bodies) == markdown
                assert len(bodies) == 3
                assert bodies[1].startswith("## Start and Stop")
                assert bodies[2].startswith("## Emergency Procedure")
            else:
                paragraphs = [body for body in bodies if body.startswith(expected_starts)]
                assert tuple(body.split(" ", 1)[0] for body in paragraphs) == ("The", "2.1.", "(b)")
                assert paragraphs[0].endswith("Consult U.S. guidance. ")
                assert "c. Operators restart after inspection." in paragraphs[-1]
                if media == "docx":
                    assert "".join(paragraphs) == paragraph
                    assert "Except when delegated" in bodies
                    table = next(unit for unit in units if unit["text"] == cells[1].text)
                    assert len(table["regions"]) == 1
                    assert json.loads(table["regions"][0]["origin"])["column"] == 2
                else:
                    assert "".join(bodies).strip() == paragraph
    decimal = (
        "Observations.\n3.14 volts were measured.\n  3.2. VERIFIED SETTINGS. Use the documented value.\n"
        "  3.2.1 Access Requirements\nRecord access changes."
    )
    chunks = parser._chunk_text(decimal, {"format": "txt"}, "decimals", [])
    units = build_rule_context({"chunks": chunks}, structured=True)
    assert len(units) == 3
    assert "3.14 volts" in units[0]["text"] and units[1]["text"].startswith("  3.2.")
    assert units[2]["text"].startswith("  3.2.1 Access Requirements")


def _check_window_budgets():
    parser = Parser(SimpleNamespace(chunk_max=80, chunk_target=65, chunk_overlap=15))
    long_text = "Protocol observations continue without headings or explicit separators. " * 4
    chunks = parser._chunk_text(long_text, {"format": "txt"}, "doc-long", [])
    parsed = {"chunks": chunks}
    units = build_rule_context(parsed, structured=True)
    assert len(units) == 1
    assert sum(len(chunk["text"]) for chunk in chunks) > len(long_text)

    standalone = _structure_plan(parsed, units, target=50)
    assert len(standalone) == 1
    assert visible_source_chars(standalone[0]) == len(long_text)
    assert standalone[0]["unit_views"][0]["fully_supplied"]

    fallback = _structure_plan(parsed, units, target=50, hard_limit=100)
    assert len(fallback) > 1
    assert all(not scope["unit_views"][0]["fully_supplied"] for scope in fallback)
    try:
        _structure_plan(parsed, units, target=50, hard_limit=30)
    except KGError as exc:
        assert exc.code == "MODEL_INPUT_TOO_LARGE"
    else:
        raise AssertionError("An oversized physical chunk must still obey the hard model limit")

    text = "A sample paragraph contains source details.\n\n" * 5
    chunks = parser._chunk_text(text, {"format": "txt"}, "doc-multiple", [], overlap=False)
    parsed = {"chunks": chunks}
    units = build_rule_context(parsed, structured=True)
    narrow = _structure_plan(parsed, units, target=140)
    assert len(narrow) > 1 and all(visible_source_chars(scope) <= 140 for scope in narrow)
    assert len(_structure_plan(parsed, units, target=500)) == 1


async def _check_provider_size_errors():
    settings = Settings(llm={"api_key_env": "KG_TEST_MODEL_KEY", "base_url": "https://api.openai.com/v1"})
    language = HttpLanguage(settings)
    await language.close()
    for status, payload in (
        (413, {}),
        (400, {"error": {"code": "context_length_exceeded"}}),
        (200, {"error": {"code": "context_window_exceeded"}}),
    ):
        calls = []

        def respond(request):
            calls.append(request.url.path)
            body = json.loads(request.content)
            assert body["max_completion_tokens"] == language.profile.max_output_tokens
            assert "max_tokens" not in body
            return httpx.Response(status, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            language.client = client
            with patch.dict(environ, {"KG_TEST_MODEL_KEY": "test"}):
                try:
                    await language.json("system", {"text": "contract"})
                except KGError as exc:
                    assert exc.code == "MODEL_INPUT_TOO_LARGE" and not exc.retryable
                else:
                    raise AssertionError("Provider size error was treated as a valid response")
        assert len(calls) == 1


async def _check_structure_links():
    model = BusinessModel.model_validate(
        {
            "name": "Structure links",
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
    first = "Alice works with Bob.\n\n"
    second = "The employee named Robert also goes by Bob."
    parser = Parser(SimpleNamespace(chunk_max=23, chunk_target=23, chunk_overlap=0))
    chunks = parser._chunk_text(first + second, {"format": "txt"}, "doc-link", [])
    assert not any("Robert" in chunk["text"] for chunk in chunks)

    class Language:
        profile = SimpleNamespace(max_input_chars=1000, model_dump=lambda **_: {"model": "test"})

        def json_request_chars(self, _, request, _schema):
            return 10 if len(request["rule_units"]) == 1 else 2000

        async def json(self, _, request, _schema):
            assert "chunks" not in request
            assert request["rule_units"][0]["fully_supplied"]
            if request.get("allowed_pairs"):
                directory = {entry["name"]: entry["stable_id"] for entry in request["endpoint_catalog"]}
                return {
                    "entities": [],
                    "relations": [
                        {
                            "type": "LINKS",
                            "source_ref": directory["Alice"],
                            "target_ref": directory["Robert"],
                            "properties": {},
                            "evidence": [{"unit_key": "unit/0", "quote": "Alice works with Bob."}],
                        }
                    ],
                }
            visible = request["rule_units"][0]["visible_parts"][0]["text"]
            properties = {"name": "Alice"} if "Alice" in visible else {"name": "Robert", "aliases": "Bob"}
            return {
                "entities": [
                    {
                        "ref": "person",
                        "type": "Person",
                        "properties": properties,
                        "evidence": {
                            field: [{"unit_key": "unit/0", "quote": value}]
                            for field, value in properties.items()
                        },
                    }
                ],
                "relations": [],
            }

    async def persist():
        return None

    result = await extract_text(Language(), {"chunks": chunks}, model, {}, persist, scope_policy="structure")
    assert not result["issues"]
    assert len(result["relations"]) == 1
    names = {candidate["ref"]: candidate["properties"]["name"] for candidate in result["entities"]}
    relation = result["relations"][0]
    assert (names[relation["source_ref"]], names[relation["target_ref"]]) == ("Alice", "Robert")


async def check():
    _check_format_boundaries()
    _check_window_budgets()
    await _check_structure_extraction()
    await _check_recovery_and_patches()
    await _check_provider_size_errors()
    await _check_structure_links()
