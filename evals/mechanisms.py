from __future__ import annotations

import copy
import json
from uuid import uuid4

import yaml

from .evidence import ROOT, read_json, write_json
from .process import hold_paths

EXAMPLE = ROOT / "examples" / "service-catalog"


def model():
    return yaml.safe_load((EXAMPLE / "model.yaml").read_text("utf-8"))


def values(item, field):
    return [value["value"] for value in item.get("properties", {}).get(field, {}).get("values", [])]


def entities_by_code(snapshot):
    return {next(iter(item["identity"]["fields"].values())): item for item in snapshot["entities"]}


def edge_map(snapshot):
    codes = {
        item["entity_id"]: next(iter(item["identity"]["fields"].values())) for item in snapshot["entities"]
    }
    return {
        (codes[item["source_id"]], item["relation_type"], codes[item["target_id"]]): item
        for item in snapshot["relations"]
    }


def graph_coverage(result):
    return next((item for item in result["coverage"] if item["component"] == "graph"), {})


async def run(runtime):
    e, api = runtime.e, runtime.api
    kb = await runtime.kb("mechanisms", model())
    runtime.start_worker()
    a = await api.upload("mechanisms.import_a", kb, EXAMPLE / "source-a.csv", "calls_and_caller_owner")
    await api.wait_job(a["job_id"])
    snap_a, rec_a = await runtime.snapshot(kb, "A imported")
    before = entities_by_code(snap_a)
    e.check(
        "CSV mapped identities",
        set(before)
        == {"order", "pay-core", "pay-demo", "ledger", "order-team", "pay-team", "demo-team", "ledger-team"},
        "four services and four teams",
        sorted(before),
        evidence=rec_a,
        required=True,
    )
    b = await api.upload("mechanisms.import_b", kb, EXAMPLE / "source-b.json", "calls_and_callee_owner")
    b_job = await api.wait_job(b["job_id"])
    both, rec_b = await runtime.snapshot(kb, "A and B imported")
    nodes, edges = entities_by_code(both), edge_map(both)
    e.check(
        "same business identity reuses ID",
        all(nodes[code]["entity_id"] == item["entity_id"] for code, item in before.items()),
        {code: item["entity_id"] for code, item in before.items()},
        {code: nodes[code]["entity_id"] for code in before},
        evidence=rec_b,
    )
    pay, demo = nodes["pay-core"], nodes["pay-demo"]
    pay_id, order_id, ledger_id = pay["entity_id"], nodes["order"]["entity_id"], nodes["ledger"]["entity_id"]
    common = edges[("order", "CALLS", "pay-core")]
    e.check(
        "same name different identity stays separate",
        pay_id != demo["entity_id"] and pay["canonical_name"] == demo["canonical_name"] == "支付服务",
        "two distinct IDs named 支付服务",
        [pay_id, demo["entity_id"]],
        evidence=rec_b,
    )
    e.check(
        "shared relationship retains both sources",
        set(common["document_ids"]) == {a["document_id"], b["document_id"]},
        sorted([a["document_id"], b["document_id"]]),
        common["document_ids"],
        evidence=rec_b,
    )
    expected_calls = {
        ("order", "CALLS", "pay-core"),
        ("pay-core", "CALLS", "ledger"),
        ("ledger", "CALLS", "order"),
        ("order", "CALLS", "pay-demo"),
        ("pay-demo", "CALLS", "ledger"),
        ("audit", "CALLS", "pay-core"),
    }
    e.check(
        "directed call graph",
        {key for key in edges if key[1] == "CALLS"} == expected_calls,
        sorted(expected_calls),
        sorted(key for key in edges if key[1] == "CALLS"),
        evidence=rec_b,
    )
    e.check(
        "single value conflict is retained",
        set(values(pay, "status")) == {"active", "degraded"}
        and pay["properties"]["status"]["state"] == "conflicted",
        ["active", "degraded"],
        pay["properties"]["status"],
        evidence=rec_b,
    )
    owners = [item for key, item in edges.items() if key[0] == "pay-core" and key[1] == "OWNED_BY"]
    e.check(
        "unique ownership conflict is disclosed",
        len(owners) == 2
        and all(item["conflicts"] for item in owners)
        and any(
            warning["code"] == "SOURCE_CONFLICT" for warning in b_job.get("counts", {}).get("warnings", [])
        ),
        "pay-team and ops-team claims, edge conflicts, job warning",
        owners,
        evidence=rec_b,
    )

    repeated = await api.upload(
        "mechanisms.import_a", kb, EXAMPLE / "source-a.csv", "calls_and_caller_owner", replay=True
    )
    e.check(
        "idempotent same bytes and key",
        (repeated["document_id"], repeated["job_id"]) == (a["document_id"], a["job_id"]),
        a,
        repeated,
        evidence=api.last_record,
    )
    original = (EXAMPLE / "source-a.csv").read_bytes()
    changed = original.replace(b"ONLY_A_PAY_ALIAS", b"ONLY_X_PAY_ALIAS")
    conflict = await api.request(
        "POST",
        f"/kbs/{kb}/documents",
        expected=409,
        headers={"Idempotency-Key": e.key("mechanisms.import_a")},
        data={
            "mapping_id": "calls_and_caller_owner",
            "source": json.dumps(
                {"dataset": "artificial-service-mechanisms", "file": "source-a.csv", "encoding": "utf-8"}
            ),
        },
        files={"file": ("source-a.csv", changed, "application/octet-stream")},
    )
    e.check(
        "idempotency compares same-length differing bytes",
        len(changed) == len(original) and conflict.get("code") == "IDEMPOTENCY_CONFLICT",
        "409 IDEMPOTENCY_CONFLICT",
        conflict,
        evidence=api.last_record,
    )
    await runtime.stop_worker()
    await runtime.restart_api()
    runtime.start_worker()
    replay = await api.upload(
        "mechanisms.import_a", kb, EXAMPLE / "source-a.csv", "calls_and_caller_owner", replay=True
    )
    e.check(
        "idempotency survives API and Worker restart",
        replay["document_id"] == a["document_id"] and replay["job_id"] == a["job_id"],
        a,
        replay,
        evidence=api.last_record,
    )

    described = await api.request("GET", f"/kbs/{kb}/model")
    await api.parity("describe_model", kb, described)
    request = {"query": "pay-core", "semantic": False}
    searched = await api.request("POST", f"/kbs/{kb}/knowledge/search", json=request)
    e.check(
        "exact business ID ranks first",
        bool(searched["knowledge"])
        and searched["knowledge"][0]["entity_id"] == pay_id
        and "exact_identity" in searched["knowledge"][0]["match_channels"],
        pay_id,
        searched["knowledge"],
        evidence=api.last_record,
    )
    await api.parity("search_knowledge", kb, searched, request=request)
    detail = await api.request("GET", f"/kbs/{kb}/knowledge/entity/{pay_id}")
    await api.parity("get_knowledge", kb, detail, kind="entity", object_id=pay_id)
    incoming_request = {
        "start_ids": [pay_id],
        "relation_types": ["CALLS"],
        "direction": "incoming",
        "mode": "neighbors",
    }
    incoming = await api.request("POST", f"/kbs/{kb}/graph/query", json=incoming_request)
    e.check(
        "incoming callers enumerate graph facts",
        {item["source_id"] for item in incoming["relations"]} == {order_id, nodes["audit"]["entity_id"]}
        and all(item["target_id"] == pay_id for item in incoming["relations"])
        and graph_coverage(incoming).get("mode") == "bounded_complete",
        "order and audit -> pay-core",
        incoming,
        evidence=api.last_record,
    )
    await api.parity("query_graph", kb, incoming, request=incoming_request)
    paths = await api.request(
        "POST",
        f"/kbs/{kb}/graph/query",
        json={
            "start_ids": [order_id],
            "end_ids": [ledger_id],
            "relation_types": ["CALLS"],
            "direction": "outgoing",
            "mode": "paths",
            "max_hops": 3,
            "evidence_limit": 1000,
        },
    )
    expected_paths = {(order_id, pay_id, ledger_id), (order_id, demo["entity_id"], ledger_id)}
    e.check(
        "bounded simple paths and edge evidence",
        {tuple(item["entity_ids"]) for item in paths["paths"]} == expected_paths
        and all(
            len(path["entity_ids"]) == len(set(path["entity_ids"]))
            and path["hops"] <= 3
            and all(item["evidence_indices"] for item in path["edge_evidence"])
            for path in paths["paths"]
        ),
        [list(item) for item in expected_paths],
        paths,
        evidence=api.last_record,
    )
    budget = await api.request(
        "POST",
        f"/kbs/{kb}/graph/query",
        json={"start_ids": [order_id], "relation_types": ["CALLS"], "edge_limit": 1},
    )
    e.check(
        "edge budget reports incomplete coverage",
        graph_coverage(budget).get("reason") == "EDGE_BUDGET"
        and graph_coverage(budget).get("mode") == "bounded_partial"
        and not graph_coverage(budget).get("execution_complete"),
        "bounded_partial EDGE_BUDGET",
        budget,
        evidence=api.last_record,
    )
    evidence_budget = await api.request(
        "POST", f"/kbs/{kb}/graph/query", json={**incoming_request, "evidence_limit": 0}
    )
    e.check(
        "graph completeness independent of evidence budget",
        graph_coverage(evidence_budget).get("mode") == "bounded_complete"
        and any(
            item["component"] == "evidence" and item["truncated"] for item in evidence_budget["coverage"]
        ),
        "complete graph; truncated evidence",
        evidence_budget,
        evidence=api.last_record,
    )
    scoped = await api.request(
        "POST",
        f"/kbs/{kb}/graph/query",
        json={
            "start_ids": [pay_id],
            "relation_types": ["OWNED_BY"],
            "filters": {"document_ids": [b["document_id"]]},
        },
    )
    scoped_pay = next(item for item in scoped["knowledge"] if item["entity_id"] == pay_id)
    e.check(
        "selected-document declarations exclude other sources",
        scoped.get("scope") == "selected_documents"
        and len(scoped["relations"]) == 1
        and scoped["relations"][0]["target_id"] == nodes["ops-team"]["entity_id"]
        and values(scoped_pay, "status") == ["degraded"]
        and not values(scoped_pay, "aliases")
        and "description" not in scoped_pay
        and all(item["document_id"] == b["document_id"] for item in scoped["evidence"]),
        "only B-supported degraded status/ops-team; no A alias or description",
        scoped,
        evidence=api.last_record,
    )
    await api.parity(
        "query_graph",
        kb,
        scoped,
        request={
            "start_ids": [pay_id],
            "relation_types": ["OWNED_BY"],
            "filters": {"document_ids": [b["document_id"]]},
        },
    )
    semantic = await api.paid_request(
        "mechanisms.semantic",
        f"/kbs/{kb}/knowledge/search",
        {"query": "支付服务", "semantic": True, "limit": 10},
    )
    e.check(
        "real semantic channel executes",
        any("vector" in item.get("match_channels", []) for item in semantic["knowledge"])
        and not any(item["code"] == "SEMANTIC_UNAVAILABLE" for item in semantic["warnings"]),
        "real embedding/vector candidates",
        semantic,
        evidence=api.last_record,
    )
    natural = await api.paid_request(
        "mechanisms.ambiguity", f"/kbs/{kb}/retrieve", {"query": "支付服务的调用者有哪些"}
    )
    unresolved = natural.get("unresolved_entities", [])
    ambiguous_ids = {item["entity_id"] for mention in unresolved for item in mention.get("candidates", [])}
    e.check(
        "natural-language ambiguity is explicit",
        natural["status"] == "needs_disambiguation" and {pay_id, demo["entity_id"]} <= ambiguous_ids,
        "both 支付服务 candidates; no guessed graph start",
        natural,
        evidence=api.last_record,
    )
    if unresolved:
        mention = next(
            (
                item
                for item in unresolved
                if pay_id in {candidate["entity_id"] for candidate in item.get("candidates", [])}
            ),
            None,
        )
        if mention:
            selected = await api.paid_request(
                "mechanisms.resolved",
                f"/kbs/{kb}/retrieve",
                {
                    "query": "支付服务的调用者有哪些",
                    "resolved_entities": [
                        {
                            "mention": mention["mention"],
                            "query_range": mention["query_range"],
                            "entity_id": pay_id,
                        }
                    ],
                },
            )
            e.check(
                "explicit selection resolves natural query",
                selected["status"] != "needs_disambiguation"
                and {item["relation_id"] for item in selected["relations"]}
                == {item["relation_id"] for item in incoming["relations"]},
                "the same two incoming CALLS edges",
                selected,
                evidence=api.last_record,
            )
    direct = await api.request("POST", f"/kbs/{kb}/retrieve", json={"query": pay_id})
    await api.parity("retrieve", kb, direct, request={"query": pay_id})

    isolate = await runtime.kb("isolation-maintenance", model())
    isolated_doc = await api.upload(
        "mechanisms.isolation", isolate, EXAMPLE / "isolation.csv", "calls_and_caller_owner"
    )
    await api.wait_job(isolated_doc["job_id"])
    isolated, isolated_rec = await runtime.snapshot(isolate, "isolated same-code service")
    isolated_pay = entities_by_code(isolated)["pay-core"]
    found_isolated = await api.request(
        "POST", f"/kbs/{isolate}/knowledge/search", json={"query": "pay-core", "semantic": False}
    )
    e.check(
        "business identity and keyword queries are KB-isolated",
        isolated_pay["entity_id"] != pay_id
        and found_isolated["knowledge"]
        and all(
            item["kb_id"] == isolate and item["entity_id"] != pay_id for item in found_isolated["knowledge"]
        ),
        "different object IDs for the same app_id in separate KBs",
        found_isolated,
        evidence=isolated_rec,
    )
    foreign = await api.request("GET", f"/kbs/{isolate}/knowledge/entity/{pay_id}", expected=404)
    await api.parity("get_knowledge", isolate, foreign, kind="entity", object_id=pay_id)
    foreign_source = await api.request(
        "GET", f"/kbs/{isolate}/documents/{a['document_id']}/content", expected=404
    )
    e.check(
        "foreign document content is absent",
        foreign_source.get("code") == "NOT_FOUND",
        "NOT_FOUND",
        foreign_source,
        evidence=api.last_record,
    )

    deleted_a = await api.mutation("mechanisms.delete_a", "DELETE", f"/kbs/{kb}/documents/{a['document_id']}")
    await api.wait_job(deleted_a["job_id"])
    remaining, rec_remaining = await runtime.snapshot(kb, "A withdrawn")
    remaining_nodes, remaining_edges = entities_by_code(remaining), edge_map(remaining)
    surviving = remaining_nodes["pay-core"]
    surviving_common = remaining_edges[("order", "CALLS", "pay-core")]
    e.check(
        "shared relationship ID and remaining source survive deletion",
        surviving_common["relation_id"] == common["relation_id"]
        and surviving_common["document_ids"] == [b["document_id"]],
        common["relation_id"],
        surviving_common,
        evidence=rec_remaining,
    )
    text_after = json.dumps(remaining, ensure_ascii=False)
    e.check(
        "A-only fields descriptions and vector text are withdrawn",
        not values(surviving, "aliases")
        and not values(surviving, "description")
        and surviving["description"] == ""
        and "ONLY_A_PAY_ALIAS" not in text_after
        and "ONLY_A_PAY_DESCRIPTION" not in text_after
        and all(item["document_id"] == b["document_id"] for item in remaining["contributions"]),
        "no A-only aliases/descriptions/index payload or contributions",
        surviving,
        evidence=rec_remaining,
    )
    e.check(
        "identity-only endpoint recomputed",
        surviving["completeness"] == "identity_only"
        and "name" in surviving["missing_fields"]
        and not values(surviving, "name"),
        "identity_only; name missing",
        surviving,
        evidence=rec_remaining,
    )
    e.check(
        "conflicts disappear after opposing source deletion",
        values(surviving, "status") == ["degraded"]
        and not any(item["conflicts"] for item in remaining["entities"] + remaining["relations"]),
        "B status and ownership only; no conflicts",
        remaining,
        evidence=rec_remaining,
    )
    await api.request("GET", f"/kbs/{kb}/documents/{a['document_id']}/content", expected=404)
    deleted_b = await api.mutation("mechanisms.delete_b", "DELETE", f"/kbs/{kb}/documents/{b['document_id']}")
    await api.wait_job(deleted_b["job_id"])
    empty, rec_empty = await runtime.snapshot(kb, "last source withdrawn")
    counts = {
        kind: len(empty[kind]) for kind in ("entities", "relations", "contributions", "chunks", "objects")
    }
    e.check(
        "last source removes graph contributions and both indexes",
        not any(counts.values()),
        {key: 0 for key in counts},
        counts,
        evidence=rec_empty,
    )
    await api.request("GET", f"/kbs/{kb}/documents/{b['document_id']}/content", expected=404)
    stale = await api.request(
        "POST",
        f"/jobs/{a['job_id']}/retry",
        expected=409,
        headers={"Idempotency-Key": e.key("mechanisms.stale_import")},
    )
    e.check(
        "old IMPORT cannot revive deleted source",
        stale.get("code") == "INVALID_JOB_STATE",
        "INVALID_JOB_STATE",
        stale,
        evidence=api.last_record,
    )
    duplicate = await api.mutation(
        "mechanisms.duplicate_delete", "DELETE", f"/kbs/{kb}/documents/{a['document_id']}"
    )
    duplicate_job = await api.wait_job(duplicate["job_id"])
    e.check(
        "different-key duplicate DELETE completes idempotently",
        duplicate_job["state"] == "SUCCEEDED"
        and duplicate_job.get("counts", {}).get("already_deleted") is True,
        "already_deleted without resurrecting raw source",
        duplicate_job,
        evidence=api.last_record,
    )
    await maintenance(runtime, isolate, isolated_doc, isolated)


async def maintenance(runtime, kb, active_doc, baseline):
    from knowledge_graph.domain import BusinessModel

    e, api = runtime.e, runtime.api
    extra = await api.upload(
        "mechanisms.maintenance_deleted", kb, EXAMPLE / "recovery.csv", "calls_and_caller_owner"
    )
    await api.wait_job(extra["job_id"])
    deletion = await api.mutation(
        "mechanisms.maintenance_delete", "DELETE", f"/kbs/{kb}/documents/{extra['document_id']}"
    )
    await api.wait_job(deletion["job_id"])
    invalid = e.directory / "inputs" / "inactive.json"
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_text('{"calls": [', encoding="utf-8")
    inactive = await api.upload("mechanisms.maintenance_inactive", kb, invalid, "calls_and_callee_owner")
    failed = await api.wait_job(inactive["job_id"], allow_failure=True)
    e.check(
        "invalid source stays inactive",
        failed["state"] == "PREPARE_FAILED",
        "PREPARE_FAILED",
        failed,
        evidence=api.last_record,
    )
    await runtime.stop_worker()
    original = await api.request("GET", f"/kbs/{kb}/model")
    target = copy.deepcopy(model())
    target["name"] = "service-catalog-rebuilt"
    target["node_types"]["Service"]["properties"]["tier"] = {
        "type": "string",
        "merge": "single",
        "queryable": True,
    }
    target = BusinessModel.model_validate(target).model_dump(mode="json")
    job = await api.mutation("mechanisms.rebuild", "POST", f"/kbs/{kb}/model", body={"model": target})
    hold = str(uuid4())
    entered, release = hold_paths(e.directory, hold)
    runtime.start_worker("--job-id", job["job_id"], "--hold-prepared", hold)
    event = await runtime.wait_file(entered)
    before_apply = await api.request("GET", f"/kbs/{kb}/model")
    plan = runtime.context.files.read_plan(await runtime.context.metadata.job(job["job_id"]))
    e.check(
        "REBUILD target is persisted while old knowledge remains readable",
        before_apply["model"] == original["model"]
        and plan["model"] == target
        and plan["document_ids"] == [active_doc["document_id"]],
        {"active_only": [active_doc["document_id"]], "target_model": target},
        {"event": event, "document_ids": plan["document_ids"], "model": plan["model"]},
        evidence=e.record("snapshots", {"label": "prepared REBUILD plan", "plan": plan}),
    )
    write_json(release, {"release": True})
    await api.wait_job(job["job_id"])
    rebuilt, rec = await runtime.snapshot(kb, "REBUILD completed")
    e.check(
        "REBUILD uses active sources and publishes requested configuration",
        rebuilt["kb"]["model"] == target
        and {item["document_id"] for item in rebuilt["contributions"]} == {active_doc["document_id"]}
        and {item["entity_id"] for item in rebuilt["entities"]}
        == {item["entity_id"] for item in baseline["entities"]},
        "active-only target model and reused compatible identity IDs",
        rebuilt,
        evidence=rec,
    )
    await runtime.stop_worker()
    profile = copy.deepcopy(rebuilt["kb"]["embedding_profile"])
    profile.update(distance="Dot", revision_id=str(uuid4()))
    reindex = await api.mutation(
        "mechanisms.reindex", "POST", f"/kbs/{kb}/reindex", body={"embedding_profile": profile}
    )
    hold = str(uuid4())
    entered, release = hold_paths(e.directory, hold)
    runtime.start_worker("--job-id", reindex["job_id"], "--hold-prepared", hold)
    await runtime.wait_file(entered)
    current = await runtime.context.metadata.kb(kb)
    plan = runtime.context.files.read_plan(await runtime.context.metadata.job(reindex["job_id"]))
    readable = await api.request("GET", f"/kbs/{kb}/model")
    e.check(
        "REINDEX prepares a fixed target without early publication",
        current["embedding_profile"] == rebuilt["kb"]["embedding_profile"]
        and plan["embedding_profile"] == profile
        and readable["model"] == target,
        "old Cosine profile readable until apply; persisted Dot target",
        {"current": current, "plan_profile": plan["embedding_profile"]},
        evidence=e.record("snapshots", {"label": "prepared REINDEX plan", "plan": plan}),
    )
    write_json(release, {"release": True})
    await api.wait_job(reindex["job_id"])
    indexed, rec = await runtime.snapshot(kb, "REINDEX completed")
    e.check(
        "REINDEX keeps graph and contribution IDs",
        indexed["entities"] == rebuilt["entities"]
        and indexed["relations"] == rebuilt["relations"]
        and {item["contribution_id"] for item in indexed["contributions"]}
        == {item["contribution_id"] for item in rebuilt["contributions"]},
        "unchanged graph objects and contribution IDs",
        indexed,
        evidence=rec,
    )
    configured = []
    for kind in ("objects", "chunks"):
        info = await runtime.context.vectors.client.get_collection(
            runtime.context.vectors.collection(kb, kind)
        )
        configured.append(
            {
                "kind": kind,
                "size": info.config.params.vectors.size,
                "distance": info.config.params.vectors.distance.value,
            }
        )
    e.check(
        "REINDEX publishes fixed profile and both collections",
        indexed["kb"]["embedding_profile"] == profile
        and all(item["distance"] == "Dot" and item["size"] == 1024 for item in configured)
        and all(
            item["payload"]["embedding_profile"] == profile for item in indexed["objects"] + indexed["chunks"]
        ),
        "Dot 1024 profile in metadata, collections and payloads",
        configured,
        evidence=rec,
    )
    for source, expected in ((active_doc, True), (inactive, False), (extra, False)):
        doc = await api.request("GET", f"/kbs/{kb}/documents/{source['document_id']}")
        e.check(
            "maintenance source activity " + source["document_id"],
            bool(doc["active"]) == expected,
            expected,
            doc,
            evidence=api.last_record,
        )
    # Store mechanical evidence for manual inspection without running any model again.
    write_json(
        e.directory / "mechanisms-summary.json",
        {
            "kb_id": kb,
            "active_document_id": active_doc["document_id"],
            "rebuild_job": job["job_id"],
            "reindex_job": reindex["job_id"],
            "baseline_snapshot": read_json(e.directory / rec),
        },
    )
