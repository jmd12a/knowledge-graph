from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from .evidence import CaseFailure, read_json, write_json
from .mechanisms import EXAMPLE, entities_by_code, model
from .process import CRASH_EXIT, hold_paths

BOUNDARIES = [
    (step, when)
    for step in ("chunks", "contributions", "graph", "objects", "finalcommit")
    for when in ("before", "after")
]


def id_sets(snapshot):
    return {
        "entities": sorted(item["entity_id"] for item in snapshot["entities"]),
        "relations": sorted(item["relation_id"] for item in snapshot["relations"]),
        "contributions": sorted(item["contribution_id"] for item in snapshot["contributions"]),
        "chunks": sorted(item["id"] for item in snapshot["chunks"]),
        "objects": sorted(item["id"] for item in snapshot["objects"]),
    }


def plan_ids(plan):
    return {
        "entities": sorted(item["entity_id"] for item in plan["entities"]),
        "relations": sorted(item["relation_id"] for item in plan["relations"]),
        "contributions": sorted(item["contribution_id"] for item in plan["contributions"]),
        "chunks": sorted(item["id"] for item in plan["chunk_points"]),
        "objects": sorted(item["id"] for item in plan["object_points"]),
    }


def stored_fields(snapshot, kind):
    field = {"entities": "entity_id", "relations": "relation_id", "contributions": "contribution_id"}.get(
        kind, "id"
    )
    return {item[field]: item for item in snapshot[kind]}


def exclusive_available(runtime, kb_id):
    import fcntl

    path = runtime.context.files.path("locks", kb_id + ".lock")
    descriptor = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


async def run(runtime):
    e, api = runtime.e, runtime.api
    await runtime.stop_worker()
    kb = await runtime.kb("recovery", model())
    baseline_doc = await api.upload(
        "recovery.baseline", kb, EXAMPLE / "recovery.csv", "calls_and_caller_owner"
    )
    baseline_job = await api.job(baseline_doc["job_id"])
    if baseline_job["state"] not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
        runtime.start_worker()
        await api.wait_job(baseline_doc["job_id"])
        await runtime.stop_worker()
    imported = await api.upload("recovery.import", kb, EXAMPLE / "recovery.csv", "calls_and_caller_owner")
    job_id = imported["job_id"]
    completed = e.state.setdefault("recovery_boundaries", [])
    expected_plan = None
    if e.saved("recovery.plan"):
        expected_plan = read_json(e.directory / e.saved("recovery.plan"))["plan"]
    for index, (step, when) in enumerate(BOUNDARIES):
        if index < len(completed):
            if (completed[index]["step"], completed[index]["when"]) != (step, when):
                raise CaseFailure("Saved recovery sequence differs from the fixed case sequence")
            continue
        job = await api.job(job_id)
        if job["attempt"] >= 3:
            # Production Worker.run holds its global lock, performs startup reconciliation, and cannot claim
            # the capped task. No test code writes a task state or invokes reconcile without that lock.
            token = runtime.start_worker("--once", "--no-model")
            code = await runtime.processes.exited(token, timeout=40)
            await runtime.stop_worker()
            coordinated = await api.job(job_id)
            e.check(
                "third-attempt startup reconciliation " + str(index),
                code == 0
                and coordinated["attempt"] == 3
                and coordinated["state"] == "NEEDS_REPAIR"
                and expected_plan is not None
                and coordinated["plan_id"] == expected_plan["plan_id"],
                "production startup leaves capped same-plan task retryable",
                coordinated,
                evidence=api.last_record,
                required=True,
            )
            retried = await api.retry_job("recovery.manual_retry." + str(index), job_id)
            e.check(
                "HTTP manual retry retains prepared IDs " + str(index),
                retried["attempt"] == 0
                and retried["plan_id"] == expected_plan["plan_id"]
                and retried["job_id"] == job_id,
                {"attempt": 0, "job_id": job_id, "plan_id": expected_plan["plan_id"]},
                retried,
                evidence=api.last_record,
                required=True,
            )
        options = ["--job-id", job_id, "--crash-step", step, "--crash-when", when]
        if expected_plan is not None:
            options.append("--no-model")
        token = runtime.start_worker(*options)
        code = await runtime.processes.exited(token)
        await runtime.stop_worker()
        checkpoint_path = e.directory / "processes" / token / "checkpoint.json"
        e.check(
            "real worker interruption " + step + " " + when,
            code == CRASH_EXIT and checkpoint_path.exists(),
            {"exit_code": CRASH_EXIT, "step": step, "when": when},
            {"exit_code": code, "token": token},
            evidence=str(checkpoint_path.relative_to(e.directory)),
            required=True,
        )
        checkpoint = read_json(checkpoint_path)
        current = await runtime.context.metadata.job(job_id)
        plan = runtime.context.files.read_plan(current)
        if expected_plan is None:
            expected_plan = plan
            path = e.record("snapshots", {"label": "immutable prepared recovery plan", "plan": plan})
            e.remember("recovery.plan", path)
            e.remember("recovery.prepared_model_calls", runtime.model_calls())
        e.check(
            "immutable plan reused " + step + " " + when,
            plan == expected_plan and checkpoint["plan_id"] == expected_plan["plan_id"],
            expected_plan["plan_id"],
            {"plan_id": plan["plan_id"], "all_plan_fields_equal": plan == expected_plan},
            evidence=str(checkpoint_path.relative_to(e.directory)),
            required=True,
        )
        snapshot, record = await runtime.snapshot(kb, "interrupted " + step + " " + when)
        actual_ids, fixed_ids = id_sets(snapshot), plan_ids(expected_plan)
        no_extra = all(
            len(ids) == len(set(ids)) and set(ids) <= set(fixed_ids[kind]) for kind, ids in actual_ids.items()
        )
        e.check(
            "no foreign or duplicate IDs " + step + " " + when,
            no_extra,
            fixed_ids,
            actual_ids,
            evidence=record,
        )
        is_committed = step == "finalcommit" and when == "after"
        gate = await api.request("GET", f"/kbs/{kb}/model", expected=200 if is_committed else 503)
        if not is_committed:
            e.check(
                "knowledge gate stays closed " + step + " " + when,
                gate.get("code") == "KB_MAINTENANCE" and snapshot["kb"]["maintenance_state"] != "READY",
                "KB_MAINTENANCE; not READY",
                gate,
                evidence=api.last_record,
            )
            mcp = await api.mcp("describe_model", {"kb_id": kb})
            e.check(
                "MCP gate stays closed " + step + " " + when,
                mcp.get("code") == gate.get("code") == "KB_MAINTENANCE",
                "KB_MAINTENANCE",
                mcp,
                evidence=api.last_record,
            )
        else:
            e.check(
                "committed final transaction is immediately readable",
                snapshot["kb"]["maintenance_state"] == "READY"
                and current["state"] in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"},
                "READY and successful task despite lost worker acknowledgement",
                current,
                evidence=record,
            )
        e.check(
            "replay makes no new model calls " + step + " " + when,
            runtime.model_calls() == e.saved("recovery.prepared_model_calls"),
            e.saved("recovery.prepared_model_calls"),
            runtime.model_calls(),
            evidence=record,
        )
        completed.append(
            {
                "step": step,
                "when": when,
                "process_token": token,
                "plan_id": expected_plan["plan_id"],
                "evidence": record,
            }
        )
        e.save()
    if not e.saved("recovery.final_verified"):
        await verify_final(runtime, kb, job_id, expected_plan)
    await read_isolation(runtime, kb, baseline_doc, cancel=False)
    await read_isolation(runtime, kb, imported, cancel=True)
    blocked = list((e.directory / "processes").glob("*/forbidden-model-*.json"))
    e.check(
        "read isolation and source withdrawal reuse prepared vectors",
        not blocked and runtime.model_calls() == e.saved("recovery.prepared_model_calls"),
        "no new actual or attempted model calls during either read/isolation lifecycle",
        {"blocked_attempts": [str(path) for path in blocked], "model_call_records": runtime.model_calls()},
    )


async def verify_final(runtime, kb, job_id, expected_plan):
    e, api = runtime.e, runtime.api
    token = runtime.start_worker("--once", "--no-model")
    code = await runtime.processes.exited(token, timeout=40)
    await runtime.stop_worker()
    final, final_record = await runtime.snapshot(kb, "restarted after final commit")
    final_job = await api.job(job_id)
    e.check(
        "post-commit restart cannot revert to repair",
        code == 0
        and final["kb"]["maintenance_state"] == "READY"
        and final_job["state"] in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"},
        "successful READY remains successful READY",
        final_job,
        evidence=final_record,
    )
    e.check(
        "recovered final ID sets equal prepared ID sets",
        id_sets(final) == plan_ids(expected_plan),
        plan_ids(expected_plan),
        id_sets(final),
        evidence=final_record,
    )
    fields_equal = all(
        {item[key]: item for item in final[kind]} == {item[key]: item for item in expected_plan[kind]}
        for kind, key in (
            ("entities", "entity_id"),
            ("relations", "relation_id"),
            ("contributions", "contribution_id"),
        )
    )
    payloads_equal = all(
        {point["id"]: point["payload"] for point in final[kind]}
        == {
            point["id"]: point["payload"]
            for point in expected_plan["chunk_points" if kind == "chunks" else "object_points"]
        }
        for kind in ("chunks", "objects")
    )
    e.check(
        "recovered business fields and index payloads equal plan",
        fields_equal and payloads_equal,
        "complete graph/contribution business fields and both index payloads match immutable plan",
        {"graph_and_claim_fields_equal": fields_equal, "index_payloads_equal": payloads_equal},
        evidence=final_record,
    )
    blocked = list((e.directory / "processes").glob("*/forbidden-model-*.json"))
    e.check(
        "prepared replay never attempts LLM or embedding",
        not blocked and runtime.model_calls() == e.saved("recovery.prepared_model_calls"),
        "zero new actual or attempted model calls after preparation",
        {"blocked_attempts": [str(path) for path in blocked], "model_call_records": runtime.model_calls()},
        evidence=final_record,
    )
    e.remember("recovery.final_verified", {"plan_id": expected_plan["plan_id"], "evidence": final_record})


async def read_isolation(runtime, kb, imported, *, cancel):
    e, api = runtime.e, runtime.api
    label = "cancelled" if cancel else "normal"
    if e.saved("recovery.read_done." + label):
        return
    await runtime.stop_worker()
    baseline, baseline_record = await runtime.snapshot(kb, label + " read baseline")
    nodes = entities_by_code(baseline)
    selected_id = nodes["pay-core"]["entity_id"]
    token = str(uuid4())
    entered, release = hold_paths(e.directory, token)
    prepared_release = None
    if not cancel:
        mutation = await api.mutation(
            "recovery.normal_delete_shared_source", "DELETE", f"/kbs/{kb}/documents/{imported['document_id']}"
        )
        prepared_token = str(uuid4())
        prepared_entered, prepared_release = hold_paths(e.directory, prepared_token)
        runtime.start_worker("--job-id", mutation["job_id"], "--hold-prepared", prepared_token, "--no-model")
        await runtime.wait_file(prepared_entered)
    write_json(
        e.directory / "controls" / "read-hold.json",
        {"token": token, "kb_id": kb, "object_id": selected_id, "cancel_request_task": cancel},
    )
    read_task = asyncio.create_task(api.request("GET", f"/kbs/{kb}/knowledge/entity/{selected_id}"))
    try:
        await runtime.wait_file(entered, timeout=40)
        if cancel:
            mutation = await api.mutation(
                "recovery.cancelled_delete", "DELETE", f"/kbs/{kb}/documents/{imported['document_id']}"
            )
            runtime.start_worker("--no-model")
        else:
            write_json(prepared_release, {"release": True})
        await runtime.wait_state(mutation["job_id"], "DRAINING")
        draining, draining_record = await runtime.snapshot(kb, label + " reader holds lock while draining")
        e.check(
            "in-flight read prevents writes " + label,
            all(
                stored_fields(draining, kind) == stored_fields(baseline, kind)
                for kind in ("entities", "relations", "contributions", "chunks", "objects")
            )
            and not exclusive_available(runtime, kb),
            "real storage unchanged until in-flight read finishes",
            {
                "exclusive_available": exclusive_available(runtime, kb),
                "job": await api.job(mutation["job_id"]),
            },
            evidence=draining_record,
            required=True,
        )
        new_http = await api.request("GET", f"/kbs/{kb}/model", expected=503)
        new_mcp = await api.mcp("describe_model", {"kb_id": kb})
        e.check(
            "DRAINING rejects new HTTP and MCP " + label,
            new_http.get("code") == new_mcp.get("code") == "KB_MAINTENANCE",
            "both KB_MAINTENANCE",
            {"HTTP": new_http, "MCP": new_mcp},
            evidence=api.last_record,
        )
        if cancel:
            # Cancel the actual in-flight server request while its blocking adapter operation is pending.
            # A client disconnect alone is not guaranteed to cancel an ASGI handler in every server version.
            write_json(e.directory / "controls" / (token + ".cancel.json"), {"cancel": True})
            await runtime.wait_file(e.directory / "controls" / (token + ".cancel-observed.json"), timeout=10)
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.2)
            held = not exclusive_available(runtime, kb)
            not_exited = not (e.directory / "controls" / (token + ".exited.json")).exists()
            state = await api.job(mutation["job_id"])
            e.check(
                "request cancellation drains background read before unlock",
                held and not_exited and state["state"] == "DRAINING",
                "shared lock held and no write while cancelled thread operation remains active",
                {"exclusive_available": not held, "reader_exited": not not_exited, "job": state},
                evidence=draining_record,
                required=True,
            )
        write_json(release, {"release": True})
        if not cancel:
            returned = await read_task
            e.check(
                "in-flight request returns consistent old knowledge",
                returned["item"]["entity_id"] == selected_id
                and returned["item"]["contribution_ids"] == nodes["pay-core"]["contribution_ids"],
                "original source-supported object completes before maintenance writes",
                returned,
                evidence=api.last_record,
            )
        await runtime.wait_file(e.directory / "controls" / (token + ".exited.json"), timeout=15)
        await api.wait_job(mutation["job_id"])
        after, after_record = await runtime.snapshot(kb, label + " read released and maintenance completed")
        e.check(
            "maintenance completes after read exit " + label,
            after["kb"]["maintenance_state"] == "READY" and exclusive_available(runtime, kb),
            "reader finished; task complete; READY",
            after["kb"],
            evidence=after_record,
        )
        if cancel:
            e.check(
                "cancelled-read deletion has no residual knowledge",
                not any(
                    after[kind] for kind in ("entities", "relations", "contributions", "chunks", "objects")
                ),
                "empty after last-source deletion",
                id_sets(after),
                evidence=after_record,
            )
        e.remember(
            "recovery.read_done." + label,
            {"baseline": baseline_record, "final": after_record, "job_id": mutation["job_id"]},
        )
    finally:
        write_json(release, {"release": True})
        if prepared_release is not None:
            write_json(prepared_release, {"release": True})
        if not read_task.done():
            read_task.cancel()
        try:
            await read_task
        except (asyncio.CancelledError, Exception):
            pass
        control = e.directory / "controls" / "read-hold.json"
        if control.exists() and read_json(control)["token"] == token:
            control.unlink()
        await runtime.stop_worker()
