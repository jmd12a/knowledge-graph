"""验证连续条款的同窗补抽、跨窗补读和证据授权。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from knowledge_graph.adapters.models.http import Language
from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import KGError
from knowledge_graph.domain.model import BusinessModel
from knowledge_graph.services.construction.alignment import align
from knowledge_graph.services.construction.extraction import build_rule_context, extract_text
from knowledge_graph.services.construction.extraction_review import ReviewBudget, _has_anchor, _locations
from knowledge_graph.settings import LanguageProfile, Settings

from .evidence import write_json

_DOCUMENT_ID = "vendor-payment-controls"
_CUTOFF = "4. PAYMENT CUTOFF. Vendor payment requests must reach Treasury by 17:00 local time."
_FREEZE = (
    "5. SECURITY FREEZE. Treasury must immediately stop every payment from a frozen vendor account, "
    "including before the cutoff."
)
_CHANGE = (
    "6. ACH ACCOUNT CHANGES. For U.S. ACH vendor bank-account changes exceeding USD 25,000, "
    "Treasury must follow the approval chain in Appendix B."
)
_APPROVAL = (
    "Appendix B. ACH APPROVAL CHAIN. The Treasury Controller and Fraud Operations Reviewer must both "
    "approve the change before activation. Finance Operations receives a copy but does not approve "
    "the change. This approval chain does not apply to EU SEPA."
)
_FOREIGN = (
    "Appendix C. FOREIGN PAYMENT CONTROLS. The Controller only receives notice after the transfer settles."
)
_HEADINGS = {
    _CUTOFF: "PAYMENT CUTOFF",
    _FREEZE: "SECURITY FREEZE",
    _CHANGE: "ACH ACCOUNT CHANGES",
    _APPROVAL: "ACH APPROVAL CHAIN",
    _FOREIGN: "FOREIGN PAYMENT CONTROLS",
}


def _inputs():
    model = BusinessModel.model_validate(
        {
            "name": "Vendor payment controls",
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
    parser = Parser(SimpleNamespace(chunk_max=360, chunk_target=180, chunk_overlap=0))
    chunks, offset = [], 0
    # 两个附录共用物理块，验证读到 B 不会同时授权 C。
    blocks = [_CUTOFF, _FREEZE, _CHANGE, _APPROVAL + "\n\n" + _FOREIGN]
    for order, block in enumerate(blocks):
        text = block + "\n\n"
        chunks.extend(
            parser._chunk_text(
                text,
                {"format": "txt"},
                _DOCUMENT_ID,
                [],
                overlap=False,
                char_offset=offset,
                line_offset=order * 2,
                block_order=order,
            )
        )
        offset += len(text)
    parsed = {"chunks": chunks}
    parsed["rule_units"] = build_rule_context(parsed, structured=True)
    return model, parsed


def _clause(unit_key, text):
    heading = _HEADINGS[text]
    return {
        "ref": heading,
        "type": "Clause",
        "properties": {"heading": heading},
        "evidence": {"heading": [{"unit_key": unit_key, "quote": heading}]},
        "atomic_groups": {
            "full_statement": {
                "fields": {},
                "evidence": {},
                "literal_parts": {"definition": [{"unit_key": unit_key, "quote": text}]},
                "section_key": unit_key,
                "rule_units": [unit_key],
                "complete": True,
                "references_resolved": text != _CHANGE,
            }
        },
    }


def _texts(views):
    return {
        "".join(part["text"] for part in view["visible_parts"]).strip(): view["unit_key"] for view in views
    }


class _Language:
    profile = LanguageProfile(max_input_chars=30_000, model="test")

    def __init__(self, rules, *, forged=False, wrong_primary=False):
        self.keys = {unit["text"].strip(): unit["unit_key"] for unit in rules}
        self.primary = []
        self.reviews = []
        self.actions = []
        self.forged = forged
        self.wrong_primary = wrong_primary

    def json_request_chars(self, *_):
        return 10

    async def json(self, _, request, _schema):
        if "tools" in request:
            return self._reference(request)
        texts = _texts(request["rule_units"])
        if "review_units" in request:
            self.reviews.append(texts)
            assert request["draft_candidates"]
            entities = [_clause(texts[_FREEZE], _FREEZE)] if _FREEZE in texts else []
        else:
            self.primary.append(list(texts))
            entities = [_clause(unit, text) for text, unit in texts.items() if text != _FREEZE]
            for entity in entities:
                # 真实模型可能把原子组的逐字值同时放在实体属性里。
                if entity["ref"] in {_HEADINGS[_APPROVAL], _HEADINGS[_FOREIGN]}:
                    source = next(text for text, heading in _HEADINGS.items() if heading == entity["ref"])
                    entity["properties"]["definition"] = source
            if self.wrong_primary and _CHANGE in texts:
                rule = next(item for item in entities if item["ref"] == _HEADINGS[_CHANGE])
                statement = rule["atomic_groups"]["full_statement"]
                statement["references_resolved"] = True
                statement["resolved_references"] = [
                    {
                        "reference": [{"unit_key": texts[_CHANGE], "quote": "Appendix B"}],
                        "target": [{"unit_key": texts[_CUTOFF], "quote": _CUTOFF}],
                    }
                ]
        return {"entities": entities, "relations": []}

    def _reference(self, request):
        tools = request["tools"]
        assert request["task"]["kind"] == "reference"
        source = _texts(request["source_rule_units"])
        assert _CHANGE in source
        assert _APPROVAL not in source and _FOREIGN not in source
        if not self.actions:
            assert "read_source_blocks" in tools
            assert self.keys[_APPROVAL] in {item["unit_key"] for item in request["search_results"]}
            self.actions.append("read_source_blocks")
            return {"action": "read_source_blocks", "unit_keys": [self.keys[_APPROVAL]]}
        if self.actions[-1] == "read_source_blocks":
            read = _texts(request["read_rule_units"])
            assert _APPROVAL in read and _FOREIGN not in read
            target = _FOREIGN if self.forged else _APPROVAL
            self.actions.append("submit")
            return {
                "action": "submit",
                "references": [
                    {
                        "reference": [{"unit_key": source[_CHANGE], "quote": "Appendix B"}],
                        "target": [{"unit_key": self.keys[target], "quote": target}],
                    }
                ],
            }
        self.actions.append("stop")
        return {"action": "stop"}


async def _extract(language, model, parsed, checkpoint, *, loop_calls=0, reuse=None):
    async def persist():
        return None

    return await extract_text(
        language,
        parsed,
        model,
        checkpoint,
        persist,
        scope_policy="structure",
        structure_window_chars=360,
        loop_calls=loop_calls,
        reuse=reuse,
    )


async def _adopt(model, parsed, extraction):
    rows, issues = await align(
        {"document_id": _DOCUMENT_ID, "kb_id": "kb", "source": {}},
        extraction,
        {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]},
        model,
        {},
        "vendor-payment-import",
        [],
        [],
        parsed["rule_units"],
    )
    statements = [row["value"] for row in rows if row["field_path"] == "/atomic_groups/full_statement"]
    return statements, issues


async def _build(model, parsed, *, enabled=False, forged=False, wrong_primary=False):
    parsed = deepcopy(parsed)
    language = _Language(parsed["rule_units"], forged=forged, wrong_primary=wrong_primary)
    extraction = await _extract(language, model, parsed, {}, loop_calls=8 if enabled else 0)
    statements, issues = await _adopt(model, parsed, extraction)
    assert not issues, issues
    assert language.primary == [[_CUTOFF, _FREEZE, _CHANGE], [_APPROVAL, _FOREIGN]], language.primary
    return {statement["fields"]["definition"].strip(): statement for statement in statements}, language


async def check():
    model, parsed = _inputs()
    baseline, baseline_calls = await _build(model, parsed)
    assert set(baseline) == {_CUTOFF, _CHANGE, _APPROVAL, _FOREIGN}
    assert not baseline[_CHANGE]["references_resolved"] and not baseline_calls.actions
    assert not baseline_calls.reviews

    optimized, optimized_calls = await _build(model, parsed, enabled=True)
    assert set(optimized) == set(baseline) | {_FREEZE}
    assert len(optimized_calls.reviews) == 1
    assert optimized_calls.actions == ["read_source_blocks", "submit"]
    assert optimized[_CHANGE]["references_resolved"] is True
    [reference] = optimized[_CHANGE]["resolved_references"]
    assert [item["quote"] for item in reference["reference"]] == ["Appendix B"]
    assert [item["quote"] for item in reference["target"]] == [_APPROVAL]
    assert all(optimized[text] == baseline[text] for text in baseline if text != _CHANGE)

    rejected, _ = await _build(model, parsed, enabled=True, forged=True)
    assert rejected[_CHANGE]["resolved_references"] == []
    assert rejected[_CHANGE]["references_resolved"] is False

    corrected, _ = await _build(model, parsed, enabled=True, wrong_primary=True)
    [corrected_reference] = corrected[_CHANGE]["resolved_references"]
    assert [item["quote"] for item in corrected_reference["target"]] == [_APPROVAL]
    heading_catalog = {
        "right": {"text": "7. INCIDENT ESCALATION."},
        "wrong": {"text": "Section 7.1. AFTER HOURS."},
    }
    assert [item["unit_key"] for item in _locations(heading_catalog, "Section 7", [])] == ["right"]
    assert not _has_anchor("Follow Section 7.1", "Section 7")
    await _check_physical_budget()


async def _check_physical_budget():
    checkpoint, issues, sent = {}, [], []

    async def persist():
        return None

    async def respond(_request):
        sent.append(checkpoint["_review"]["used"])
        return httpx.Response(503, json={"error": {"message": "Retry"}})

    language = Language(Settings(llm=LanguageProfile(api_key_env="KG_REVIEW_TEST_CREDENTIAL")))
    await language.client.aclose()
    language.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with patch.dict("os.environ", {"KG_REVIEW_TEST_CREDENTIAL": "test"}):
            budget = ReviewBudget(checkpoint, 2, persist, issues)
            assert await budget.call(language, "review", "Return JSON.", {}, None) is None
            resumed = ReviewBudget(deepcopy(checkpoint), 2, persist, issues)
            assert await resumed.call(language, "review", "Return JSON.", {}, None) is None
    finally:
        await language.close()
    assert sent == [1, 2]
    assert {item["reason_code"] for item in issues} == {"CONSTRUCTION_REVIEW_BUDGET"}


class _MeasuredLanguage(Language):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []
        self.client.event_hooks["request"].append(self._count_attempt)

    async def _count_attempt(self, _request):
        self.calls[-1]["http_attempts"] += 1

    async def json(self, system, data, schema=None):
        stage = next(
            (
                name
                for key, name in (
                    ("review_units", "window_review"),
                    ("tools", "document_review"),
                    ("tasks", "grounding"),
                    ("allowed_pairs", "cross_window_links"),
                )
                if key in data
            ),
            "primary",
        )
        record = {"stage": stage, "http_attempts": 0, "outcome": "completed"}
        self.calls.append(record)
        started = perf_counter()
        try:
            return await super().json(system, data, schema)
        except KGError as exc:
            record["outcome"] = exc.code
            raise
        finally:
            record["seconds"] = round(perf_counter() - started, 3)


def _score(statements, issues):
    bodies = {source: source.split(heading + ". ", 1)[-1] for source, heading in _HEADINGS.items()}
    full_clauses = Counter(statement["fields"]["definition"].strip() for statement in statements)
    clauses = Counter()
    for text, count in full_clauses.items():
        clauses[bodies.get(text, text)] += count
    expected_bodies = set(bodies.values())
    correct, incorrect, role_evidence = 0, 0, set()
    target_start = sum(len(text) + 2 for text in (_CUTOFF, _FREEZE, _CHANGE))
    target_end = target_start + len(_APPROVAL) + 2
    for statement in statements:
        for reference in statement["resolved_references"]:
            ranges = [
                span["origin"]["char_range"] for item in reference["target"] for span in item["source_spans"]
            ]
            if (
                bodies.get(
                    statement["fields"]["definition"].strip(), statement["fields"]["definition"].strip()
                )
                == bodies[_CHANGE]
                and any("Appendix B" in item["quote"] for item in reference["reference"])
                and ranges
                and all(target_start <= left < right <= target_end for left, right in ranges)
            ):
                correct += 1
                target_text = " ".join(item["quote"] for item in reference["target"])
                role_evidence.update(
                    role
                    for role in ("Treasury Controller", "Fraud Operations Reviewer")
                    if role in target_text
                )
            else:
                incorrect += 1
    return {
        "exact_clause_bodies": {
            "correct": len(set(clauses) & expected_bodies),
            "expected": len(_HEADINGS),
            "by_clause": {heading: clauses[bodies[source]] for source, heading in _HEADINGS.items()},
            "unexpected": sum(count for body, count in clauses.items() if body not in expected_bodies),
            "duplicates": sum(count - 1 for count in clauses.values()),
        },
        "complete_source_blocks": {
            "correct": len(set(full_clauses) & _HEADINGS.keys()),
            "expected": len(_HEADINGS),
        },
        "cross_window_references": {
            "correct": min(correct, 1),
            "expected": 1,
            "duplicate": max(correct - 1, 0),
            "incorrect": incorrect,
            "approval_roles_cited": sorted(role_evidence),
        },
        "issue_counts": dict(Counter(issue["reason_code"] for issue in issues)),
    }


async def _live(config):
    settings = Settings.load(config)
    settings = settings.model_copy(update={"llm": settings.construction_llm or settings.llm})
    model, parsed = _inputs()
    records = {}
    report = {"model": settings.llm.model, "fixture": _DOCUMENT_ID, "modes": records}
    checkpoint, reuse = {}, {}
    for name, limit in (("baseline", 0), ("loop", 8)):
        if limit:
            # 固定原始首抽结果，包括没有进入生产复用池的结果。两组分别验证和对齐。
            checkpoint = deepcopy(
                {
                    key: value
                    for key, value in checkpoint.items()
                    if key == "_input_scopes" or key.startswith("batch/")
                }
            )
            checkpoint["_input_scopes"]["review_limit"] = limit
        frozen = sum(key.startswith("batch/") for key in checkpoint)
        language = _MeasuredLanguage(settings)
        started = perf_counter()
        result = {"frozen_primary_windows": frozen}
        records[name] = result
        try:
            extraction = await _extract(
                language, model, deepcopy(parsed), checkpoint, loop_calls=limit, reuse=reuse
            )
            statements, issues = await _adopt(model, parsed, extraction)
            result.update(status="completed", **_score(statements, [*extraction["issues"], *issues]))
        except KGError as exc:
            result.update(status="failed", reason_code=exc.code)
        finally:
            await language.close()
            result.update(
                model_calls=len(language.calls),
                http_attempts=sum(call["http_attempts"] for call in language.calls),
                elapsed_seconds=round(perf_counter() - started, 3),
                calls=language.calls,
            )
        if result["status"] == "failed":
            break
    return report


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="用配置的模型执行固定首抽 A/B。")
    parser.add_argument("--config", help="模型配置文件。")
    parser.add_argument("--output", type=Path, help="保存真实模型 A/B 的 JSON 结果。")
    args = parser.parse_args()
    if args.live:
        if not args.config:
            parser.error("--live 需要指定 --config")
        report = asyncio.run(_live(args.config))
        if args.output:
            write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False))
        if any(result["status"] != "completed" for result in report["modes"].values()):
            raise SystemExit(1)
    else:
        if args.config or args.output:
            parser.error("--config 和 --output 需要 --live")
        asyncio.run(check())
        print("通过构建 Loop 的基线、补抽、跨窗补读和证据授权检查。")


if __name__ == "__main__":
    _main()
