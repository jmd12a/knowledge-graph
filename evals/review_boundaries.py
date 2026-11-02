"""Real HTTP, files, SQLite and model-backed mapping migration regressions from final review."""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import yaml

from knowledge_graph.settings import Settings

from .evidence import Evidence, PaidValidationStopped, ROOT, now, write_json
from .runtime import Runtime


async def evaluate(e):
    runtime, code = Runtime(e), 0
    old_tmp = os.environ.get("TMPDIR")
    temporary = e.directory / "multipart-temporary"
    temporary.mkdir()
    os.environ["TMPDIR"] = str(temporary)
    try:
        e.bind_sources()
        settings = Settings.load().model_copy(
            update={
                "data_root": e.state["data_root"],
                "port": e.state["port"],
                "upload_limit": 1024 * 1024,
                "checkpoint_retention_hours": 0,
                "poll_interval": 0.2,
                "retrieval_timeout": 80,
            }
        )
        write_json(e.directory / "settings.json", settings.model_dump(mode="json"))
        await runtime.start()
        api = runtime.api
        e.stage = "mechanisms"
        e.state["stages"][e.stage] = {"status": "running", "started_at": now()}
        model = yaml.safe_load((ROOT / "examples/service-catalog/model.yaml").read_text())
        kb = await runtime.kb("review-boundaries", model)
        target = f"/kbs/{kb}/documents"
        api_pid = runtime.processes.running[runtime.server][0].pid

        def spool_handles():
            result = []
            for fd in Path(f"/proc/{api_pid}/fd").iterdir():
                try:
                    value = os.readlink(fd)
                    if str(temporary) in value:
                        result.append(value)
                except FileNotFoundError:
                    pass
            return result

        too_large = b"a" * (settings.upload_limit + 128 * 1024)
        result = await api.request(
            "POST",
            target,
            expected=413,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("large.txt", too_large)},
        )
        e.check(
            "known length rejected before multipart parsing",
            result["code"] == "UPLOAD_TOO_LARGE",
            "HTTP 413 UPLOAD_TOO_LARGE",
            result,
            evidence=api.last_record,
            required=True,
        )
        handles_during = []

        async def body():
            yield b'--probe\r\nContent-Disposition: form-data; name="file"; filename="large.txt"\r\n\r\n'
            yield b"a" * (settings.upload_limit + 1)
            await asyncio.sleep(0.15)
            handles_during.extend(spool_handles())
            yield b"a" * (128 * 1024)
            yield b"\r\n--probe--\r\n"

        result = await api.request(
            "POST",
            target,
            expected=413,
            content=body(),
            headers={"Content-Type": "multipart/form-data; boundary=probe", "Idempotency-Key": str(uuid4())},
        )
        await api.request("GET", "/health/live")
        handles_after = spool_handles()
        e.check(
            "unknown length multipart closes its spooled file",
            result["code"] == "UPLOAD_TOO_LARGE" and bool(handles_during) and not handles_after,
            "actual chunked HTTP 413; opened spool observed before rejection, no spool remains",
            {"response": result, "during": handles_during, "after": handles_after},
            required=True,
        )
        result = await api.request(
            "POST",
            target,
            expected=413,
            headers={"Idempotency-Key": str(uuid4())},
            files={"file": ("large.txt", b"a" * (settings.upload_limit + 1))},
        )
        docs = await api.request("GET", target)
        e.check(
            "file budget enforced inside allowed envelope",
            result["code"] == "UPLOAD_TOO_LARGE"
            and docs == []
            and not list(Path(e.state["data_root"]).joinpath("uploads").iterdir()),
            "HTTP 413 and no registered or staged documents",
            {"response": result, "documents": docs},
            required=True,
        )

        path = ROOT / "examples/service-catalog/source-a.csv"
        source = {"encoding": "utf-8"}
        uploaded = await api.upload("review.original", kb, path, source=source)
        runtime.start_worker()
        await api.wait_job(uploaded["job_id"])
        await runtime.stop_worker()
        revised = deepcopy(model)
        original_mapping = next(key for key, value in model["mappings"].items() if value["format"] == "csv")
        revised["mappings"]["second_csv_mapping"] = deepcopy(revised["mappings"][original_mapping])
        changed = await api.mutation(
            "review.change-model", "POST", f"/kbs/{kb}/model", body={"model": revised}
        )
        runtime.start_worker()
        await api.wait_job(changed["job_id"])
        await runtime.stop_worker()
        key = e.key("review.original")
        same = await api.request(
            "POST",
            target,
            expected=202,
            headers={"Idempotency-Key": key},
            data={"source": json.dumps(source)},
            files={"file": (path.name, path.read_bytes())},
        )
        e.check(
            "old omitted mapping survives real model rebuild",
            all(same[field] == uploaded[field] for field in ("document_id", "job_id")),
            uploaded,
            same,
            evidence=api.last_record,
            required=True,
        )
        different = await api.request(
            "POST",
            target,
            expected=409,
            headers={"Idempotency-Key": key},
            data={"source": json.dumps(source), "mapping_id": "second_csv_mapping"},
            files={"file": (path.name, path.read_bytes())},
        )
        e.check(
            "explicit mapping change conflicts with old upload",
            different["code"] == "IDEMPOTENCY_CONFLICT",
            "HTTP 409",
            different,
            evidence=api.last_record,
            required=True,
        )
        new = await api.request(
            "POST",
            target,
            expected=422,
            headers={"Idempotency-Key": str(uuid4())},
            data={"source": "{}"},
            files={"file": (path.name, path.read_bytes())},
        )
        e.check(
            "new upload still requires an explicit ambiguous mapping",
            new["code"] == "MAPPING_REQUIRED",
            "new request retains current model validation",
            new,
            evidence=api.last_record,
            required=True,
        )
        long_query = "catalog " * 500
        result = await api.request(
            "POST", f"/kbs/{kb}/knowledge/search", json={"query": long_query, "semantic": False}
        )
        e.check(
            "search accepts the shared retrieve query budget",
            result["status"] == "ok",
            "HTTP 200 for a 4000-character query",
            {"status": result["status"], "query_length": len(long_query)},
            evidence=api.last_record,
            required=True,
        )
        snapshot, record = await runtime.snapshot(kb, "after actual rebuild and upload replays")
        claims = snapshot["contributions"]
        wanted = [item["contribution_id"] for item in claims[:3]]
        selected = await runtime.context.metadata.contributions(kb, contribution_ids=wanted + wanted)
        missing = await runtime.context.metadata.contributions(str(uuid4()), contribution_ids=wanted)
        empty = await runtime.context.metadata.contributions(kb, contribution_ids=[])
        excluded = await runtime.context.metadata.contributions(kb, [], contribution_ids=wanted)
        other_doc = await runtime.context.metadata.contributions(kb, [str(uuid4())], contribution_ids=wanted)
        e.check(
            "contribution ID lookup preserves scope and complete records",
            sorted(selected, key=lambda row: row["contribution_id"])
            == sorted(claims[:3], key=lambda row: row["contribution_id"])
            and not any((missing, empty, excluded, other_doc)),
            "exact selected rows only; duplicate IDs deduplicate and KB/document/empty scopes remain enforced",
            {
                "returned": len(selected),
                "requested": len(wanted),
                "excluded_results": [missing, empty, excluded, other_doc],
            },
            evidence=record,
            required=True,
        )

        entered, release = asyncio.Event(), asyncio.Event()
        json_path = ROOT / "examples/service-catalog/source-b.json"

        class SlowUpload:
            filename = json_path.name

            def __init__(self):
                self.first = True

            async def read(self, size):
                if self.first:
                    self.first = False
                    entered.set()
                    await release.wait()
                    return json_path.read_bytes()
                return b""

        pending = asyncio.create_task(
            runtime.context.management.upload(kb, SlowUpload(), {}, None, str(uuid4()))
        )
        try:
            await entered.wait()
            staged_before = list(Path(e.state["data_root"]).joinpath("uploads").glob("*.tmp"))
            worker = runtime.start_worker("--once", "--no-model")
            await runtime.processes.exited(worker)
            await runtime.stop_worker()
            preserved = all(path.exists() for path in staged_before)
            release.set()
            accepted = await pending
        finally:
            release.set()
            if not pending.done():
                await pending
        document = await runtime.context.metadata.document(kb, accepted["document_id"])
        e.check(
            "zero retention cleanup cannot remove an in-flight upload",
            bool(staged_before)
            and preserved
            and Path(document["raw_path"]).read_bytes() == json_path.read_bytes(),
            "independent Worker cleanup skips active staging; actual registration and original bytes survive",
            {
                "staged_files_observed": len(staged_before),
                "survived_cleanup": preserved,
                "document_id": document["document_id"],
            },
            required=True,
        )
        worker = runtime.start_worker("--once", "--no-model", "--maintenance-scan-limit", "1")
        await runtime.processes.exited(worker)
        await runtime.stop_worker()
        e.check(
            "zero retention cleanup preserves registered originals",
            Path(document["raw_path"]).read_bytes() == json_path.read_bytes(),
            "registered source survives cleanup after upload lock is released",
            document["document_id"],
            required=True,
        )
        e.state["stages"][e.stage].update(status="completed", finished_at=now())
    except BaseException as exc:
        code = (
            2
            if isinstance(exc, PaidValidationStopped)
            or (e.directory / "PAID_VALIDATION_STOPPED.json").exists()
            else 1
        )
        e.state["failure"] = {"type": type(exc).__name__, "message": str(exc), "at": now()}
        e.state["stages"].setdefault(e.stage, {}).update(status="failed")
    finally:
        if old_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmp
        try:
            await runtime.close()
        except BaseException as exc:
            e.state["shutdown_error"] = {"type": type(exc).__name__, "message": str(exc)}
            code = code or 1
        e.state["exit_code"] = code
        e.save()
        report = e.report()
    print(
        json.dumps(
            {"run_id": e.state["run_id"], "exit_code": code, "checks": report["check_counts"]}, indent=2
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(evaluate(Evidence())))
