"""Evaluation-only process boundaries around the actual application and storage adapters.

The wrappers preserve real return values. They only record network calls, wait at an explicitly
selected boundary, or terminate this isolated child after/before an actual adapter operation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from functools import wraps
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from .evidence import now, read_json, write_json

CRASH_EXIT = 86


def hold_paths(directory, token):
    base = directory / "controls"
    return base / (token + ".entered.json"), base / (token + ".release.json")


def wait_release(path, timeout=80):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Evaluation hold was not released")
        time.sleep(0.05)


def quota_failure(response):
    if response.status_code in {401, 402, 403}:
        return "authentication_rejected" if response.status_code in {401, 403} else "quota_exhausted"
    if not response.is_error:
        return None
    try:
        body = response.json()
        error = body.get("error", body)
        text = (str(error.get("code", "")) + " " + str(error.get("message", ""))).lower()
    except (ValueError, AttributeError, TypeError):
        return None
    markers = (
        "insufficient_quota",
        "quotaexhaust",
        "quota_exhaust",
        "arrearage",
        "insufficient_balance",
        "allocationquota",
        "freequota",
        "billing_hard_limit",
        "quota_exceeded",
        "insufficient quota",
        "quota exhausted",
        "quota has been exhausted",
        "insufficient balance",
        "allocated quota",
        "free quota",
        "余额不足",
        "额度不足",
        "欠费",
        "额度已用尽",
    )
    return "quota_exhausted" if any(marker in text for marker in markers) else None


def provider_error(response, url, headers):
    """Keep bounded diagnostic fields after removing request credentials and endpoints."""
    secrets = [
        value
        for name, value in os.environ.items()
        if value and re.search(r"key|token|secret|password|credential|auth", name, re.I)
    ]
    for name, value in headers.items():
        if value and re.search(r"key|token|secret|password|credential|auth|cookie", name, re.I):
            secrets.append(str(value))
            if str(value).lower().startswith(("bearer ", "basic ")):
                secrets.append(str(value).split(None, 1)[-1])
    endpoint = urlsplit(str(url))
    secrets.extend(value for value in (str(url), endpoint.hostname) if value)
    # Redact before truncation so a credential crossing the length limit is not exposed.
    secrets = sorted(secrets + [quote(value, safe="") for value in secrets], key=len, reverse=True)

    def safe(value, limit):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return None
        text = str(value)
        for secret in secrets:
            text = text.replace(secret, "<REDACTED>")
        text = re.sub(r"https?://[^\s<>\"']+", "<REDACTED_URL>", text, flags=re.I)
        text = re.sub(r"\b(?:Bearer|Basic)\s+[^\s,;\"']+", "<REDACTED_AUTH>", text, flags=re.I)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<REDACTED_KEY>", text)
        text = re.sub(
            r"(?i)(?:authorization|api[_ -]?key|password|secret|access[_ -]?token)"
            r"[\"']?\s*[:=]\s*[\"']?[^\n,;\"'}]+",
            "<REDACTED_CREDENTIAL>",
            text,
        )
        return " ".join(text.split())[:limit]

    result = {"byte_length": len(response.content)}
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    result["content_type"] = safe(content_type, 100)
    try:
        body = response.json()
    except ValueError:
        result["payload_kind"] = "non_json"
        if content_type != "text/event-stream":
            result["body_preview"] = safe(response.text, 700)
        else:
            # Never copy reasoning deltas into transport diagnostics.
            result["stream_done_marker"] = "data: [DONE]" in response.text
        return result
    if not isinstance(body, dict):
        result["payload_kind"] = "non_object_json"
        return result
    result["payload_kind"] = "json_object"
    error = body.get("error", body)
    if not isinstance(error, dict):
        return result
    for field in ("code", "type", "param", "request_id"):
        value = safe(error.get(field, body.get(field)), 160)
        if value is not None:
            result[field] = value
    message = safe(error.get("message"), 1000)
    if message is not None:
        result["message"] = message
    return result


def instrument_language(context, args, directory):
    construction = getattr(context, "construction_language", context.language)
    if construction is context.language:
        instrument_language_client(context.language, args, directory, "shared")
    else:
        instrument_language_client(context.language, args, directory, "retrieval")
        instrument_language_client(construction, args, directory, "construction")


def instrument_language_client(language, args, directory, language_stage):
    original = language.client.post
    stopped = directory / "PAID_VALIDATION_STOPPED.json"

    @wraps(original)
    async def post(url, **kwargs):
        from knowledge_graph.domain import KGError

        if stopped.exists():
            raise KGError("MODEL_VALIDATION_STOPPED", "Further paid validation is stopped", 503)
        if args.no_model:
            write_json(
                directory / "processes" / args.token / ("forbidden-model-" + str(uuid4()) + ".json"),
                {
                    "at": now(),
                    "endpoint": url,
                    "model": kwargs.get("json", {}).get("model"),
                    "language_stage": language_stage,
                },
            )
            raise KGError("UNEXPECTED_MODEL_CALL", "Prepared-plan replay attempted a new model call", 503)
        call_id, started = str(uuid4()), time.monotonic()
        body = kwargs.get("json", {})
        path = directory / "model_calls" / (call_id + ".json")
        record = {
            "call_id": call_id,
            "started_at": now(),
            "role": args.role,
            "process_token": args.token,
            "language_stage": language_stage,
            "endpoint": str(url),
            "model": body.get("model"),
            "request": body,
            "input_items": len(body.get("input", [])),
            "outcome": "started",
        }
        write_json(path, record)
        try:
            response = await original(url, **kwargs)
            reason = quota_failure(response)
            record.update(
                status_code=response.status_code, outcome="error" if response.is_error else "completed"
            )
            if reason:
                write_json(
                    stopped,
                    {
                        "at": now(),
                        "reason": reason,
                        "call_id": call_id,
                        "status_code": response.status_code,
                        "endpoint": str(url),
                        "model": body.get("model"),
                        "language_stage": language_stage,
                    },
                )
            if not response.is_error:
                try:
                    from knowledge_graph.adapters.models.http import decode_response

                    record["response"] = decode_response(response)
                    if isinstance(record["response"], dict):
                        record["usage"] = record["response"].get("usage")
                except (ValueError, TypeError, AttributeError) as exc:
                    record["decode_error_type"] = type(exc).__name__
                    record["response"] = {"byte_length": len(response.content), "invalid_json": True}
                    record["error"] = {"provider": provider_error(response, url, kwargs.get("headers", {}))}
                    record["outcome"] = "error"
            else:
                record["error"] = {
                    "status_code": response.status_code,
                    "stop_reason": reason,
                    "provider": provider_error(response, url, kwargs.get("headers", {})),
                }
            return response
        except BaseException as exc:
            record.update(outcome="error", error={"exception_type": type(exc).__name__})
            raise
        finally:
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            record["finished_at"] = now()
            write_json(path, record)

    language.client.post = post


def instrument_worker(context, args, directory):
    from knowledge_graph.runtime import blocking

    async def boundary(step, when, kb_id):
        if step != args.crash_step or when != args.crash_when:
            return
        job = await context.metadata.job(args.job_id)
        if kb_id != job["kb_id"]:
            return
        plan = await blocking(context.files.read_plan, job)
        checkpoint = {
            "at": now(),
            "step": step,
            "when": when,
            "job": job,
            "plan_id": plan["plan_id"],
            "kb": await context.metadata.kb(kb_id),
            "id_sets": context.files._object_ids(plan),
        }
        write_json(directory / "processes" / args.token / "checkpoint.json", checkpoint)
        os._exit(CRASH_EXIT)

    original_upsert = context.vectors.upsert

    @wraps(original_upsert)
    async def upsert(kb_id, kind, points):
        if kind in {"chunks", "objects"}:
            await boundary(kind, "before", kb_id)
        result = await original_upsert(kb_id, kind, points)
        if kind in {"chunks", "objects"}:
            await boundary(kind, "after", kb_id)
        return result

    context.vectors.upsert = upsert
    for attribute, step in (("replace_contributions", "contributions"), ("finish", "finalcommit")):
        original = getattr(context.metadata, attribute)

        def wrap_metadata(original, step):
            @wraps(original)
            async def operation(*values, **kwargs):
                kb_id = (
                    (await context.metadata.job(values[0]))["kb_id"] if step == "finalcommit" else values[0]
                )
                await boundary(step, "before", kb_id)
                result = await original(*values, **kwargs)
                await boundary(step, "after", kb_id)
                return result

            return operation

        setattr(context.metadata, attribute, wrap_metadata(original, step))
    original_graph = context.graph.apply

    @wraps(original_graph)
    async def graph(kb_id, *values, **kwargs):
        await boundary("graph", "before", kb_id)
        result = await original_graph(kb_id, *values, **kwargs)
        await boundary("graph", "after", kb_id)
        return result

    context.graph.apply = graph
    original_apply = context.construction.apply

    @wraps(original_apply)
    async def apply(job):
        if args.hold_prepared and job["job_id"] == args.job_id:
            entered, release = hold_paths(directory, args.hold_prepared)
            current = await context.metadata.job(job["job_id"])
            if current["state"] != "PREPARED":
                raise RuntimeError("Evaluation prepared hold requires a real PREPARED task")
            write_json(
                entered,
                {
                    "at": now(),
                    "job": await context.metadata.job(job["job_id"]),
                    "kb": await context.metadata.kb(job["kb_id"]),
                },
            )
            await blocking(wait_release, release)
        return await original_apply(job)

    context.construction.apply = apply


def instrument_reads(context, directory):
    from knowledge_graph.runtime import blocking

    original = context.graph.get

    async def cancel_when_requested(task, token):
        path = directory / "controls" / (token + ".cancel.json")
        while not path.exists():
            await asyncio.sleep(0.02)
        write_json(
            path.with_name(token + ".cancel-observed.json"),
            {"at": now(), "kind": "actual_in_flight_request_task_cancelled_by_eval_control"},
        )
        task.cancel()

    @wraps(original)
    async def get(kb_id, kind, object_id):
        result = await original(kb_id, kind, object_id)
        control = directory / "controls" / "read-hold.json"
        if control.exists():
            selection = read_json(control)
            entered, release = hold_paths(directory, selection["token"])
            if selection["kb_id"] == kb_id and selection["object_id"] == object_id and not entered.exists():
                write_json(
                    entered,
                    {
                        "at": now(),
                        "kb_id": kb_id,
                        "object_id": object_id,
                        "real_storage_read_completed": True,
                    },
                )
                cancellation = (
                    asyncio.create_task(cancel_when_requested(asyncio.current_task(), selection["token"]))
                    if selection.get("cancel_request_task")
                    else None
                )
                try:
                    # The actual query is inside gate.read. Cancellation must await this real blocking operation.
                    await blocking(wait_release, release)
                finally:
                    if cancellation is not None:
                        cancellation.cancel()
                        with suppress(asyncio.CancelledError):
                            await cancellation
                    write_json(entered.with_name(selection["token"] + ".exited.json"), {"at": now()})
        return result

    context.graph.get = get


async def worker(context, args, directory):
    from knowledge_graph.worker import Worker

    instrument_worker(context, args, directory)
    try:
        await Worker(context.resources, context.construction).run(once=args.once)
    finally:
        await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("api", "worker"))
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--job-id")
    parser.add_argument(
        "--crash-step", choices=("chunks", "contributions", "graph", "objects", "finalcommit")
    )
    parser.add_argument("--crash-when", choices=("before", "after"), default="after")
    parser.add_argument("--hold-prepared")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--maintenance-scan-limit", type=int)
    args = parser.parse_args()
    if (args.crash_step or args.hold_prepared) and not args.job_id:
        parser.error("A boundary control requires --job-id")
    if args.maintenance_scan_limit is not None and args.maintenance_scan_limit < 1:
        parser.error("The maintenance scan limit must be positive")
    directory = args.run_dir.resolve()
    from knowledge_graph.bootstrap import Context
    from knowledge_graph.settings import Settings

    Settings.load(str(directory / "settings.json"))
    settings = Settings.model_validate(read_json(directory / "settings.json"))
    if args.maintenance_scan_limit is not None:
        settings = settings.model_copy(update={"maintenance_scan_limit": args.maintenance_scan_limit})
    write_json(
        directory / "processes" / args.token / "effective-settings.json", settings.model_dump(mode="json")
    )
    context = Context(settings)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    instrument_language(context, args, directory)
    if args.role == "worker":
        asyncio.run(worker(context, args, directory))
    else:
        import uvicorn

        from knowledge_graph.server import create_app

        instrument_reads(context, directory)
        app = create_app(context)
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            try:
                async with original_lifespan(app):
                    yield
            finally:
                await context.close()

        app.router.lifespan_context = lifespan
        uvicorn.run(app, host=settings.host, port=settings.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
