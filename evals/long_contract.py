"""Evaluate one frozen complete CUAD contract through the real isolated service."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import yaml

from knowledge_graph.domain import KGError

from .contract import audit_source_evidence, character_range, model
from .evidence import ROOT, CaseFailure, Evidence, PaidValidationStopped, now, read_json, write_json

CORPUS = ROOT / "evals" / "corpus" / "cuad-long"
STAGE = "long_contract"
LIMITS = [
    "One complete public contract, not the full CUAD benchmark or enterprise accuracy.",
    "CUAD annotations cover selected categories, not every valid graph object; no false-positive score.",
    "Scores measure exact source-coordinate coverage, not legal or semantic correctness.",
    "The service returns candidates and evidence, not final answers; no negative-answer accuracy or hallucination score.",
    "Supplemental references are manual source review, not official CUAD annotations.",
    "The separately referenced NDA is absent; its full terms are not inferred.",
    "Actual monetary cost is not measured; physical attempts and provider usage are recorded.",
]


def load_case():
    manifest = read_json(CORPUS / "source-manifest.json")
    if len(manifest["files"]) != 1:
        raise CaseFailure("The long-contract case must contain exactly one full source document")
    entry = manifest["files"][0]
    path = CORPUS / entry["path"]
    source_bytes = path.read_bytes()
    source = source_bytes.decode("utf-8")
    if not 20000 <= len(source_bytes) <= 40000 or len(source_bytes) != entry["byte_length"]:
        raise CaseFailure("Full contract bytes differ from the frozen 20–40 KB source manifest")
    if len(source) != entry["character_length"]:
        raise CaseFailure("Contract Unicode character count differs from its manifest")
    official = read_json(CORPUS / manifest["official_qa_file"])
    if len(official["data"]) != 1:
        raise CaseFailure("Official QA must retain exactly the selected complete document")
    document = official["data"][0]
    if document["title"] != entry["title"] or len(document["paragraphs"]) != 1:
        raise CaseFailure("Official QA document identity differs from the frozen source")
    paragraph = document["paragraphs"][0]
    if paragraph["context"].encode("utf-8") != source_bytes:
        raise CaseFailure("Official QA context and uploaded original TXT bytes differ")
    questions, spans = {}, []
    for qa in paragraph["qas"]:
        category = qa["id"].rsplit("__", 1)[-1]
        if category in questions or bool(qa["answers"]) == bool(qa["is_impossible"]):
            raise CaseFailure("Official QA category or no-answer annotation is inconsistent")
        questions[category] = qa
        for index, answer in enumerate(qa["answers"]):
            start = answer["answer_start"]
            end = start + len(answer["text"])
            character_range([start, end], len(source))
            if source[start:end] != answer["text"]:
                raise CaseFailure("Official answer coordinates differ from the unchanged source: " + category)
            spans.append(
                {
                    "category": category,
                    "qa_id": qa["id"],
                    "answer_index": index,
                    "char_range": [start, end],
                    "text": answer["text"],
                    "origin": "official_cuad",
                }
            )
    if len(questions) != 41:
        raise CaseFailure("The complete official 41-category QA set is required")
    fixed = read_json(CORPUS / manifest["fixed_case_file"])
    if fixed["schema_version"] != 1 or fixed["top_k"] != 5:
        raise CaseFailure("Frozen long-contract queries require schema 1 and Top-K 5")
    if not 8 <= len(fixed["positive_queries"]) <= 12 or len(fixed["retrieve_queries"]) != 2:
        raise CaseFailure("Expected 8–12 fixed positive queries and two supplemental retrieval queries")
    names, categories = set(), set()
    for query in fixed["positive_queries"]:
        qa = questions[query["category"]]
        if qa["is_impossible"] or query["query"] != qa["question"] or query["category"] in categories:
            raise CaseFailure("Positive queries must use distinct official positive questions verbatim")
        categories.add(query["category"])
    for query in fixed["positive_queries"] + fixed["retrieve_queries"]:
        if query["name"] in names:
            raise CaseFailure("Frozen query names must be unique")
        names.add(query["name"])
    for query in fixed["retrieve_queries"]:
        if not query["reference_spans"]:
            raise CaseFailure("Supplemental retrieval queries require separately labelled source references")
        for span in query["reference_spans"]:
            start, end = character_range(span["char_range"], len(source))
            if span["origin"] != "manual_source_review" or source[start:end] != span["text"]:
                raise CaseFailure("Supplemental reference differs from its manual source coordinates")
    freeze = read_json(CORPUS / manifest["freeze_file"])
    if freeze["stage"] != "before_paid_model_run" or freeze["model_calls_during_freeze"] != 0:
        raise CaseFailure("Fixed query freeze must predate paid execution")
    if (
        freeze["positive_query_count"] != len(fixed["positive_queries"])
        or freeze["retrieve_query_count"] != 2
        or freeze["references"]
        != {
            query["name"]: [span["char_range"] for span in query["reference_spans"]]
            for query in fixed["retrieve_queries"]
        }
    ):
        raise CaseFailure("Frozen question/reference inventory differs from the fixed case")
    counts = {
        "official_categories": len(questions),
        "positive_categories": sum(not qa["is_impossible"] for qa in questions.values()),
        "negative_categories": sum(qa["is_impossible"] for qa in questions.values()),
        "official_positive_spans": len(spans),
    }
    if any(counts[key] != entry[key] for key in counts):
        raise CaseFailure("Official QA counts differ from the frozen source manifest")
    return {
        "manifest": manifest,
        "entry": entry,
        "path": path,
        "source": source,
        "source_bytes": source_bytes,
        "questions": questions,
        "spans": spans,
        "fixed": fixed,
        "freeze": freeze,
        "counts": counts,
    }


def estimate(case):
    """Run the actual parser/scope planner without constructing a network client or loading settings."""
    from knowledge_graph.adapters.models.http import Language
    from knowledge_graph.adapters.parsers.documents import Parser
    from knowledge_graph.domain import BusinessModel
    from knowledge_graph.prompts.extraction import EXTRACTION_SYSTEM, STRUCTURE_EXTRACTION_SYSTEM
    from knowledge_graph.services.construction.extraction import (
        Extraction,
        StructuredExtraction,
        _plan_scopes,
        build_rule_context,
    )
    from knowledge_graph.services.construction.extraction_batches import ExtractionBatch
    from knowledge_graph.settings import LanguageProfile

    budgets = SimpleNamespace(chunk_target=1200, chunk_max=2000, chunk_overlap=150)
    business_model = BusinessModel.model_validate(model())
    parsed = Parser(budgets).parse(
        {
            "raw_path": str(case["path"]),
            "media_type": "txt",
            "source": {"encoding": "utf-8"},
            "document_id": str(uuid4()),
        },
        business_model,
    )
    # Only pure serialization methods are used; Language.__init__ would construct an HTTP client.
    language = object.__new__(Language)
    profile = yaml.safe_load((ROOT / "examples/contracts/openrouter-quality.yaml").read_text())
    language.profile = LanguageProfile.model_validate(profile["construction_llm"])
    chunks = {item["chunk_id"]: item for item in parsed["chunks"]}
    model_data = business_model.model_dump()
    estimates = {}
    for policy, system, output in (
        ("whole", EXTRACTION_SYSTEM, Extraction),
        ("structure", STRUCTURE_EXTRACTION_SYSTEM, StructuredExtraction),
    ):
        context = build_rule_context(parsed, structured=policy == "structure")
        schema = output.model_json_schema()
        scopes = _plan_scopes(language, chunks, context, model_data, schema, scope_policy=policy)
        estimates[policy] = {
            "source_rule_units": len(context),
            "extraction_batches": len(scopes),
            "batch_request_chars": [
                language.json_request_chars(
                    system, ExtractionBatch(scope, chunks, model_data, scope_policy=policy).request, schema
                )
                for scope in scopes
            ],
        }
    return {
        "basis": "Parser and scope planner using the published OpenRouter profile; no Settings.load",
        "chunk_target": 1200,
        "chunk_max": 2000,
        "chunk_overlap": 150,
        "max_input_chars": language.profile.max_input_chars,
        "max_output_tokens": language.profile.max_output_tokens,
        "chunks": len(chunks),
        **estimates["whole"],
        "structure": estimates["structure"],
        "chunk_embedding_batches_at_size_10": math.ceil(len(chunks) / 10),
        "fixed_semantic_searches": len(case["fixed"]["positive_queries"]) * 2,
        "fixed_natural_language_retrievals": 2,
        "unknown_until_real_output": [
            "rule assembly calls",
            "adopted entity embedding batches",
            "retries/corrections",
            "retrieval plan-dependent embedding calls",
        ],
    }


def check():
    """No credentials, Settings.load, databases, network, subprocesses, or real business execution."""
    from knowledge_graph.domain import BusinessModel
    from knowledge_graph.services.contracts import RetrieveRequest, SearchRequest

    for path in sorted((ROOT / "evals").glob("*.py")):
        compile(path.read_bytes().decode("utf-8"), str(path), "exec", dont_inherit=True)
    BusinessModel.model_validate(model())
    case = load_case()
    for query in case["fixed"]["positive_queries"]:
        for kind in ("entity", "chunk"):
            SearchRequest(query=query["query"], kinds=[kind], semantic=True, limit=5)
    for query in case["fixed"]["retrieve_queries"]:
        RetrieveRequest(query=query["query"], limit=5, evidence_limit=1000)
    return {
        "status": "static_checks_passed",
        "source_bytes": len(case["source_bytes"]),
        "source_characters": len(case["source"]),
        **case["counts"],
        "positive_queries": len(case["fixed"]["positive_queries"]),
        "retrieve_queries": 2,
        "top_k": 5,
        "frozen_at": case["freeze"]["frozen_at_utc"],
        "estimate": estimate(case),
        "network_calls": 0,
        "processes_started": 0,
        "business_cases_executed": 0,
    }


def merged(ranges):
    output = []
    for start, end in sorted(ranges):
        if output and start <= output[-1][1]:
            output[-1][1] = max(output[-1][1], end)
        else:
            output.append([start, end])
    return output


def interval_coverage(span, ranges):
    start, end = span["char_range"]
    covered = merged([[max(start, a), min(end, b)] for a, b in ranges if max(start, a) < min(end, b)])
    count = sum(b - a for a, b in covered)
    uncovered, cursor = [], start
    for left, right in covered:
        if cursor < left:
            uncovered.append([cursor, left])
        cursor = right
    if cursor < end:
        uncovered.append([cursor, end])
    return {
        "covered_characters": count,
        "total_characters": end - start,
        "character_coverage": count / (end - start),
        "full_span": count == end - start,
        "covered_ranges": covered,
        "uncovered_ranges": uncovered,
    }


def chunk_ranges(chunk, source, document_id):
    if chunk["document_id"] != document_id:
        raise ValueError("Source chunk belongs to a different document")
    output = []
    for span in chunk["source_spans"]:
        if span.get("role") != "source":
            continue
        a, b = character_range(span["chunk_range"], len(chunk["text"]))
        start, end = character_range(span["origin"]["char_range"], len(source))
        if b - a != end - start or chunk["text"][a:b] != source[start:end]:
            raise ValueError("Source chunk coordinates differ from the unchanged contract")
        output.append([start, end])
    return merged(output)


def reference_ranges(reference, chunks, source, document_id, *, returned=False):
    from knowledge_graph.domain.source import validate_stored_evidence

    chunk = chunks[reference["chunk_id"]]
    chunk_ranges(chunk, source, document_id)
    if reference["document_id"] != document_id:
        raise ValueError("Citation belongs to a different document")
    keys = {
        "document_id",
        "chunk_id",
        "chunk_range",
        "role",
        "quote",
        "quote_truncated",
        "quote_range",
        "source_mapping_verified",
        "locator",
        "source_spans",
    }
    stored = {key: value for key, value in reference.items() if key in keys} if returned else reference
    text = validate_stored_evidence(stored, chunk)
    if returned and reference.get("text") != text:
        raise ValueError("Returned citation text differs from its complete source interval")
    return [list(span["origin"]["char_range"]) for span in stored["source_spans"]]


def definition_ranges(value, support, source, *, require_complete=True):
    """Locate only an actually adopted/returned definition, within its verified source support."""
    if require_complete and (
        value.get("complete") is not True or value.get("references_resolved") is not True
    ):
        raise ValueError("Clause declaration is not complete with resolved references")
    definition = value["fields"]["definition"]
    if not isinstance(definition, str) or not definition:
        raise ValueError("Clause definition is absent")
    positions, offset = [], 0
    while (start := source.find(definition, offset)) >= 0:
        end = start + len(definition)
        if interval_coverage({"char_range": [start, end]}, support)["full_span"]:
            positions.append([start, end])
        offset = start + 1
    if not positions:
        raise ValueError("Adopted/returned definition is not a continuous source text within its evidence")
    return positions


def span_metrics(spans, groups):
    by_target = {}
    for index, group in enumerate(groups):
        key = (group.get("target_id"), group.get("rank")) if group.get("target_id") else (None, index)
        item = by_target.setdefault(key, {**group, "ranges": [], "contribution_ids": []})
        item["ranges"].extend(group["ranges"])
        if group.get("contribution_id"):
            item["contribution_ids"].append(group["contribution_id"])
    groups = list(by_target.values())
    maximum_rank = max((group.get("rank", 0) for group in groups), default=0)
    rows = []
    all_ranges = [interval for group in groups for interval in group["ranges"]]
    for span in spans:
        row = {**span, **interval_coverage(span, all_ranges)}
        hits = [
            {
                "target_id": group.get("target_id"),
                "contribution_ids": sorted(set(group["contribution_ids"])),
                "rank": group.get("rank"),
                **interval_coverage(span, group["ranges"]),
            }
            for group in groups
            if interval_coverage(span, group["ranges"])["covered_characters"]
        ]
        row["overlapping_targets"] = hits
        row["single_target_full_span"] = any(hit["target_id"] and hit["full_span"] for hit in hits)
        row["first_full_span_prefix_rank"] = (
            next(
                (
                    rank
                    for rank in range(1, maximum_rank + 1)
                    if interval_coverage(
                        span,
                        [
                            interval
                            for group in groups
                            if group.get("rank", 0) <= rank
                            for interval in group["ranges"]
                        ],
                    )["full_span"]
                ),
                None,
            )
            if groups and all("rank" in group for group in groups)
            else None
        )
        rows.append(row)
    category_rows = []
    for category in dict.fromkeys(row.get("category", "supplemental") for row in rows):
        selected = [row for row in rows if row.get("category", "supplemental") == category]
        category_rows.append(
            {
                "category": category,
                "spans": len(selected),
                "full_spans": sum(row["full_span"] for row in selected),
                "all_spans_full": all(row["full_span"] for row in selected),
                "any_span_overlap": any(row["covered_characters"] for row in selected),
                "mean_span_character_coverage": sum(row["character_coverage"] for row in selected)
                / len(selected),
            }
        )
    total = sum(row["total_characters"] for row in rows)
    full = sum(row["full_span"] for row in rows)
    return {
        "spans": rows,
        "categories": category_rows,
        "aggregate": {
            "span_denominator": len(rows),
            "full_span_numerator": full,
            "full_span_fraction": full / len(rows) if rows else None,
            "single_target_full_span_numerator": sum(row["single_target_full_span"] for row in rows),
            "covered_characters": sum(row["covered_characters"] for row in rows),
            "total_characters": total,
            "micro_character_coverage": sum(row["covered_characters"] for row in rows) / total
            if total
            else None,
            "macro_category_mean_span_coverage": sum(
                row["mean_span_character_coverage"] for row in category_rows
            )
            / len(category_rows)
            if category_rows
            else None,
            "categories_all_spans_full": sum(row["all_spans_full"] for row in category_rows),
            "category_denominator": len(category_rows),
        },
    }


def graph_coverage(snapshot, case, document_id):
    source = case["source"]
    chunks = {point["id"]: point["payload"] for point in snapshot["chunks"]}
    targets = {("entity", item["entity_id"]): item for item in snapshot["entities"]}
    targets.update({("relation", item["relation_id"]): item for item in snapshot["relations"]})
    groups = {
        "adopted_clause_definition": [],
        "adopted_clause_text": [],
        "graph_fact_evidence": [],
        "clause_definition_field_fact_evidence": [],
        "graph_context_evidence": [],
        "indexed_chunks_only": [],
    }
    errors = []
    for claim in snapshot["contributions"]:
        try:
            local = {key: [] for key in groups}
            target = targets[(claim["target_kind"], claim["target_id"])]
            if claim["document_id"] != document_id or document_id not in target["document_ids"]:
                raise ValueError("Accepted graph contribution has a different document scope")
            if claim["contribution_id"] not in target["contribution_ids"]:
                raise ValueError("Metadata contribution is not attached to the accepted graph target")
            base = {"target_id": claim["target_id"], "contribution_id": claim["contribution_id"]}
            support = []
            for reference in claim["evidence"]:
                ranges = reference_ranges(reference, chunks, source, document_id)
                role = reference.get("role", "legacy")
                if role == "context":
                    local["graph_context_evidence"].append({**base, "ranges": ranges})
                elif role == "fact":
                    local["graph_fact_evidence"].append({**base, "ranges": ranges})
                support.extend(ranges)
            if (
                target.get("entity_type") == "Clause"
                and claim["field_path"] == "/atomic_groups/full_statement"
            ):
                declarations = target["atomic_groups"]["full_statement"]["declarations"]
                adopted = [
                    item
                    for item in declarations
                    if item["value"]["fields"] == claim["value"]["fields"]
                    and claim["contribution_id"] in item["contribution_ids"]
                ]
                source_declarations = [
                    item
                    for declaration in adopted
                    for item in declaration["source_declarations"]
                    if item["contribution_id"] == claim["contribution_id"]
                ]
                if (
                    len(source_declarations) != 1
                    or source_declarations[0]["document_id"] != document_id
                    or any(
                        source_declarations[0].get(key) != claim["value"].get(key)
                        for key in (
                            "section_key",
                            "field_evidence",
                            "resolved_references",
                            "completeness_check",
                        )
                    )
                ):
                    raise ValueError("Metadata Clause is absent from the graph's matching source declaration")
                field_support = []
                for reference in claim["value"]["field_evidence"]["definition"]:
                    if reference.get("role") != "fact":
                        raise ValueError("Clause definition field support must be fact evidence")
                    field_support.extend(reference_ranges(reference, chunks, source, document_id))
                if not field_support:
                    raise ValueError("Adopted Clause has no definition field fact evidence")
                local["clause_definition_field_fact_evidence"].append({**base, "ranges": field_support})
                ranges = definition_ranges(claim["value"], support, source, require_complete=False)
                local["adopted_clause_text"].append({**base, "ranges": ranges})
                if (
                    claim["value"].get("complete") is True
                    and claim["value"].get("references_resolved") is True
                ):
                    local["adopted_clause_definition"].append({**base, "ranges": ranges})
            for key in groups:
                groups[key].extend(local[key])
        except (KeyError, TypeError, ValueError, KGError) as exc:
            errors.append({"contribution_id": claim.get("contribution_id"), "reason": str(exc)})
    for chunk_id, chunk in chunks.items():
        try:
            groups["indexed_chunks_only"].append(
                {"target_id": chunk_id, "ranges": chunk_ranges(chunk, source, document_id)}
            )
        except (KeyError, TypeError, ValueError, KGError) as exc:
            errors.append({"chunk_id": chunk_id, "reason": str(exc)})
    return {
        "scope": "All 30 official positive spans; exact coordinate coverage, not semantic accuracy",
        "document_id": document_id,
        "snapshot_counts": {
            key: len(snapshot[key]) for key in ("entities", "relations", "contributions", "chunks", "objects")
        },
        "clause_count": sum(item["entity_type"] == "Clause" for item in snapshot["entities"]),
        "metrics": {key: span_metrics(case["spans"], value) for key, value in groups.items()},
        "definition_mapping_basis": "Actual adopted verbatim definition within its verified fact/context source support; context alone does not count as adoption",
        "mapping_errors": errors,
        "limits": LIMITS,
    }


async def returned_groups(runtime, response, snapshot, case, document_id):
    """Audit actual HTTP content; entity candidates are expanded through the real detail endpoint."""
    groups = {
        "candidate_clause_definition_after_detail": [],
        "candidate_clause_text_after_detail": [],
        "candidate_fact_evidence_after_detail": [],
        "candidate_context_evidence_after_detail": [],
        "initial_response_document_chunks": [],
        "initial_response_fact_evidence": [],
        "initial_response_context_evidence": [],
    }
    chunks = {point["id"]: point["payload"] for point in snapshot["chunks"]}
    entities = {item["entity_id"] for item in snapshot["entities"]}
    targets = entities | {item["relation_id"] for item in snapshot["relations"]}
    errors, records = [], []
    for rank, candidate in enumerate(response["knowledge"], 1):
        target_id = candidate.get("entity_id", candidate.get("chunk_id", candidate.get("id")))
        base = {"rank": rank, "target_id": target_id}
        try:
            local = {key: [] for key in groups}
            if candidate.get("kind") == "chunk":
                stored = chunks[target_id]
                if candidate["text"] != stored["text"] or candidate["source_spans"] != stored["source_spans"]:
                    raise ValueError("Returned document candidate differs from its saved storage source")
                groups["initial_response_document_chunks"].append(
                    {**base, "ranges": chunk_ranges(candidate, case["source"], document_id)}
                )
                continue
            if target_id not in entities or document_id not in candidate.get("document_ids", []):
                raise ValueError("Returned entity is absent from this document's accepted graph")
            detail = await runtime.api.request(
                "GET",
                f"/kbs/{response['kb_id']}/knowledge/entity/{target_id}",
                params={"evidence_limit": 1000},
            )
            records.append({"rank": rank, "target_id": target_id, "record": runtime.api.last_record})
            if detail["item"]["entity_id"] != target_id:
                raise ValueError("Returned entity detail has a different identity")
            support = []
            for reference in detail["evidence"]:
                if target_id not in reference["fact_ids"]:
                    raise ValueError("Returned citation is not attached to this candidate")
                ranges = reference_ranges(reference, chunks, case["source"], document_id, returned=True)
                support.extend(ranges)
                if reference.get("role") == "fact":
                    local["candidate_fact_evidence_after_detail"].append({**base, "ranges": ranges})
                if reference.get("role") == "context" or "context" in reference.get("evidence_roles", []):
                    local["candidate_context_evidence_after_detail"].append({**base, "ranges": ranges})
            for declaration in (
                detail["item"].get("atomic_groups", {}).get("full_statement", {}).get("declarations", [])
            ):
                ranges = definition_ranges(
                    declaration["value"], support, case["source"], require_complete=False
                )
                local["candidate_clause_text_after_detail"].append({**base, "ranges": ranges})
                if (
                    declaration["value"].get("complete") is True
                    and declaration["value"].get("references_resolved") is True
                ):
                    local["candidate_clause_definition_after_detail"].append({**base, "ranges": ranges})
            for key in groups:
                groups[key].extend(local[key])
        except (KeyError, TypeError, ValueError, KGError) as exc:
            errors.append({"rank": rank, "target_id": target_id, "reason": str(exc)})
    # Natural-language retrieval may carry additional evidence for returned relations/paths.
    for index, reference in enumerate(response.get("evidence", [])):
        try:
            ranges = reference_ranges(reference, chunks, case["source"], document_id, returned=True)
            if not reference["fact_ids"] or any(
                target_id not in targets for target_id in reference["fact_ids"]
            ):
                raise ValueError("Initial-response evidence is not attached to this accepted graph")
            key = (
                "initial_response_context_evidence"
                if reference.get("role") == "context"
                else "initial_response_fact_evidence"
            )
            for target_id in reference["fact_ids"]:
                groups[key].append({"target_id": target_id, "ranges": ranges})
            if reference.get("role") != "context" and "context" in reference.get("evidence_roles", []):
                for target_id in reference["fact_ids"]:
                    groups["initial_response_context_evidence"].append(
                        {"target_id": target_id, "ranges": ranges}
                    )
        except (KeyError, TypeError, ValueError, KGError) as exc:
            errors.append({"response_evidence_index": index, "reason": str(exc)})
    return groups, errors, records


async def paid_query(runtime, action, path, body):
    e = runtime.e
    pending = e.state.setdefault("long_contract_requests", {})
    previous = pending.get(action)
    if previous and previous["status"] != "completed" and e.saved(action) is None:
        raise CaseFailure(
            "An earlier paid request has no safely reusable completed response; inspect: " + action
        )
    pending[action] = {"status": "started", "started_at": now()}
    e.save()
    try:
        response = await runtime.api.paid_request(action, path, body)
    except BaseException as exc:
        pending[action].update(status="failed_or_interrupted", exception_type=type(exc).__name__)
        e.save()
        raise
    pending[action].update(
        status="completed",
        completed_at=now(),
        reused=runtime.api.last_response_reused,
        response_record=runtime.api.last_record,
    )
    e.save()
    return response, runtime.api.last_record, runtime.api.last_response_reused


def planned_queries(case):
    return [
        {"name": query["name"], "route": route, "status": "not_run"}
        for query in case["fixed"]["positive_queries"]
        for route in ("entity", "chunk")
    ] + [
        {"name": query["name"], "route": "retrieve", "status": "not_run"}
        for query in case["fixed"]["retrieve_queries"]
    ]


async def retrieve_cases(runtime, case, kb_id, document_id, snapshot):
    e = runtime.e
    result_path = e.directory / "long-contract-retrieval.json"
    results = (
        read_json(result_path)
        if result_path.exists()
        else {"queries": planned_queries(case), "limits": LIMITS}
    )
    write_json(result_path, results)

    def persist(row):
        results["queries"] = [
            row if item["name"] == row["name"] and item["route"] == row["route"] else item
            for item in results["queries"]
        ]
        write_json(result_path, results)

    for query in case["fixed"]["positive_queries"] + case["fixed"]["retrieve_queries"]:
        supplemental = "reference_spans" in query
        spans = (
            query["reference_spans"]
            if supplemental
            else [span for span in case["spans"] if span["category"] == query["category"]]
        )
        for route in ("retrieve",) if supplemental else ("entity", "chunk"):
            action = "long_contract.query." + query["name"] + "." + route
            body = {"query": query["query"], "filters": {"document_ids": [document_id]}, "limit": 5}
            if route == "retrieve":
                body["evidence_limit"] = 1000
                path = f"/kbs/{kb_id}/retrieve"
            else:
                body.update(kinds=[route], semantic=True)
                path = f"/kbs/{kb_id}/knowledge/search"
            row = {
                "name": query["name"],
                "category": query.get("category"),
                "route": route,
                "query": query["query"],
                "reference_origin": "manual_source_review" if supplemental else "official_cuad",
                "status": "attempt_started",
                "action": action,
                "started_at": now(),
            }
            persist(row)
            try:
                response, response_record, reused = await paid_query(runtime, action, path, body)
            except BaseException as exc:
                row.update(status="attempt_failed_or_interrupted", failure_type=type(exc).__name__)
                persist(row)
                raise
            row.update(status="response_saved", response_record=response_record, reused=reused)
            persist(row)
            try:
                groups, errors, details = await returned_groups(
                    runtime, response, snapshot, case, document_id
                )
            except BaseException as exc:
                row.update(status="response_saved_detail_or_scoring_failed", failure_type=type(exc).__name__)
                persist(row)
                raise
            row.update(
                {
                    "status": "executed",
                    "response_status": response["status"],
                    "request_limit": 5,
                    "knowledge_count": len(response["knowledge"]),
                    "relation_count": len(response["relations"]),
                    "response_record": response_record,
                    "reused": reused,
                    "detail_records": details,
                    "plan_summary": response.get("plan_summary"),
                    "warnings": response["warnings"],
                    "coverage": response["coverage"],
                    "mapping_errors": errors,
                    "metrics": {key: span_metrics(spans, value) for key, value in groups.items()},
                    "semantic_correctness": "pending_independent_review",
                    "metric_scope": "Initial-response metrics use only the search/retrieve body; after_detail metrics additionally use explicitly recorded HTTP entity detail responses",
                    "rank_scope": "Service knowledge response order; mixed retrieve search plans may return up to the request limit separately for entities and chunks",
                }
            )
            persist(row)
            e.check(
                "returned source mapping " + query["name"] + " / " + route,
                not errors,
                "every returned candidate/evidence maps to this unchanged source",
                errors,
                evidence=response_record,
            )
            e.check(
                "semantic route availability " + query["name"] + " / " + route,
                not any(
                    item["code"] in {"SEMANTIC_UNAVAILABLE", "PLAN_UNAVAILABLE"}
                    for item in response["warnings"]
                ),
                "real requested route without semantic/planning downgrade",
                response["warnings"],
                evidence=response_record,
            )


async def diagnostics(runtime, kb_id, document):
    """Direct read-only diagnostics remain possible after the shared paid-call stop gate fires."""
    e, context = runtime.e, runtime.context
    if context is None or kb_id is None:
        return
    value = {"label": "long contract final diagnostics", "diagnostic_direct_storage_read": True}
    if document:
        value["job"] = await context.metadata.job(document["job_id"])
        issues, offset = [], 0
        while True:
            page = await context.metadata.candidate_issues(document["job_id"], 1000, offset)
            issues.extend(page)
            if len(page) < 1000:
                break
            offset += len(page)
        value["candidate_issues"] = issues
        write_json(e.directory / "long-contract-issues.json", value)
    _, record = await runtime.snapshot(kb_id, "long contract final diagnostic snapshot")
    e.state["long_contract_diagnostics"] = {
        "snapshot": record,
        "issues": "long-contract-issues.json" if document else None,
    }
    e.save()


def retrieval_totals(case, queries):
    totals = {}
    for route in ("entity", "chunk", "retrieve"):
        planned = case["fixed"]["retrieve_queries" if route == "retrieve" else "positive_queries"]
        references = [
            query["reference_spans"]
            if route == "retrieve"
            else [span for span in case["spans"] if span["category"] == query["category"]]
            for query in planned
        ]
        denominator = sum(len(spans) for spans in references)
        characters = sum(
            span["char_range"][1] - span["char_range"][0] for spans in references for span in spans
        )
        selected = [item for item in queries if item["route"] == route]
        executed = [item for item in selected if item["status"] == "executed"]
        complete = len(executed) == len(planned)
        metrics = {}
        for metric in dict.fromkeys(key for item in executed for key in item["metrics"]):
            observed = [item["metrics"][metric]["aggregate"] for item in executed]
            full = sum(item["full_span_numerator"] for item in observed)
            covered = sum(item["covered_characters"] for item in observed)
            metrics[metric] = {
                "full_span_numerator_observed": full,
                "fixed_span_denominator": denominator,
                "unverified_span_count": denominator - sum(item["span_denominator"] for item in observed),
                "covered_characters_observed": covered,
                "fixed_character_denominator": characters,
                "full_span_fraction": full / denominator if complete else None,
                "micro_character_coverage": covered / characters if complete else None,
                "all_spans_full_query_numerator": sum(item["categories_all_spans_full"] for item in observed),
                "fixed_query_denominator": len(planned),
                "macro_query_mean_span_coverage": sum(
                    item["macro_category_mean_span_coverage"] for item in observed
                )
                / len(planned)
                if complete
                else None,
            }
        totals[route] = {
            "planned_queries": len(planned),
            "executed_queries": len(executed),
            "query_status_counts": dict(Counter(item["status"] for item in selected)),
            "execution_complete": complete,
            "fixed_reference_spans": denominator,
            "metrics": metrics,
        }
    return totals


def summary(e, case, invocation_calls):
    calls = [read_json(path) for path in sorted((e.directory / "model_calls").glob("*.json"))]
    current = [item for item in calls if item["call_id"] not in invocation_calls]
    graph_path, retrieval_path = (
        e.directory / "long-contract-coverage.json",
        e.directory / "long-contract-retrieval.json",
    )
    graph = read_json(graph_path) if graph_path.exists() else None
    retrieval = read_json(retrieval_path) if retrieval_path.exists() else {"queries": planned_queries(case)}
    value = {
        "schema_version": 1,
        "run_id": e.state["run_id"],
        "generated_at": now(),
        "evaluation_kind": STAGE,
        "stage": e.state["stages"].get(STAGE, {"status": "not_run"}),
        "exit_code": e.state.get("exit_code"),
        "failure": e.state.get("failure"),
        "shutdown_error": e.state.get("shutdown_error"),
        "diagnostic_error": e.state.get("diagnostic_error"),
        "source": {
            "title": case["entry"]["title"],
            "byte_length": len(case["source_bytes"]),
            "character_length": len(case["source"]),
            **case["counts"],
        },
        "frozen_at": case["freeze"]["frozen_at_utc"],
        "top_k": 5,
        "check_counts": dict(Counter(item["status"] for item in e.state["checks"].values())),
        "graph_metrics": {key: item["aggregate"] for key, item in graph["metrics"].items()}
        if graph
        else None,
        "query_results": [
            {
                key: item.get(key)
                for key in (
                    "name",
                    "route",
                    "status",
                    "response_status",
                    "response_record",
                    "reused",
                    "knowledge_count",
                    "warnings",
                    "failure_type",
                )
            }
            for item in retrieval["queries"]
        ],
        "retrieval_totals": retrieval_totals(case, retrieval["queries"]),
        "request_attempts": e.state.get("long_contract_requests", {}),
        "negative_annotations": [
            {
                "category": category,
                "qa_id": qa["id"],
                "status": "not_queried",
                "scoring": "not_applicable_to_candidate_only_service",
            }
            for category, qa in case["questions"].items()
            if qa["is_impossible"]
        ],
        "model_calls": {
            "physical_attempts_total": len(calls),
            "physical_attempts_this_invocation": len(current),
            "outcomes": dict(Counter(item.get("outcome", "started") for item in calls)),
            "by_role_and_stage": dict(Counter(item["role"] + "/" + item["language_stage"] for item in calls)),
            "records": [
                {
                    key: item.get(key)
                    for key in (
                        "call_id",
                        "role",
                        "language_stage",
                        "model",
                        "outcome",
                        "elapsed_ms",
                        "usage",
                        "input_items",
                        "error",
                    )
                }
                for item in calls
            ],
        },
        "action_provenance": {
            key: {field: item.get(field) for field in ("response_record", "executed_at", "reuse_records")}
            for key, item in e.state["actions"].items()
            if "response_record" in item
        },
        "artifacts": {
            "build": "long-contract-build.json",
            "coverage": "long-contract-coverage.json",
            "retrieval": "long-contract-retrieval.json",
            "settings": "settings.json",
            "diagnostics": e.state.get("long_contract_diagnostics"),
        },
        "semantic_correctness": "pending_independent_review",
        "limits": LIMITS,
    }
    write_json(e.directory / "long-contract-summary.json", value)
    rows = [
        "# 完整长合同专项评测",
        "",
        f"Run: {value['run_id']}",
        "",
        f"状态：{value['stage']['status']}；退出码：{value['exit_code']}。",
        "",
        f"完整原文 {len(case['source_bytes'])} bytes；官方 41 类，19 正例、30 spans、22 负例。",
        "",
        "固定 12 个官方正例问题，各执行 entity/chunk Top-K 5；另有 2 个自然语言规划问题。",
        "",
        f"已保存物理模型尝试 {len(calls)} 次，本次入口新产生 {len(current)} 次。复用响应不算新调用。",
        "",
        "| 覆盖范围 | 完整 span / 总 span | 字符覆盖 |",
        "|---|---:|---:|",
    ]
    if graph:
        for key, item in value["graph_metrics"].items():
            rows.append(
                f"| {key} | {item['full_span_numerator']} / {item['span_denominator']} | "
                f"{item['micro_character_coverage']:.3%} |"
            )
    rows += [
        "",
        "采纳定义、fact、context、仅索引切片分别统计；字符覆盖不是语义准确率。",
        "",
        "逐 span 未覆盖区间及证据见 long-contract-coverage.json；检索各路排名与实际返回证据见 "
        "long-contract-retrieval.json；失败、未运行、调用与复用见 long-contract-summary.json。",
        "",
        "官方未标注的对象不判假阳性，未查询的负例不计准确率。所有结果仍需独立语义审查。",
        "",
    ]
    (e.directory / "long-contract-summary.md").write_text("\n".join(rows), encoding="utf-8")
    return value


async def execute(args, e, case):
    from .runtime import Runtime

    invocation_calls = {path.stem for path in (e.directory / "model_calls").glob("*.json")}
    e.stage = STAGE
    # Reject changed bytes before opening dependencies or altering a previous execution's outcome.
    e.bind_sources(extra_files=sorted(CORPUS.glob("*")))
    write_json(e.directory / "long-contract-preflight.json", check())
    previous = e.state["stages"].get(STAGE, {})
    if previous.get("status") == "completed_pending_independent_review":
        e.record(
            "reuses",
            {
                "reused": True,
                "stage": STAGE,
                "meaning": "Completed frozen case reused; no new business/model execution",
            },
        )
        return e.state["exit_code"], summary(e, case, invocation_calls)
    runtime, kb_id, document, code = Runtime(e), None, None, 0
    try:
        e.stopped()
        if previous:
            e.record(
                "invocations",
                {
                    "event": "resume_previous_attempt",
                    "previous_stage": previous,
                    **{key: e.state.get(key) for key in ("failure", "diagnostic_error", "shutdown_error")},
                },
            )
        for key in ("failure", "diagnostic_error", "shutdown_error"):
            e.state.pop(key, None)
        e.state["stages"][STAGE] = {"status": "running", "started_at": now()}
        e.save()
        await runtime.start()
        kb_id = await runtime.kb("long-cuad", model())
        manifest, entry = case["manifest"], case["entry"]
        document = await runtime.api.upload(
            "long_contract.upload",
            kb_id,
            case["path"],
            source={
                "dataset": manifest["dataset"],
                "license": manifest["license"],
                "source_url": entry["source_url"],
                "title": entry["title"],
                "encoding": "utf-8",
            },
        )
        write_json(
            e.directory / "long-contract-build.json",
            {"kb_id": kb_id, **document, "status": "uploaded_job_pending"},
        )
        job = await runtime.api.job(document["job_id"])
        if job["state"] not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
            if job["state"] in {"PREPARE_FAILED", "INVALIDATED"}:
                raise CaseFailure("Saved build job is terminal; no automatic retry is performed")
            runtime.start_worker()
            job = await runtime.api.wait_job(document["job_id"], timeout=1800)
        else:
            e.record(
                "reuses",
                {
                    "reused": True,
                    "job_id": document["job_id"],
                    "meaning": "Existing successful build reused without starting a worker",
                },
            )
        await runtime.stop_worker()
        write_json(e.directory / "long-contract-build.json", {"kb_id": kb_id, **document, "job": job})
        original = await runtime.api.client.get(f"/kbs/{kb_id}/documents/{document['document_id']}/content")
        record = e.record(
            "responses",
            {
                "operation": "original TXT byte comparison",
                "status_code": original.status_code,
                "expected_bytes": len(case["source_bytes"]),
                "actual_bytes": len(original.content),
                "bytes_equal": original.content == case["source_bytes"],
            },
        )
        e.check(
            "complete original TXT bytes preserved",
            original.status_code == 200 and original.content == case["source_bytes"],
            "all frozen original bytes, unchanged",
            {"bytes_equal": original.content == case["source_bytes"]},
            evidence=record,
            required=True,
        )
        snapshot, snapshot_record = await runtime.snapshot(
            kb_id, "long contract accepted graph before fixed queries"
        )
        citation = audit_source_evidence(snapshot, {document["document_id"]: case["source"]})
        citation_record = e.record("snapshots", {"label": "long contract citation audit", **citation})
        e.check(
            "persistent source citations",
            not citation["errors"] and not citation["context_body_errors"],
            "all persisted fact/context citations map exactly to the complete original source",
            citation,
            evidence=citation_record,
        )
        graph = graph_coverage(snapshot, case, document["document_id"])
        graph["snapshot_record"] = snapshot_record
        write_json(e.directory / "long-contract-coverage.json", graph)
        e.check(
            "accepted definition source mapping",
            not graph["mapping_errors"],
            "all accepted definitions locate within their verified source evidence",
            graph["mapping_errors"],
            evidence="long-contract-coverage.json",
        )
        await retrieve_cases(runtime, case, kb_id, document["document_id"], snapshot)
        e.state["stages"][STAGE].update(status="completed_pending_independent_review", finished_at=now())
        if any(item["status"] == "fail" for item in e.state["checks"].values()):
            code = 1
            e.state["stages"][STAGE]["status"] = "failed_invariants"
    except (Exception, asyncio.CancelledError) as exc:
        code = 2 if isinstance(exc, PaidValidationStopped) else 1
        e.state["failure"] = {"stage": STAGE, "type": type(exc).__name__, "message": str(exc), "at": now()}
        e.record("failures", e.state["failure"])
        e.state["stages"].setdefault(STAGE, {}).update(
            status="stopped_model_quota_or_auth" if code == 2 else "failed", failed_at=now()
        )
    finally:
        try:
            await runtime.stop_worker()
            await diagnostics(runtime, kb_id, document)
        except Exception as exc:
            e.state["diagnostic_error"] = {"type": type(exc).__name__, "message": str(exc)}
            e.record("failures", {"phase": "diagnostics", **e.state["diagnostic_error"]})
            if code != 2:
                code = 1
        try:
            await runtime.close()
        except Exception as exc:
            e.state["shutdown_error"] = {"type": type(exc).__name__, "message": str(exc)}
            e.record("failures", {"phase": "shutdown", **e.state["shutdown_error"]})
            if code != 2:
                code = 1
        e.state["exit_code"] = code
        e.save()
    return code, summary(e, case, invocation_calls)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Offline syntax, complete case, coordinates and batch estimate only",
    )
    parser.add_argument(
        "--run-dir", type=Path, help="Reuse only this frozen long-contract evals/records/<UUIDv4>"
    )
    parser.add_argument(
        "--port", type=int, default=8766, help="Isolated loopback API port for a new run (default 8766)"
    )
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(), ensure_ascii=False, indent=2))
        return 0
    if sys.platform != "linux" or ROOT != Path("/srv/knowledge-graph"):
        parser.error("Real execution must run in WSL Ubuntu at /srv/knowledge-graph")
    if not 1024 <= args.port <= 65535:
        parser.error("Evaluation port must be between 1024 and 65535")
    case = load_case()
    new_run = args.run_dir is None or not (args.run_dir / "manifest.json").exists()
    e = Evidence(args.run_dir, port=args.port)
    if not new_run and e.state.get("evaluation_kind") != STAGE:
        parser.error("This directory belongs to another evaluation; historical runs are not changed")
    import fcntl

    lock = os.open(e.directory / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another runner owns this run directory")
        # A previous runner may have finished after our initial read but before flock succeeded.
        e = Evidence(e.directory, port=args.port)
        if e.state.get("evaluation_kind") != STAGE:
            if not new_run or e.state.get("evaluation_kind") is not None:
                parser.error("This run directory belongs to another evaluation")
            e.state.update(
                evaluation_kind=STAGE,
                classification={STAGE: "One complete 35 KB CUAD contract and its 41 official QA categories"},
            )
            e.save()
        try:
            code, value = asyncio.run(execute(args, e, case))
        except CaseFailure as exc:
            parser.error(str(exc))
    finally:
        os.close(lock)
    print(
        json.dumps(
            {
                "run_id": e.state["run_id"],
                "summary": "long-contract-summary.json",
                "stage": value["stage"]["status"],
                "check_counts": value["check_counts"],
                "new_model_attempts": value["model_calls"]["physical_attempts_this_invocation"],
                "exit_code": code,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
