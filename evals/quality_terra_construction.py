"""比较同一首抽上的跨窗引用和连续导入，记录每一步的图和证据。"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, defaultdict
from copy import deepcopy
from functools import partial
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import yaml
from dotenv import dotenv_values
from neo4j.exceptions import ServiceUnavailable

from knowledge_graph.adapters.models.http import Language as HttpLanguage
from knowledge_graph.adapters.neo4j.graph import Graph
from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import BusinessModel
from knowledge_graph.domain.source import effective_contributions
from knowledge_graph.services.construction.alignment import align
from knowledge_graph.services.construction.extraction import extract_text
from knowledge_graph.services.construction.extraction_identity_review import search_published_mentions
from knowledge_graph.services.construction.merge import aggregate
from knowledge_graph.settings import Settings

from .construction_metrics import coverage, precision_recall
from .evidence import ROOT, write_json

CORPUS = ROOT / "evals" / "corpus"
LOOP_CALLS = 12


class MeasuredLanguage(HttpLanguage):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []
        self.client.event_hooks["request"].append(self.count_attempt)

    async def count_attempt(self, _):
        self.calls[-1]["attempts"] += 1

    async def json(self, system, data, schema=None):
        step = next(
            (
                name
                for key, name in [
                    ("tools", "document_review"),
                    ("review_units", "window_review"),
                    ("allowed_pairs", "link"),
                    ("tasks", "grounding"),
                ]
                if key in data
            ),
            "primary",
        )
        self.calls.append({"stage": step, "attempts": 0})
        return await super().json(system, data, schema)

    async def _post(self, base_url, endpoint, key_env, body, timeout):
        response = await super()._post(base_url, endpoint, key_env, body, timeout)
        if "usage" in response:
            self.calls[-1].setdefault("usage", []).append(response["usage"])
        return response


def _frozen(checkpoint, loop_calls=LOOP_CALLS):
    copy = deepcopy(
        {
            key: value
            for key, value in checkpoint.items()
            if key == "_input_scopes" or key.startswith("batch/")
        }
    )
    copy["_input_scopes"]["review_limit"] = loop_calls
    reuse = {
        value["request_key"]: value["result"]
        for key, value in checkpoint.items()
        if key.startswith(("grounding/", "link/")) and "request_key" in value and "result" in value
    }
    return copy, reuse


async def _import(
    settings, doc, parsed, model, checkpoint, path, *, limit=0, reuse=None, graph=None, mention_search=None
):
    started = perf_counter()
    language = MeasuredLanguage(settings)

    async def persist():
        write_json(path, checkpoint)

    try:
        extracted = await extract_text(
            language,
            deepcopy(parsed),
            model,
            checkpoint,
            persist,
            scope_policy="structure",
            structure_window_chars=doc["window_chars"],
            loop_calls=limit,
            reuse=reuse,
            review_document=doc if graph else None,
            published_graph=graph,
            published_mention_search=mention_search,
        )
        rows, issues = await align(
            doc,
            extracted,
            {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]},
            model,
            {},
            str(uuid4()),
            [],
            [],
            parsed.get("rule_units") or extracted.get("rule_units"),
            graph=graph,
        )
        return rows, {
            "http_attempts": sum(call["attempts"] for call in language.calls),
            "frozen_responses_replayed": sum(call.get("replayed", 0) for call in language.calls),
            "primary_windows": sum(key.startswith("batch/") for key in checkpoint),
            "tokens": dict(
                sum(
                    (
                        Counter({k: usage.get(k, 0) for k in ("prompt_tokens", "completion_tokens")})
                        for call in language.calls
                        for usage in call.get("usage", [])
                    ),
                    Counter(),
                )
            ),
            "elapsed_seconds": round(perf_counter() - started, 3),
            "calls": language.calls,
            "issues": dict(Counter(item["reason_code"] for item in extracted["issues"] + issues)),
            "tools": dict(
                Counter(
                    value["result"]["action"]
                    for key, value in checkpoint.get("_review", {}).get("responses", {}).items()
                    if not key.startswith("window/")
                )
            ),
        }
    finally:
        await language.close()


def _document(path, window_chars):
    return {
        "kb_id": str(uuid4()),
        "document_id": str(uuid4()),
        "raw_path": str(path),
        "media_type": path.suffix[1:],
        "source": {"domain_ids": []},
        "window_chars": window_chars,
    }


def _parse(doc, model, chunk_target, chunk_max):
    from knowledge_graph.services.construction.extraction import build_rule_context

    parser = Parser(Settings(chunk_target=chunk_target, chunk_max=chunk_max, chunk_overlap=0))
    parsed = parser.parse(doc, model)
    parsed["rule_units"] = build_rule_context(parsed, structured=True)
    return parsed


def _char_ranges(evidence):
    return [
        tuple(span["origin"]["char_range"])
        for item in evidence
        for span in item["source_spans"]
        if "char_range" in span["origin"]
    ]


def _covers(spans, left, right):
    for start, end in sorted(spans):
        if start > left:
            return False
        left = max(left, end)
        if left >= right:
            return True
    return False


def _score_references(rows, gold, original):
    clauses = {item["heading"]: item["body"] for item in gold["clauses"]}
    body_spans = {
        heading: (original.index(body), original.index(body) + len(body)) for heading, body in clauses.items()
    }
    headings = {heading: (original.index(heading), end) for heading, (_, end) in body_spans.items()}
    expected = {item["source_heading"]: item for item in gold["references"]}
    absent = {item["source_heading"]: item for item in gold["unavailable_references"]}
    body_hits, references, false, covered = set(), {}, [], []
    unresolved_absent, source_context = set(), set()
    for row in rows:
        if not row["field_path"].startswith("/atomic_groups/"):
            continue
        statement = row["value"]
        text = statement["fields"]["definition"]
        body_hits.update(heading for heading, body in clauses.items() if body in text)
        statement_spans = _char_ranges(statement["field_evidence"]["definition"])
        covered.extend(statement_spans)
        context_spans = _char_ranges([item for item in row["evidence"] if item.get("role") == "context"])
        source_context.update(
            heading
            for heading, (left, right) in body_spans.items()
            if any(a < right and b > left for a, b in statement_spans) and _covers(context_spans, left, right)
        )
        if not statement["references_resolved"]:
            for heading, item in absent.items():
                start = original.index(item["source_quote"])
                if _covers(statement_spans, start, start + len(item["source_quote"])):
                    unresolved_absent.add(heading)
        for ref in statement["resolved_references"]:
            source_quotes = [key["quote"] for key in ref["reference"]]
            source_spans = _char_ranges(ref["reference"])
            spans = _char_ranges(ref["target"])
            source = next(
                (
                    heading
                    for heading, limits in headings.items()
                    if source_spans
                    and all(limits[0] <= left < right <= limits[1] for left, right in source_spans)
                ),
                None,
            )
            pair = expected.get(source)
            anchor_pattern = None if not pair else re.escape(pair["anchor"]) + r"(?![\w(]|[.-]\w)"
            if (
                not pair
                or not spans
                or not any(re.search(anchor_pattern, quote) for quote in source_quotes)
                or not all(
                    headings[pair["target_heading"]][0] <= left < right <= headings[pair["target_heading"]][1]
                    for left, right in spans
                )
            ):
                false.append({"source": source, "source_quotes": source_quotes, "target_spans": spans})
                continue
            keys = [
                quote
                for quote in pair["target_quotes"]
                if _covers(spans, original.index(quote), original.index(quote) + len(quote))
            ]
            references.setdefault(source, set()).update(keys)
    reference_hits = len(references)
    wrong_references = {
        (item["source"], tuple(item["source_quotes"]), tuple(item["target_spans"])) for item in false
    }
    target_evidence_hits = sum(map(len, references.values()))
    target_evidence_expected = sum(len(set(item["target_quotes"])) for item in expected.values())
    whitespace = [(match.start(), match.end()) for match in re.finditer(r"\s+", original)]
    body_content = sum(_covers(covered + whitespace, *span) for span in body_spans.values())
    return {
        "metrics": {
            "entities": None,
            "relations": precision_recall(
                reference_hits, reference_hits + len(wrong_references), len(expected)
            ),
            "source_evidence": coverage(target_evidence_hits, target_evidence_expected),
            "section_source_context": coverage(len(source_context), len(clauses)),
            "section_body_content": coverage(body_content, len(clauses)),
            "unavailable_references": coverage(len(unresolved_absent), len(absent)),
        },
        "body_hits": len(body_hits),
        "body_expected": len(clauses),
        "body_missing": sorted(set(clauses) - body_hits),
        "body_covered_across_facts": sum(_covers(covered, *span) for span in body_spans.values()),
        "correct_references": reference_hits,
        "expected_references": len(expected),
        "reference_evidence": {source: sorted(quotes) for source, quotes in references.items()},
        "reference_evidence_hits": target_evidence_hits,
        "reference_evidence_expected": target_evidence_expected,
        "incorrect_references": false,
        "unavailable_kept_unresolved": len(unresolved_absent),
        "unavailable_expected": len(absent),
    }


async def _references(settings, output, report, *, loop_calls=LOOP_CALLS):
    folder = CORPUS / "construction-terra-reference"
    gold = json.loads((folder / "gold.json").read_text())
    model = BusinessModel.model_validate(yaml.safe_load((folder / "model.yaml").read_text()))
    source = folder / gold["document"]
    doc = _document(source, gold["window_chars"])
    parsed = _parse(doc, model, chunk_target=620, chunk_max=620)
    checkpoint = {}
    for name, limit in (("baseline", 0), ("loop", loop_calls)):
        if limit:
            checkpoint, reuse = _frozen(checkpoint, limit)
        rows, cost = await _import(
            settings,
            doc,
            parsed,
            model,
            checkpoint,
            output / f"references-{name}-checkpoint.json",
            limit=limit,
            reuse=reuse if limit else {},
        )
        write_json(output / f"references-{name}-adopted.json", rows)
        report[name] = {"quality": _score_references(rows, gold, source.read_text()), "cost": cost}
        print(f"跨窗 {name} 已评分", flush=True)


def _graph_options(settings):
    import os

    values = dotenv_values(ROOT / ".env")
    name = settings.neo4j_password_env
    if not os.getenv(name) and values.get(name):
        os.environ[name] = values[name]
    return settings


def _ids(entities):
    return {
        item["identity"]["fields"]["app_id"]: item["entity_id"]
        for item in entities
        if item["entity_type"] == "Service"
    }


async def _project(graph, kb, model, rows):
    old_entities, old_edges = await graph.all(kb)
    active, _ = effective_contributions(rows)
    entities, edges = aggregate(kb, active, model, old_entities, old_edges)
    mapped = ({item["entity_id"] for item in entities}, {item["relation_id"] for item in edges})
    await graph.apply(
        kb,
        model.model_dump(),
        entities,
        edges,
        [item["entity_id"] for item in old_entities if item["entity_id"] not in mapped[0]],
        [item["relation_id"] for item in old_edges if item["relation_id"] not in mapped[1]],
    )
    snapshot = await graph.all(kb)
    if ({item["entity_id"] for item in snapshot[0]}, {item["relation_id"] for item in snapshot[1]}) != mapped:
        raise RuntimeError("Persistent graph differs from the expected projection")
    return snapshot


def _score_graph(stage, snapshot, by_document, previous_ids, rows):
    entities, edges = snapshot
    service_entities = [item for item in entities if item["entity_type"] == "Service"]
    ids = _ids(service_entities)
    inverse = {item["entity_id"]: item["identity"]["fields"]["app_id"] for item in service_entities}
    calls = [edge for edge in edges if edge["relation_type"] == "CALLS"]
    pairs = {
        edge["relation_id"]: (inverse.get(edge["source_id"]), inverse.get(edge["target_id"]))
        for edge in calls
    }
    actual = {pairs[edge["relation_id"]]: edge for edge in calls}
    expected = {
        (item["source_app_id"], item["target_app_id"]): set(item["contribution_sources"])
        for item in stage["calls"]
    }
    expected_ids = set(stage["service_app_ids"])
    service_hits = len(expected_ids & ids.keys())
    relation_hits = len(actual.keys() & expected.keys())
    stable_ids = sum(inverse.get(value) == key for key, value in previous_ids.items())
    exact_provenance = sum(
        pair in actual and {by_document[doc_id] for doc_id in actual[pair]["document_ids"]} == sources
        for pair, sources in expected.items()
    )
    support = defaultdict(set)
    for row in rows:
        if row["field_path"] == "$relation" and row["target_id"] in pairs:
            key = pairs[row["target_id"]], by_document.get(row["document_id"])
            support[key].update(ref["quote"] for ref in row["evidence"] if ref.get("quote"))

    exact_supports, evidence_hits, expected_evidence, missing_supports = 0, 0, 0, []
    for item in stage["calls"]:
        pair = item["source_app_id"], item["target_app_id"]
        for source, quotes in item.get("evidence", {}).items():
            required = {quotes} if isinstance(quotes, str) else set(quotes)
            visible = support[pair, source]
            matched = sum(any(quote in candidate for candidate in visible) for quote in required)
            expected_evidence += len(required)
            evidence_hits += matched
            if matched == len(required):
                exact_supports += 1
            else:
                missing_supports.append([*pair, source])
    return {
        "metrics": {
            "entities": precision_recall(service_hits, len(service_entities), len(expected_ids)),
            "relations": precision_recall(relation_hits, len(calls), len(expected)),
            "source_evidence": coverage(evidence_hits, expected_evidence),
            "relation_source_sets": coverage(exact_provenance, len(expected)),
            "prior_service_ids": coverage(stable_ids, len(previous_ids)),
        },
        "services": {
            "correct": service_hits,
            "expected": len(expected_ids),
            "unexpected": sorted(ids.keys() - expected_ids),
            "prior_ids_preserved": stable_ids,
            "prior_ids_expected": len(previous_ids),
        },
        "calls": {
            "correct": relation_hits,
            "expected": len(expected),
            "unexpected": [list(pair) for pair in actual if pair not in expected],
            "forbidden": [pair for pair in stage.get("forbidden_calls", []) if tuple(pair) in actual],
            "exact_provenance": exact_provenance,
            "exact_supports": exact_supports,
            "missing_supports": missing_supports,
        },
    }


async def _wait_for_graph(graph):
    for step in range(18):
        try:
            if await graph.ready():
                return
        except ServiceUnavailable:
            if step == 17:
                raise
        await asyncio.sleep(1.5)
    raise RuntimeError("Neo4j application schema unavailable")


def _mention_search(graph, rows):
    async def contributions(kb_id, query, entity_type, limit):
        matches = {}
        active, _ = effective_contributions(rows)
        anchors = {
            row["target_id"]
            for row in active
            if row["field_path"] == "$exists" and row["value"]["entity_type"] == entity_type
        }
        for row in active:
            if (
                row["kb_id"] == kb_id
                and row["target_id"] in anchors
                and row["field_path"] == "$mention"
                and row["value"]["mention"].lower() == query.lower()
            ):
                matches.setdefault(row["target_id"], row)
        return list(matches.values())[:limit]

    resources = SimpleNamespace(
        graph=graph, metadata=SimpleNamespace(published_mention_contributions=contributions)
    )
    return partial(search_published_mentions, resources)


async def _sequence(settings, output, report, folder=CORPUS / "construction-terra", *, loop_calls=LOOP_CALLS):
    labels = json.loads((folder / "annotations.json").read_text())
    model = BusinessModel.model_validate(yaml.safe_load((folder / labels["business_model"]).read_text()))
    graph = Graph(_graph_options(settings))
    try:
        await _wait_for_graph(graph)
        kbs = {name: str(uuid4()) for name in ("baseline", "loop")}
        rows = {name: [] for name in kbs}
        snapshots = {name: ([], []) for name in kbs}
        names = {}
        report["graph_ids"] = kbs
        report["rounds"] = []
        for index, stage in enumerate(labels["imports"], start=1):
            doc = _document(folder / stage["source"], labels.get("window_chars", 2000))
            names[doc["document_id"]] = stage["source"]
            parsed = _parse(
                doc,
                model,
                chunk_target=labels.get("chunk_target", 360),
                chunk_max=labels.get("chunk_max", 555),
            )
            previous = {name: _ids(snapshots[name][0]) for name in kbs}
            compared = {
                "source": stage["source"],
                "source_chars": len((folder / stage["source"]).read_text(encoding="utf-8")),
                "source_chunks": len(parsed["chunks"]),
            }
            report["rounds"].append(compared)
            checkpoint = {}
            for name, kb in kbs.items():
                if index == 1 and name == "loop":
                    rows[name] = [dict(row, kb_id=kb) for row in rows["baseline"]]
                    cost = {"seed_reuse": True, "http_attempts": 0, "calls": []}
                else:
                    limit = loop_calls if name == "loop" else 0
                    if limit:
                        checkpoint, reuse = _frozen(checkpoint, limit)
                    adopted, cost = await _import(
                        settings,
                        dict(doc, kb_id=kb),
                        parsed,
                        model,
                        checkpoint,
                        output / f"{index}-{name}-checkpoint.json",
                        limit=limit,
                        reuse=reuse if limit else {},
                        graph=graph,
                        mention_search=_mention_search(graph, rows[name]),
                    )
                    rows[name].extend(adopted)
                snapshots[name] = await _project(graph, kb, model, rows[name])
                compared[name] = {
                    "quality": _score_graph(stage, snapshots[name], names, previous[name], rows[name]),
                    "cost": cost,
                }
                write_json(
                    output / f"{index}-{name}-projection.json", {"rows": rows[name], "graph": snapshots[name]}
                )
                write_json(output / "progress.json", report)
                print(f"{stage['source']} {name} 已核对 Neo4j", flush=True)
        withdrawal = labels["withdraw"]
        report["withdrawal"] = {"source": withdrawal["source"]}
        for name, kb in kbs.items():
            previous = _ids(snapshots[name][0])
            repeated = await _project(graph, kb, model, rows[name])
            exact_repeat = _ids(repeated[0]) == previous and {
                (edge["relation_id"], tuple(edge["document_ids"])) for edge in repeated[1]
            } == {(edge["relation_id"], tuple(edge["document_ids"])) for edge in snapshots[name][1]}
            kept = [row for row in rows[name] if names[row["document_id"]] != withdrawal["source"]]
            snapshots[name] = await _project(graph, kb, model, kept)
            report["withdrawal"][name] = {
                "repeat_ids_and_sources_stable": exact_repeat,
                "quality": _score_graph(withdrawal, snapshots[name], names, previous, kept),
            }
    finally:
        await graph.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("references", "sequence"))
    parser.add_argument("--corpus", help="连续导入用例在 evals/corpus 下的目录名")
    parser.add_argument("--loop-calls", type=int, default=LOOP_CALLS, help="每份资料的复查请求上限")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not 1 <= args.loop_calls <= 64:
        parser.error("--loop-calls 须在 1 到 64 之间")
    settings = Settings.load(args.config)
    settings = settings.model_copy(update={"llm": settings.construction_llm or settings.llm})
    folder = (CORPUS / (args.corpus or "construction-terra")).resolve()
    if args.corpus and (args.case != "sequence" or folder.parent != CORPUS.resolve() or not folder.is_dir()):
        parser.error("--corpus 须为 evals/corpus 下的连续导入用例目录")
    report = {"case": args.case, "model": settings.llm.model, "review_budget": args.loop_calls}
    output = args.output.parent / args.output.stem
    try:
        if args.case == "sequence":
            report["corpus"] = folder.name
            asyncio.run(_sequence(settings, output, report, folder, loop_calls=args.loop_calls))
        else:
            asyncio.run(_references(settings, output, report, loop_calls=args.loop_calls))
    finally:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
