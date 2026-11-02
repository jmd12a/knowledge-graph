"""Run isolated, evidence-preserving evaluations against the real service and dependencies."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

from .evidence import ROOT, STAGES, Evidence, PaidValidationStopped, now


def check():
    """Local schema and syntax checks only: no settings/env loader, database, network, or subprocess."""
    from knowledge_graph.domain import BusinessModel

    from . import contract, mechanisms

    for path in sorted((ROOT / "evals").glob("*.py")):
        compile(path.read_text("utf-8"), str(path), "exec", dont_inherit=True)
    configured = [
        BusinessModel.model_validate(mechanisms.model()),
        BusinessModel.model_validate(contract.model()),
    ]
    service = configured[0]
    for filename in ("source-a.csv", "recovery.csv", "isolation.csv"):
        with (mechanisms.EXAMPLE / filename).open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = set(reader.fieldnames or [])
            mapped = service.mappings["calls_and_caller_owner"]
            required = {
                spec["column"] for entity in mapped["entities"].values() for spec in entity["fields"].values()
            }
            if not required <= columns or not list(reader):
                raise ValueError("Example CSV does not match its declared mapping: " + filename)
    source_b = json.loads((mechanisms.EXAMPLE / "source-b.json").read_text("utf-8"))
    if not isinstance(source_b.get("calls"), list) or not source_b["calls"]:
        raise ValueError("Example JSON mapping does not select records")
    manifest = json.loads((contract.CORPUS / "source-manifest.json").read_text("utf-8"))
    for entry in manifest["files"]:
        if len((contract.CORPUS / entry["path"]).read_bytes()) != entry["byte_length"]:
            raise ValueError("Public corpus byte length differs from its unchanged source manifest")
    return {
        "status": "static_checks_passed",
        "models": [item.name for item in configured],
        "public_corpus_files": [entry["path"] for entry in manifest["files"]],
        "network_calls": 0,
        "processes_started": 0,
        "business_cases_executed": 0,
    }


async def execute(args, evidence):
    from .runtime import Runtime

    runtime = Runtime(evidence)
    selected = STAGES if args.stage == "all" else (args.stage,)
    exit_code = 0
    try:
        evidence.stopped()
        evidence.bind_sources()
        await runtime.start()
        for stage in selected:
            previous = evidence.state["stages"].get(stage, {})
            if previous.get("status") in {"completed", "completed_pending_human_review"}:
                continue
            await runtime.stop_worker()
            evidence.stage = stage
            evidence.state["stages"][stage] = {"status": "running", "started_at": now()}
            evidence.save()
            if stage == "mechanisms":
                from .mechanisms import run
            elif stage == "contract":
                from .contract import run
            else:
                from .recovery import run
            try:
                await run(runtime)
            except BaseException as exc:
                status = "stopped_model_quota_or_auth" if isinstance(exc, PaidValidationStopped) else "failed"
                evidence.state["stages"][stage].update(
                    status=status,
                    failed_at=now(),
                    failure_type=type(exc).__name__,
                    failure_stage=stage,
                    message=str(exc),
                )
                evidence.pending(
                    "unexecuted remainder of stage",
                    "complete the remaining fixed case sequence",
                    {"reason": type(exc).__name__, "message": str(exc)},
                )
                evidence.save()
                raise
            checks = [item for item in evidence.state["checks"].values() if item["stage"] == stage]
            if any(item["status"] == "fail" for item in checks):
                status = "failed"
                exit_code = 1
            elif any(item["status"] == "unverified" for item in checks):
                status = "completed_pending_human_review"
            else:
                status = "completed"
            evidence.state["stages"][stage].update(status=status, finished_at=now())
            evidence.save()
            evidence.report()
            await runtime.stop_worker()
    except (Exception, asyncio.CancelledError) as exc:
        exit_code = 2 if isinstance(exc, PaidValidationStopped) else 1
        evidence.state["failure"] = {
            "stage": evidence.stage,
            "at": now(),
            "type": type(exc).__name__,
            "message": str(exc),
        }
        for stage in selected:
            if stage not in evidence.state["stages"]:
                evidence.state["stages"][stage] = {
                    "status": "not_run",
                    "reason": "earlier stage/setup stopped",
                }
        evidence.save()
    finally:
        try:
            await runtime.close()
        except Exception as exc:
            # Keep the quota/auth stop code while retaining an independent shutdown failure.
            if exit_code != 2:
                exit_code = 1
            evidence.state["shutdown_error"] = {"type": type(exc).__name__, "message": str(exc)}
        evidence.state["exit_code"] = exit_code
        evidence.save()
        summary = evidence.report()
    return exit_code, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=(*STAGES, "all"), default="all")
    parser.add_argument(
        "--run-dir",
        type=Path,
        help="Reuse evals/records/<UUIDv4>; completed stages and paid responses are reused",
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Loopback API port for a new run (default 8765)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only validate local syntax/models/examples; no real case or model call",
    )
    args = parser.parse_args()
    if args.check:
        print(json.dumps(check(), ensure_ascii=False, indent=2))
        return 0
    if sys.platform != "linux" or ROOT != Path("/srv/knowledge-graph"):
        parser.error("Real cases must run in WSL Ubuntu at /srv/knowledge-graph")
    if not 1024 <= args.port <= 65535:
        parser.error("Evaluation port must be between 1024 and 65535")
    import fcntl

    evidence = Evidence(args.run_dir, port=args.port)
    lock = os.open(evidence.directory / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another evaluation runner owns this run directory")
        code, summary = asyncio.run(execute(args, evidence))
    finally:
        os.close(lock)
    print(
        json.dumps(
            {
                "run_id": evidence.state["run_id"],
                "report": str(evidence.directory / "report.md"),
                "stages": summary["stages"],
                "check_counts": summary["check_counts"],
                "data_root": evidence.state["data_root"],
                "exit_code": code,
                "shutdown_error": summary["shutdown_error"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
