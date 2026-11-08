"""Small closed-label construction comparison for whole and structured extraction."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import yaml

from knowledge_graph.adapters.models.http import Language
from knowledge_graph.adapters.parsers.documents import Parser
from knowledge_graph.domain import canonical
from knowledge_graph.domain.model import BusinessModel
from knowledge_graph.domain.source import effective_contributions
from knowledge_graph.services.construction.alignment import align
from knowledge_graph.services.construction.extraction import build_rule_context, extract_text
from knowledge_graph.services.construction.merge import aggregate
from knowledge_graph.settings import Settings

from .evidence import ROOT, write_json

CORPUS = ROOT / "evals" / "corpus" / "quality-labeled"
CONFIG = ROOT / "examples" / "contracts" / "openrouter-quality.yaml"


def _inputs(atomic_group=False):
    model_data = yaml.safe_load((CORPUS / "model.yaml").read_text())
    if atomic_group:
        model_data["node_types"]["Clause"]["atomic_groups"] = [
            {"name": "full_statement", "fields": ["definition"]}
        ]
    model = BusinessModel.model_validate(model_data)
    labels = json.loads((CORPUS / "annotations.json").read_text())
    files = [CORPUS / name for name in ("registry.txt", "operations.md")]
    assert labels["schema_version"] == 1
    assert all((CORPUS / row["source"]).read_text().count(row["phrase"]) == 1 for row in labels["clauses"])
    return model, labels, files


def check(atomic_group=False):
    model, labels, files = _inputs(atomic_group)
    parser = Parser(SimpleNamespace(chunk_target=180, chunk_max=240, chunk_overlap=40))
    counts = {}
    for path in files:
        document = {
            "document_id": str(uuid4()),
            "raw_path": str(path),
            "media_type": path.suffix[1:],
            "source": {"domain_ids": []},
        }
        counts[path.name] = len(parser.parse(document, model)["chunks"])
    if counts["operations.md"] < 2:
        raise ValueError("The labeled Markdown source must create multiple extraction batches")
    return {
        "source_chunks": counts,
        "gold": {
            "services": len(labels["service_app_ids"]),
            "clauses": len(labels["clauses"]),
            "calls": len(labels["calls"]),
        },
    }


class RecordedLanguage(Language):
    def __init__(self, settings, path):
        super().__init__(settings)
        self.path, self.calls, self.stage = path, [], "unknown"
        path.mkdir(parents=True, exist_ok=True)

    async def json(self, system, data, schema=None):
        self.stage = (
            "link"
            if "allowed_pairs" in data
            else "repair"
            if "repair_entities" in data
            else "assembly"
            if "atomic_group" in data and "parts" in data
            else "initial"
        )
        return await super().json(system, data, schema)

    async def _post(self, base_url, endpoint, key_env, body, timeout):
        started = time.monotonic()
        record = {"stage": self.stage, "request_chars": len(canonical(body)), "model": body.get("model")}
        try:
            response = await super()._post(base_url, endpoint, key_env, body, timeout)
            record.update(
                outcome="completed",
                finish_reason=response.get("choices", [{}])[0].get("finish_reason"),
                usage=response.get("usage"),
            )
            write_json(self.path / f"{len(self.calls):03d}.json", {"request": body, "response": response})
            return response
        except Exception as exc:
            record.update(outcome="failed", error=type(exc).__name__ + ": " + str(exc))
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.calls.append(record)
            write_json(self.path.parent / "calls.json", self.calls)


class ExistingGraph:
    def __init__(self, entities, relations):
        self.entities, self.relations = entities, relations

    async def by_identity(self, _kb, kind, keys):
        items = self.entities if kind == "entity" else self.relations
        field = "entity_type" if kind == "entity" else "relation_type"
        return [item for item in items if (item[field], canonical(item["identity"])) in keys]

    async def by_property_value(self, _kb, type_name, field, value, document_ids=None):
        return [
            item
            for item in self.entities
            if item["entity_type"] == type_name
            and item["identity"]["fields"].get(field) == value
            and (not document_ids or set(item["document_ids"]) & set(document_ids))
        ]


def _score(rows, entities, relations, labels):
    service_items = [item for item in entities if item["entity_type"] == "Service"]
    service_counts = Counter(item["identity"]["fields"]["app_id"] for item in service_items)
    services = {item["identity"]["fields"]["app_id"]: item for item in service_items}
    expected_services = set(labels["service_app_ids"])
    found_services = set(services)
    definitions = defaultdict(list)
    literal_items = []
    for row in rows:
        if row["field_path"] == "/properties/definition":
            definitions[row["target_id"]].append(row["value"])
            literal_items.append(
                {
                    "target_id": row["target_id"],
                    "value": row["value"],
                    "provenance": row.get("claim", {}).get("literal_provenance", {}),
                }
            )
        elif row["field_path"] == "/atomic_groups/full_statement":
            value = row.get("value", {}).get("fields", {}).get("definition")
            if isinstance(value, str):
                definitions[row["target_id"]].append(value)
                literal_items.append(
                    {
                        "target_id": row["target_id"],
                        "value": value,
                        "provenance": row["value"].get("literal_provenance", {}).get("definition", {}),
                    }
                )
    clause_ids = {item["entity_id"] for item in entities if item["entity_type"] == "Clause"}
    matches = {
        entity_id: [
            gold["id"]
            for gold in labels["clauses"]
            if any(gold["phrase"] in value for value in definitions[entity_id])
        ]
        for entity_id in clause_ids
    }
    assigned = set()
    clause_tp = 0
    for entity_id in sorted(matches, key=lambda key: len(matches[key])):
        available = [gold for gold in matches[entity_id] if gold not in assigned]
        if available:
            assigned.add(available[0])
            clause_tp += 1
    id_to_service = {item["entity_id"]: item["identity"]["fields"]["app_id"] for item in service_items}
    found_calls = {
        (id_to_service.get(item["source_id"]), id_to_service.get(item["target_id"]))
        for item in relations
        if item["relation_type"] == "CALLS"
    }
    expected_calls = {tuple(pair) for pair in labels["calls"]}
    counts = {
        "Service": (
            len(found_services & expected_services),
            len(service_items) - len(found_services & expected_services),
            len(expected_services - found_services),
        ),
        "Clause": (clause_tp, len(clause_ids) - clause_tp, len(labels["clauses"]) - clause_tp),
        "CALLS": (
            len(found_calls & expected_calls),
            len(found_calls - expected_calls),
            len(expected_calls - found_calls),
        ),
    }
    scores = {
        name: {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn),
        }
        for name, (tp, fp, fn) in counts.items()
    }
    source = (CORPUS / "operations.md").read_text()

    def ranges(item):
        provenance = item["provenance"]
        return sorted(
            span["origin"]["char_range"]
            for part in provenance.get("source_parts", [])
            for span in part.get("source_spans", [])
            if "char_range" in span.get("origin", {})
        )

    def covers(intervals, start, end):
        covered = start
        for left, right in sorted(intervals):
            if left > covered:
                return False
            covered = max(covered, right)
            if covered >= end:
                return True
        return False

    exact_source_rows = 0
    for item in literal_items:
        source_ranges = ranges(item)
        provenance = item["provenance"]
        if (
            source_ranges
            and provenance.get("fidelity") == "source_exact"
            and item["value"] == source[source_ranges[0][0] : source_ranges[-1][1]]
        ):
            exact_source_rows += 1
    full_value, exact_value, evidence_union = 0, 0, 0
    for gold in labels["clauses"]:
        phrase = gold["phrase"]
        start = source.index(phrase)
        end = start + len(phrase)
        full_value += any(phrase in item["value"] for item in literal_items)
        exact_value += any(item["value"].strip() == phrase for item in literal_items)
        evidence_union += any(
            covers(
                [
                    interval
                    for item in literal_items
                    if item["target_id"] == entity_id
                    for interval in ranges(item)
                ],
                start,
                end,
            )
            for entity_id in clause_ids
        )
    return {
        "scores": scores,
        "same_heading_false_merge": sum(len(found) > 1 for found in matches.values()),
        "same_clause_false_split": sum(
            sum(gold in found for found in matches.values()) > 1
            for gold in (item["id"] for item in labels["clauses"])
        ),
        "service_ids": sorted(found_services),
        "clause_assignments": matches,
        "calls": sorted(map(list, found_calls)),
        "literal_values": len(literal_items),
        "literal_source_exact_rows": exact_source_rows,
        "full_definition_values": full_value,
        "exact_definition_values": exact_value,
        "full_definition_source_span_union": evidence_union,
        "literal_methods": dict(
            Counter(item["provenance"].get("selection_method", "unknown") for item in literal_items)
        ),
        "duplicate_service_instances": sum(count - 1 for count in service_counts.values()),
        "alias_target_present": "pay-core" in services
        and "pay-demo" in services
        and services["pay-core"]["entity_id"] != services["pay-demo"]["entity_id"],
    }


async def run(atomic_group=False):
    model, labels, files = _inputs(atomic_group)
    settings = Settings.load(str(CONFIG))
    language_settings = settings.model_copy(update={"llm": settings.construction_llm or settings.llm})
    run_id = str(uuid4())
    directory = ROOT / "evals" / "records" / run_id
    directory.mkdir(parents=True)
    parser = Parser(SimpleNamespace(chunk_target=180, chunk_max=240, chunk_overlap=40))
    write_json(
        directory / "inputs.json",
        {
            "run_id": run_id,
            "atomic_group": atomic_group,
            "files": {
                path.name: sha256(path.read_bytes()).hexdigest()
                for path in [*files, CORPUS / "model.yaml", CORPUS / "annotations.json"]
            },
            "model": language_settings.llm.model_dump(exclude={"api_key"}),
            "parser": {"chunk_target": 180, "chunk_max": 240, "chunk_overlap": 40},
            "scoring": "closed labels for Service/Clause instances and CALLS only; clause matching by exact unique source phrase",
        },
    )
    for policy in ("whole", "structure"):
        language = RecordedLanguage(language_settings, directory / policy / "responses")
        kb_id, rows, entities, relations = str(uuid4()), [], [], []
        started = time.monotonic()
        doc_results = []
        try:
            for path in files:
                document = {
                    "kb_id": kb_id,
                    "document_id": str(uuid4()),
                    "raw_path": str(path),
                    "media_type": path.suffix[1:],
                    "source": {"domain_ids": []},
                }
                parsed = parser.parse(document, model)
                chunks = {item["chunk_id"]: item for item in parsed["chunks"]}

                async def persist():
                    return None

                extracted = await extract_text(
                    language,
                    parsed,
                    model,
                    {},
                    persist,
                    scope_policy=policy,
                    structure_window_chars=180,
                )
                adopted, issues = await align(
                    document,
                    extracted,
                    chunks,
                    model,
                    {},
                    str(uuid4()),
                    entities,
                    relations,
                    build_rule_context(parsed),
                    graph=ExistingGraph(entities, relations),
                )
                rows.extend(adopted)
                active, suppressed = effective_contributions(rows)
                entities, relations = aggregate(kb_id, active, model)
                doc_results.append(
                    {
                        "source": path.name,
                        "chunks": len(chunks),
                        "extraction_batches": extracted.get("batch_count"),
                        "candidates": {kind: len(extracted[kind]) for kind in ("entities", "relations")},
                        "adopted_contributions": len(adopted),
                        "issues": [
                            item.get("reason_code") for item in [*extracted.get("issues", []), *issues]
                        ],
                        "suppressed_dependencies": len(suppressed),
                    }
                )
                write_json(
                    directory / policy / (path.name + ".json"),
                    {"extracted": extracted, "adopted": adopted, "issues": issues},
                )
            result = {
                "status": "completed",
                "documents": doc_results,
                **_score(rows, entities, relations, labels),
            }
        except Exception as exc:
            result = {
                "status": "failed",
                "documents": doc_results,
                "error": type(exc).__name__ + ": " + str(exc),
            }
        finally:
            await language.close()
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        result["physical_calls"] = dict(Counter(item["stage"] for item in language.calls))
        result["model_call_records"] = language.calls
        write_json(directory / (policy + "-summary.json"), result)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "policy": policy,
                    **{
                        key: result.get(key)
                        for key in ("status", "scores", "physical_calls", "wall_seconds", "error")
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return directory


def rescore(directory):
    binding = json.loads((directory / "inputs.json").read_text())
    model, labels, files = _inputs(binding.get("atomic_group", False))
    for path in [*files, CORPUS / "model.yaml", CORPUS / "annotations.json"]:
        if binding["files"].get(path.name) != sha256(path.read_bytes()).hexdigest():
            raise ValueError("Scoring sources or business model changed; preserve the old result")
    second = "structure" if (directory / "structure").is_dir() else "small_batch"
    for policy in ("whole", second):
        rows = [
            row
            for path in files
            for row in json.loads((directory / policy / (path.name + ".json")).read_text())["adopted"]
        ]
        active, _ = effective_contributions(rows)
        entities, relations = aggregate(rows[0]["kb_id"], active, model)
        value = _score(rows, entities, relations, labels)
        write_json(directory / (policy + "-rescored.json"), value)
        print(json.dumps({"policy": policy, **value}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--atomic-group", action="store_true", help="Add Clause.full_statement as in the contract model"
    )
    parser.add_argument("--rescore", type=Path)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(args.atomic_group), ensure_ascii=False))
    elif args.rescore:
        rescore(args.rescore)
    else:
        asyncio.run(run(args.atomic_group))


if __name__ == "__main__":
    main()
