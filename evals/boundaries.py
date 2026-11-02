"""Real HTTP/storage regressions for typed idempotency and maintenance capacity; no fabricated model results."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import yaml

from .evidence import Evidence, PaidValidationStopped, ROOT, now
from .runtime import Runtime


async def evaluate(evidence):
    runtime = Runtime(evidence)
    e, api = evidence, runtime.api
    exit_code = 0
    try:
        e.stopped()
        e.bind_sources()
        await runtime.start()
        e.stage = "mechanisms"
        e.state["stages"][e.stage] = {"status": "running", "started_at": now()}
        e.state["boundary_evaluation"] = (
            "typed upload source metadata and capacity checks before writes/model calls"
        )
        e.save()
        model = yaml.safe_load((ROOT / "examples/service-catalog/model.yaml").read_text())
        kb = await runtime.kb("typed-source-and-capacity", model)
        path = ROOT / "examples/service-catalog/source-a.csv"
        source = {"encoding": "utf-8", "probe": True, "nested": {"enabled": False}}
        uploaded = await api.upload("boundary.upload", kb, path, source=source)
        key = e.key("boundary.upload")
        for label, changed in (
            ("true versus one", source | {"probe": 1}),
            ("nested false versus zero", source | {"nested": {"enabled": 0}}),
        ):
            response = await api.request(
                "POST",
                f"/kbs/{kb}/documents",
                expected=409,
                headers={"Idempotency-Key": key},
                data={"source": json.dumps(changed)},
                files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
            )
            e.check(
                "typed upload idempotency " + label,
                response.get("error", response).get("code") == "IDEMPOTENCY_CONFLICT",
                "same bytes/key with different JSON value types returns IDEMPOTENCY_CONFLICT",
                response,
                evidence=api.last_record,
                required=True,
            )
        same = await api.request(
            "POST",
            f"/kbs/{kb}/documents",
            expected=202,
            headers={"Idempotency-Key": key},
            data={"source": json.dumps(dict(reversed(list(source.items()))))},
            files={"file": (path.name, path.read_bytes(), "application/octet-stream")},
        )
        e.check(
            "same typed fields retain upload identity",
            all(same[field] == uploaded[field] for field in ("document_id", "job_id")),
            uploaded,
            same,
            evidence=api.last_record,
            required=True,
        )
        e.state["stages"][e.stage].update(status="completed", finished_at=now())

        e.stage = "recovery"
        e.state["stages"][e.stage] = {"status": "running", "started_at": now()}
        e.save()
        token = runtime.start_worker("--maintenance-scan-limit", "1", "--no-model", "--once")
        await runtime.processes.exited(token)
        await runtime.stop_worker()
        job_id = uploaded["job_id"]
        rejected = await api.job(job_id)
        snapshot, record = await runtime.snapshot(
            kb, "over-capacity preparation rejected before model calls or knowledge writes"
        )
        e.check(
            "capacity rejects preparation before publishing",
            rejected["state"] == "PREPARE_FAILED"
            and rejected["error"]["code"] == "SCAN_BUDGET_EXCEEDED"
            and snapshot["kb"]["maintenance_state"] == "READY"
            and not any(
                snapshot[kind] for kind in ("entities", "relations", "contributions", "chunks", "objects")
            )
            and all(not item["active"] for item in snapshot["documents"]),
            "PREPARE_FAILED, READY, inactive document and no graph/contribution/vector changes",
            {"job": rejected, "kb": snapshot["kb"]},
            evidence=record,
            required=True,
        )
        e.check(
            "capacity rejection makes no model attempt",
            not list((e.directory / "model_calls").glob("*.json"))
            and not list((e.directory / "processes").rglob("forbidden-model-*.json")),
            "zero successful or attempted model calls",
            {"physical_calls": 0},
            required=True,
        )

        await api.mutation("boundary.retry.prepare", "POST", f"/jobs/{job_id}/retry")
        hold = str(uuid4())
        runtime.start_worker("--job-id", job_id, "--hold-prepared", hold)
        await runtime.wait_file(e.directory / "controls" / (hold + ".entered.json"))
        await runtime.stop_worker()
        prepared = await api.job(job_id)
        stored_prepared = await runtime.context.metadata.job(job_id)
        plan = runtime.context.files.read_plan(stored_prepared)
        plan_path = Path(stored_prepared["plan_path"])
        plan_bytes = plan_path.read_bytes()
        capacity = max(len(plan["chunk_points"]), len(plan["object_points"]))
        e.check(
            "real prepared plan exceeds the reduced budget",
            prepared["state"] == "PREPARED" and capacity > 1,
            "real model-backed plan remains PREPARED before any apply",
            {"job": prepared, "capacity": capacity},
            evidence=e.record("plans", {"source": str(plan_path), "plan": plan}),
            required=True,
        )
        calls_before = len(list((e.directory / "model_calls").glob("*.json")))
        e.check(
            "preparation used real embeddings",
            calls_before > 0,
            "at least one recorded actual model endpoint request",
            calls_before,
            required=True,
        )

        token = runtime.start_worker("--maintenance-scan-limit", "1", "--no-model", "--once")
        await runtime.processes.exited(token)
        await runtime.stop_worker()
        blocked = await api.job(job_id)
        stored_blocked = await runtime.context.metadata.job(job_id)
        snapshot, record = await runtime.snapshot(
            kb, "lower capacity refuses a prepared plan before draining"
        )
        e.check(
            "lower capacity does not enter maintenance or write",
            blocked["state"] == "PREPARED"
            and blocked["error"]["code"] == "SCAN_BUDGET_EXCEEDED"
            and snapshot["kb"]["maintenance_state"] == "READY"
            and not any(
                snapshot[kind] for kind in ("entities", "relations", "contributions", "chunks", "objects")
            )
            and all(not item["active"] for item in snapshot["documents"])
            and stored_blocked["plan_path"] == str(plan_path)
            and plan_path.read_bytes() == plan_bytes,
            "READY remains readable, no knowledge writes, the same complete plan remains durable",
            {"job": blocked, "kb": snapshot["kb"]},
            evidence=record,
            required=True,
        )

        runtime.start_worker("--maintenance-scan-limit", str(capacity), "--no-model")
        completed = await api.wait_job(job_id)
        await runtime.stop_worker()
        stored_completed = await runtime.context.metadata.job(job_id)
        final, record = await runtime.snapshot(kb, "same plan recovers at the exact capacity boundary")
        e.check(
            "exact capacity completes the original plan",
            completed["plan_id"] == prepared["plan_id"]
            and final["kb"]["maintenance_state"] == "READY"
            and {item["entity_id"] for item in final["entities"]}
            == {item["entity_id"] for item in plan["entities"]}
            and len(final["chunks"]) == len(plan["chunk_points"])
            and len(final["objects"]) == len(plan["object_points"])
            and all(item["active"] for item in final["documents"])
            and stored_completed["plan_path"] == str(plan_path)
            and plan_path.read_bytes() == plan_bytes,
            "same immutable plan and IDs become visible at the exact scan budget",
            completed,
            evidence=record,
            required=True,
        )
        calls_after = len(list((e.directory / "model_calls").glob("*.json")))
        e.check(
            "capacity recovery makes no model request",
            calls_after == calls_before
            and not list((e.directory / "processes").rglob("forbidden-model-*.json")),
            calls_before,
            calls_after,
            required=True,
        )
        e.state["stages"][e.stage].update(status="completed", finished_at=now())
    except BaseException as exc:
        stopped = (e.directory / "PAID_VALIDATION_STOPPED.json").exists() or isinstance(
            exc, PaidValidationStopped
        )
        exit_code = 2 if stopped else 1
        e.state["failure"] = {"stage": e.stage, "type": type(exc).__name__, "message": str(exc), "at": now()}
        e.state["stages"].setdefault(e.stage, {}).update(
            status="stopped_model_quota_or_auth" if stopped else "failed"
        )
        e.pending(
            "unexecuted boundary checks",
            "complete the fixed boundary sequence",
            {"reason": type(exc).__name__},
        )
    finally:
        try:
            await runtime.close()
        except BaseException as exc:
            e.state["shutdown_error"] = {"type": type(exc).__name__, "message": str(exc)}
            if exit_code == 0:
                exit_code = 1
        e.state["exit_code"] = exit_code
        e.save()
        result = e.report()
    print(
        json.dumps(
            {
                "run_id": e.state["run_id"],
                "exit_code": exit_code,
                "checks": result["check_counts"],
                "report": str(e.directory / "report.md"),
            },
            indent=2,
        )
    )
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    evidence = Evidence(args.run_dir)
    if evidence.state.get("source_files"):
        parser.error(
            "Boundary evaluations require a new run directory; historical results are never overwritten"
        )
    raise SystemExit(asyncio.run(evaluate(evidence)))


if __name__ == "__main__":
    main()
