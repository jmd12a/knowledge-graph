"""Compare frozen long-contract runs by route, stage and physical model attempts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROUTE_METRICS = {
    "entity": "candidate_clause_text_after_detail",
    "chunk": "initial_response_document_chunks",
    "retrieve": "initial_response_document_chunks",
}


def _calls(directory):
    counts = Counter()
    milliseconds = Counter()
    prompt_tokens, completion_tokens = Counter(), Counter()
    for path in (directory / "model_calls").glob("*.json"):
        call = json.loads(path.read_text("utf-8"))
        request = call["request"]
        if "/embeddings" in call["endpoint"]:
            stage = (
                "construction_embedding"
                if call["language_stage"] == "construction"
                else "retrieval_embedding"
            )
        else:
            system = request["messages"][0]["content"]
            if len(request["messages"]) > 2:
                stage = "schema_correction"
            elif "Correct only the listed repair_entities" in system or system.startswith(
                "Correct the evidence"
            ):
                stage = "extraction_repair"
            elif "This call reconstructs complete atomic rule" in system or system.startswith(
                "Complete only the listed literal fields"
            ):
                stage = "rule_assembly"
            elif "only source-supported relations" in system and "endpoint_catalog" in system:
                stage = "cross_batch_link"
            elif system.startswith("Extract only source-supported facts"):
                stage = "initial_extraction"
            elif system.startswith("Rank supplied source candidates"):
                stage = "retrieval_rerank"
            elif system.startswith("Return one constrained JSON retrieval plan"):
                stage = "retrieval_plan"
            else:
                stage = "other_chat"
        counts[stage] += 1
        milliseconds[stage] += call.get("elapsed_ms") or 0
        usage = call.get("usage") or {}
        prompt_tokens[stage] += usage.get("prompt_tokens") or 0
        completion_tokens[stage] += usage.get("completion_tokens") or 0
    return {
        stage: {
            "physical_calls": count,
            "elapsed_ms_sum": milliseconds[stage],
            "reported_prompt_tokens": prompt_tokens[stage],
            "reported_completion_tokens": completion_tokens[stage],
        }
        for stage, count in sorted(counts.items())
    }


def summarize(directory):
    summary = json.loads((directory / "long-contract-summary.json").read_text("utf-8"))
    build = json.loads((directory / "long-contract-build.json").read_text("utf-8"))
    settings = json.loads((directory / "settings.json").read_text("utf-8"))
    routes = {}
    for route, metric_name in ROUTE_METRICS.items():
        data = summary["retrieval_totals"][route]
        metric = data["metrics"].get(metric_name)
        routes[route] = {
            "metric": metric_name,
            "execution_complete": data["execution_complete"],
            "executed_queries": data["executed_queries"],
            "complete_queries": metric["all_spans_full_query_numerator"]
            if data["execution_complete"]
            else None,
            "observed_complete_queries": metric["all_spans_full_query_numerator"] if metric else 0,
            "queries": data["planned_queries"],
            "complete_spans": metric["full_span_numerator_observed"] if metric else 0,
            "spans": metric["fixed_span_denominator"] if metric else data["fixed_reference_spans"],
        }
    started = datetime.fromisoformat(summary["stage"]["started_at"])
    finished = datetime.fromisoformat(summary["stage"].get("finished_at") or summary["stage"]["failed_at"])
    return {
        "run_id": summary["run_id"],
        "source_bytes": summary["source"]["byte_length"],
        "scope_policy": settings.get("extraction_scope_policy", "whole"),
        "structure_window_chars": settings.get("extraction_structure_window_chars"),
        **(
            {"small_batch_chunks": settings["extraction_small_batch_chunks"]}
            if "extraction_small_batch_chunks" in settings
            else {}
        ),
        "construction_model": (settings.get("construction_llm") or settings["llm"])["model"],
        "embedding_model": settings["embedding"]["model"],
        "job_state": build["job"]["state"],
        "build_counts": build["job"]["counts"],
        "routes": routes,
        "route_complete_queries": (
            sum(item["complete_queries"] for item in routes.values())
            if all(item["execution_complete"] for item in routes.values())
            else None
        ),
        "route_query_count": sum(item["queries"] for item in routes.values()),
        "graph_spans": {
            name: {
                "full": metric["full_span_numerator"],
                "total": metric["span_denominator"],
                "character_coverage": metric["micro_character_coverage"],
            }
            for name, metric in summary["graph_metrics"].items()
        },
        "physical_calls": _calls(directory),
        "wall_seconds": round((finished - started).total_seconds(), 3),
        "semantic_precision": "not measured by this partially labeled contract",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.runs], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
