"""复跑服务目录和独立手册两组结构化多文档留出样本。"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

import yaml

from knowledge_graph.bootstrap import Context
from knowledge_graph.domain import SUCCESS_STATES, KGError
from knowledge_graph.domain.model import BusinessModel
from knowledge_graph.domain.source import validate_stored_evidence
from knowledge_graph.services.contracts import GraphRequest, SearchRequest

from .evidence import ROOT, now, read_json, write_json
from .public_corpus import checked_bytes
from .public_quality import TOP_K, ModelBudget, SearchTrace, publish, receipt, run_context

CORPUS = ROOT / "evals" / "corpus" / "holdout"
CASES = ("service-catalog", "procedures")


def load(case: str) -> tuple[dict, dict, dict]:
    directory = CORPUS / case
    fixture = read_json(directory / "questions.json")
    documents = {
        label: checked_bytes(directory / row["filename"], row["sha256"])
        for label, row in fixture["documents"].items()
    }
    if case == "service-catalog":
        path = ROOT / "examples" / "service-catalog" / "model.yaml"
        model = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        model = read_json(directory / "model.json")
    BusinessModel.model_validate(model)
    for question in fixture["questions"]:
        labels = set(question.get("docs", []))
        if labels - documents.keys():
            raise ValueError(f"留出题的文档范围无效：{question['name']}")
        for key in question["expected"] if case == "procedures" else []:
            anchor = fixture["anchors"][key]
            name = anchor["document"]
            if (labels and name not in labels) or anchor["text"].encode() not in documents[name]:
                raise ValueError(f"手册证据与冻结原文不一致：{key}")
    return fixture, documents, model


def identity(item: dict) -> str:
    fields = item.get("identity", {}).get("fields", {})
    return fields.get("app_id") or fields.get("team_code") or item["id"]


async def catalog_control(context: Context, kb: str, docs: dict) -> dict:
    entities, _ = await context.graph.all(kb)
    entity_id = next(
        row["entity_id"] for row in entities if row["identity"]["fields"].get("app_id") == "pay-core"
    )
    result = await context.retrieval.query_graph(
        kb,
        GraphRequest(
            start_ids=[entity_id],
            relation_types=["OWNED_BY"],
            filters={"document_ids": [docs[name] for name in ("catalog_a", "catalog_b")]},
        ),
    )
    names = {item["entity_id"]: identity(item) for item in result["knowledge"]}
    owners = sorted({names[row["target_id"]] for row in result["relations"]})
    return {"owners": owners, "passed": {"pay-team", "ops-team"} <= set(owners)}


async def procedures_index(context: Context, kb: str, docs: dict, anchors: dict) -> dict:
    entities, _ = await context.graph.all(kb)
    entity_ids = {row["identity"]["fields"]["record_id"]: row["entity_id"] for row in entities}
    chunks = {
        row["id"]: row["payload"]
        for row in await context.vectors.all(kb, "chunks", with_vectors=False, max_points=500)
    }
    grouped = defaultdict(list)
    for key, row in chunks.items():
        grouped[row["document_id"]].append((key, row))
    contributions = await context.metadata.contributions(kb, list(docs.values()))
    refs = defaultdict(list)
    for row in contributions:
        if row["field_path"] == "/properties/body":
            refs[(row["target_id"], row["document_id"])].extend(row["evidence"])
    results = {}
    for key, anchor in anchors.items():
        doc_id = docs[anchor["document"]]
        entity = entity_ids[key]
        text = anchor["text"]
        found_chunks = [chunk_id for chunk_id, row in grouped[doc_id] if text in row["text"]]
        found_fact = any(
            ref["role"] != "context"
            and ref["document_id"] == doc_id
            and text in validate_stored_evidence(ref, chunks[ref["chunk_id"]])
            for ref in refs[(entity, doc_id)]
        )
        if not found_chunks or not found_fact:
            raise ValueError(f"金标缺少已发布的实体引用或切片：{key}")
        results[key] = {"entity": {entity}, "chunk": set(found_chunks)}
    return results


def grade(question: dict, route: str, items: list, selected: list, anchors: dict | None) -> dict:
    expected = question["expected"]
    scoped = all(
        not selected or set(item.get("document_ids") or [item["document_id"]]) <= set(selected)
        for item in items
    )
    if anchors is not None:
        matches = [[item["id"] in anchors[key][route] for item in items] for key in expected]
        ordered = [item["id"] for item in items]
    else:
        ordered = [identity(item) for item in items]
        matches = [
            [
                key in item["text"] if route == "chunk" else key == name
                for item, name in zip(items, ordered, strict=True)
            ]
            for key in expected
        ]
    ranks = {
        key: next((rank for rank, hit in enumerate(hits, 1) if hit), None)
        for key, hits in zip(expected, matches, strict=True)
    }
    first = question.get("first")
    first_ok = first is None or bool(ordered and ordered[0] == first)
    excludes_ok = not set(ordered).intersection(question.get("forbidden", []))
    return {
        "ranks": ranks,
        "scope_ok": scoped,
        "first_ok": first_ok,
        "excludes_ok": excludes_ok,
        "passed": scoped and first_ok and excludes_ok and all(ranks.values()),
    }


async def run(args, fixture: dict, documents: dict, model: dict) -> None:
    context, directory = run_context(args.config, args.modes)
    path = directory / "holdout-quality.json"
    budget = ModelBudget(context, directory, args.max_requests)
    report = {
        "case": args.case,
        "state": "running",
        "created_at": now(),
        "config": receipt(context.settings),
        "documents": {},
        "queries": [],
    }
    print(directory, flush=True)
    try:
        await context.ready()
        await context.check_storage_mode()
        context.files.initialize()
        kb = (await context.management.create_kb(f"holdout-{args.case}-{uuid4().hex[:8]}", model))["kb_id"]
        report["kb_id"] = kb
        async with context.gate.worker():
            for label, content in documents.items():
                spec = fixture["documents"][label]
                name = spec["filename"]
                result = await publish(
                    context, kb, label, name, content, title=spec.get("title", name), mapping=spec["mapping"]
                )
                report["documents"][label] = result
                write_json(path, report)
                if result["state"] not in SUCCESS_STATES:
                    raise ValueError(f"构建未发布：{label} / {result['state']} / {result['error_code']}")
        ids = {label: row["document_id"] for label, row in report["documents"].items()}
        report["chunk_count"] = await context.vectors.count(kb, "chunks")
        anchors = None
        if args.case == "procedures":
            anchors = await procedures_index(context, kb, ids, fixture["anchors"])
        else:
            report["graph_control"] = await catalog_control(context, kb, ids)
        trace = SearchTrace(context)
        for mode in args.modes:
            trace.fusion_only = mode == "fusion"
            for round_id in range(1, args.rounds + 1):
                for q in fixture["questions"]:
                    selected = [ids[name] for name in q.get("docs", [])]
                    routes = ("entity", "chunk") if anchors is not None else (q.get("kind", "entity"),)
                    for route in routes:
                        budget.stage = f"{mode}_{route}"
                        trace.order = []
                        request = SearchRequest(
                            query=q["query"],
                            limit=TOP_K,
                            kinds=[route],
                            semantic=q.get("semantic", True),
                            filters={"document_ids": selected},
                        )
                        start = time.monotonic()
                        row = {"name": q["name"], "round": round_id, "mode": mode, "route": route}
                        try:
                            result = await context.retrieval.search_knowledge(kb, request)
                            row.update(
                                grade(q, route, result["knowledge"], selected, anchors),
                                warnings=result["warnings"],
                                fused_ids=trace.order,
                                returned_ids=[item["id"] for item in result["knowledge"]],
                            )
                        except KGError as exc:
                            row.update(error=exc.code, passed=False)
                        report["queries"].append(row | {"seconds": round(time.monotonic() - start, 3)})
                        write_json(path, report)
                        if budget.blocked:
                            raise ValueError("模型调用达到上限，本轮结果保留为未完成")
        report["state"] = "completed"
        report["totals"] = {}
        for mode in args.modes:
            rows = [row for row in report["queries"] if row["mode"] == mode]
            report["totals"][mode] = {"passed": sum(row["passed"] for row in rows), "total": len(rows)}
    finally:
        report["model_requests"] = budget.calls
        write_json(path, report)
        await context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--check", action="store_true", help="仅检查冻结原文和题目")
    parser.add_argument("--config", type=Path, help="隔离测试配置")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--modes", nargs="+", choices=("fusion", "compact"), default=["compact"])
    args = parser.parse_args()
    try:
        fixture, documents, model = load(args.case)
        if args.check:
            print(f"{args.case}：{len(documents)} 篇文档，{len(fixture['questions'])} 道题")
            return
        if not args.config or not args.max_requests or min(args.rounds, args.max_requests) < 1:
            parser.error("实际运行要求 --config、正整数 --max-requests 和 --rounds")
        asyncio.run(run(args, fixture, documents, model))
    except (OSError, ValueError, KeyError, KGError) as exc:
        parser.exit(1, f"{getattr(exc, 'code', str(exc))}\n")


if __name__ == "__main__":
    main()
