from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .evidence import ROOT, CaseFailure, now, read_json, without_request_ids, write_json


def assert_api_port_available(port):
    # Match Uvicorn's restart semantics: TIME_WAIT connections must not look like a live listener.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise CaseFailure("The evaluation port cannot be bound; no existing process was stopped") from exc


class Processes:
    """Only controls children whose unique command-line token belongs to this run."""

    def __init__(self, evidence):
        self.e = evidence
        self.running = {}
        self.registry = evidence.directory / "processes" / "registry.json"

    @staticmethod
    def owned(pid, directory, token):
        try:
            arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            return False
        return (
            b"evals.process" in arguments
            and str(directory).encode() in arguments
            and token.encode() in arguments
        )

    async def reap_previous(self):
        if not self.registry.exists():
            return
        for entry in read_json(self.registry):
            if self.owned(entry["pid"], self.e.directory, entry["token"]):
                os.kill(entry["pid"], signal.SIGTERM)
                deadline = time.monotonic() + 5
                while (
                    self.owned(entry["pid"], self.e.directory, entry["token"]) and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.1)
                if self.owned(entry["pid"], self.e.directory, entry["token"]):
                    os.kill(entry["pid"], signal.SIGKILL)
                self.e.record("processes", {"event": "reaped_owned_previous_child", **entry})
        write_json(self.registry, [])

    def launch(self, role, *options):
        self.e.stopped()
        token = str(uuid4())
        directory = self.e.directory / "processes" / token
        directory.mkdir(parents=True)
        log = (directory / "output.log").open("ab", buffering=0)
        args = [
            sys.executable,
            "-m",
            "evals.process",
            role,
            "--run-dir",
            str(self.e.directory),
            "--token",
            token,
            *options,
        ]
        environment = os.environ.copy()
        from knowledge_graph.settings import Settings

        # Persisted run configuration wins over unrelated application overrides; credentials stay in env.
        for field in Settings.model_fields:
            environment.pop("KG_" + field.upper(), None)
        environment["KG_CONFIG"] = str(self.e.directory / "settings.json")
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen(
                args, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        finally:
            log.close()
        entry = {"pid": process.pid, "token": token, "role": role, "started_at": time.time(), "command": args}
        self.running[token] = (process, entry)
        self._save_registry()
        self.e.record("processes", {"event": "started", **entry})
        return token

    def _save_registry(self):
        write_json(
            self.registry, [entry for process, entry in self.running.values() if process.poll() is None]
        )

    async def exited(self, token, timeout=240):
        process, entry = self.running[token]
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            self.e.stopped()
            if time.monotonic() >= deadline:
                raise CaseFailure(f"Timed out waiting for child {token}")
            await asyncio.sleep(0.1)
        self.e.record("processes", {"event": "exited", "exit_code": process.returncode, **entry})
        self._save_registry()
        return process.returncode

    async def stop(self, token):
        if token is None:
            return
        process, entry = self.running[token]
        if process.poll() is None:
            if not self.owned(process.pid, self.e.directory, token):
                raise CaseFailure("Refusing to signal a process whose run token cannot be verified")
            process.terminate()
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait)
            self.e.record(
                "processes", {"event": "stopped_owned_child", "exit_code": process.returncode, **entry}
            )
        self._save_registry()

    async def stop_all(self):
        for token in list(self.running):
            await self.stop(token)


class API:
    def __init__(self, evidence):
        self.e = evidence
        self.base = f"http://127.0.0.1:{evidence.state['port']}"
        self.client = httpx.AsyncClient(base_url=self.base + "/api/v1", timeout=180)
        self.last_record = None
        self.last_response_reused = False

    def _reuse(self, action, request, input_bytes=None):
        entry = self.e.state["actions"].get(action, {})
        if "result" not in entry:
            return None
        if (
            entry.get("request") != request
            or not entry.get("response_record")
            or not entry.get("executed_at")
        ):
            raise CaseFailure("Saved action inputs/provenance differ; use a new run: " + action)
        if input_bytes is not None:
            original = entry.get("input_bytes_record")
            if not original or (self.e.directory / original).read_bytes() != input_bytes:
                raise CaseFailure("Uploaded source bytes changed; use a new run: " + action)
        self.last_record = entry["response_record"]
        self.last_response_reused = True
        reuse = self.e.record(
            "reuses",
            {
                "action": action,
                "reused": True,
                "request": request,
                "original_response_record": self.last_record,
                "original_executed_at": entry["executed_at"],
                "meaning": "Previously observed response; not a newly executed verification",
            },
        )
        entry.setdefault("reuse_records", []).append(reuse)
        self.e.save()
        return entry["result"]

    def _remember(self, action, request, result, input_bytes=None):
        entry = self.e.state["actions"].setdefault(action, {})
        entry.update(request=request, response_record=self.last_record, executed_at=now(), result=result)
        if input_bytes is not None:
            path = self.e.directory / "inputs" / (str(uuid4()) + ".original")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(input_bytes)
            entry["input_bytes_record"] = str(path.relative_to(self.e.directory))
        self.e.save()
        return result

    async def close(self):
        await self.client.aclose()

    async def request(self, method, path, *, expected=200, **kwargs):
        self.e.stopped()
        started = time.monotonic()
        response = await self.client.request(method, path, **kwargs)
        try:
            value = response.json()
        except ValueError:
            value = {"byte_length": len(response.content), "text": response.text}
        self.last_record = self.e.record(
            "responses",
            {
                "protocol": "HTTP",
                "method": method,
                "path": path,
                "input": kwargs.get("json", kwargs.get("data")),
                "params": kwargs.get("params"),
                "status_code": response.status_code,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "response": value,
            },
        )
        self.last_response_reused = False
        self.e.stopped()
        acceptable = {expected} if isinstance(expected, int) else set(expected)
        if response.status_code not in acceptable:
            self.e.check(
                "HTTP " + method + " " + path,
                False,
                sorted(acceptable),
                response.status_code,
                evidence=self.last_record,
            )
            raise CaseFailure(
                f"HTTP {method} {path}: expected {sorted(acceptable)}, got {response.status_code}"
            )
        return value

    async def upload(self, action, kb_id, path, mapping_id=None, *, replay=False, source=None):
        path = Path(path)
        source = source or {
            "dataset": "artificial-service-mechanisms",
            "file": path.name,
            "encoding": "utf-8",
        }
        content = path.read_bytes()
        request = {
            "method": "POST",
            "path": f"/kbs/{kb_id}/documents",
            "original_name": path.name,
            "mapping_id": mapping_id,
            "source": source,
            "byte_length": len(content),
        }
        if not replay:
            reused = self._reuse(action, request, content)
            if reused is not None:
                return reused
        data = {"source": json.dumps(source, ensure_ascii=False)}
        if mapping_id:
            data["mapping_id"] = mapping_id
        result = await self.request(
            "POST",
            f"/kbs/{kb_id}/documents",
            expected=202,
            headers={"Idempotency-Key": self.e.key(action)},
            data=data,
            files={"file": (path.name, content, "application/octet-stream")},
        )
        return result if replay else self._remember(action, request, result, content)

    async def mutation(self, action, method, path, *, body=None):
        request = {"method": method, "path": path, "body": body}
        reused = self._reuse(action, request)
        if reused is not None:
            return reused
        kwargs = {"headers": {"Idempotency-Key": self.e.key(action)}}
        if body is not None:
            kwargs["json"] = body
        result = await self.request(method, path, expected=202, **kwargs)
        return self._remember(action, request, result)

    async def retry_job(self, action, job_id):
        prefix = f"/jobs/{job_id}/retry?generation="
        saved_path = self.e.state["actions"].get(action, {}).get("request", {}).get("path", "")
        if saved_path.startswith(prefix):
            return await self.mutation(action, "POST", saved_path)
        job = await self.request("GET", f"/jobs/{job_id}")
        return await self.mutation(action, "POST", prefix + str(job["retry_generation"]))

    async def paid_request(self, action, path, body):
        request = {"method": "POST", "path": path, "body": body}
        reused = self._reuse(action, request)
        if reused is not None:
            return reused
        result = await self.request("POST", path, json=body)
        return self._remember(action, request, result)

    async def mcp(self, name, arguments):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self.e.stopped()
        started = time.monotonic()
        async with streamablehttp_client(self.base + "/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments=arguments)
        raw = result.model_dump(mode="json", by_alias=True)
        value = result.structuredContent
        if value is None:
            text = next((block.text for block in result.content if block.type == "text"), None)
            value = json.loads(text) if text else None
        self.last_record = self.e.record(
            "responses",
            {
                "protocol": "MCP",
                "tool": name,
                "input": arguments,
                "response": raw,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
        self.last_response_reused = False
        self.e.stopped()
        if not isinstance(value, dict):
            raise CaseFailure("MCP did not return a structured JSON object")
        return value

    async def parity(self, name, kb_id, http_value, **arguments):
        mcp_value = await self.mcp(name, {"kb_id": kb_id, **arguments})
        self.e.check(
            "HTTP/MCP " + name,
            without_request_ids(http_value) == without_request_ids(mcp_value),
            without_request_ids(http_value),
            without_request_ids(mcp_value),
            evidence=self.last_record,
        )
        return mcp_value

    async def job(self, job_id):
        return await self.request("GET", "/jobs/" + job_id)

    async def wait_job(self, job_id, timeout=420, *, allow_failure=False):
        started, last, transitions = time.monotonic(), None, []
        while time.monotonic() - started < timeout:
            self.e.stopped()
            value = await self.job(job_id)
            state = (value["state"], value["attempt"], value.get("last_step"))
            if state != last:
                transitions.append({"elapsed_seconds": time.monotonic() - started, "job": value})
                last = state
            if value["state"] in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
                self.e.record(
                    "jobs",
                    {
                        "job_id": job_id,
                        "elapsed_seconds": time.monotonic() - started,
                        "transitions": transitions,
                    },
                )
                return value
            if value["state"] in {"PREPARE_FAILED", "CLEANUP_FAILED", "INVALIDATED"} or (
                value["attempt"] >= 3 and value["state"] in {"NEEDS_REPAIR", "PREPARED"}
            ):
                if allow_failure:
                    return value
                self.e.record("jobs", {"job_id": job_id, "transitions": transitions, "failure": value})
                raise CaseFailure("Job stopped: " + job_id + " " + value["state"])
            await asyncio.sleep(0.3)
        self.e.record("jobs", {"job_id": job_id, "transitions": transitions, "failure": "timeout"})
        raise CaseFailure("Job wait timed out: " + job_id)


class Runtime:
    def __init__(self, evidence):
        self.e, self.processes = evidence, Processes(evidence)
        self.api, self.context, self.server, self.worker = API(evidence), None, None, None

    async def start(self):
        from knowledge_graph.bootstrap import Context
        from knowledge_graph.settings import Settings

        await self.processes.reap_previous()
        settings_path = self.e.directory / "settings.json"
        if not settings_path.exists():
            settings = Settings.load()
            embedding = settings.embedding.model_copy(update={"revision_id": str(uuid4())})
            settings = settings.model_copy(
                update={
                    "data_root": self.e.state["data_root"],
                    "host": "127.0.0.1",
                    "port": self.e.state["port"],
                    "embedding": embedding,
                    "drain_timeout": 90,
                    "poll_interval": 0.2,
                    "retrieval_timeout": max(settings.retrieval_timeout, 80),
                    "checkpoint_retention_hours": 168,
                }
            )
            write_json(settings_path, settings.model_dump(mode="json"))
        # Load credential variables without storing their values in the evaluation settings.
        Settings.load(str(settings_path))
        settings = Settings.model_validate(read_json(settings_path))
        self.e.state["configuration"] = settings.model_dump(mode="json")
        self.e.save()
        missing = [
            name
            for name in {
                settings.llm.api_key_env,
                settings.embedding.api_key_env,
                settings.neo4j_password_env,
            }
            if not os.getenv(name)
        ]
        if missing:
            raise CaseFailure(
                "Required credential environment variables are missing: " + ", ".join(sorted(missing))
            )
        self.context = Context(settings)
        await self.context.initialize()
        self.e.record("setup", {"dependencies": await self.context.ready()})
        await self.start_api()

    async def start_api(self):
        assert_api_port_available(self.e.state["port"])
        self.server = self.processes.launch("api")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            process = self.processes.running[self.server][0]
            if process.poll() is not None:
                raise CaseFailure("Independent API child exited during startup")
            try:
                response = await self.api.client.get("/health/live", timeout=1)
                if response.status_code == 200:
                    await self.api.request("GET", "/health/ready")
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.1)
        raise CaseFailure("Independent API did not become ready")

    async def restart_api(self):
        await self.processes.stop(self.server)
        await self.start_api()

    def start_worker(self, *options):
        if self.worker is not None and self.processes.running[self.worker][0].poll() is None:
            raise CaseFailure("An evaluation worker is already running")
        self.worker = self.processes.launch("worker", *options)
        return self.worker

    async def stop_worker(self):
        await self.processes.stop(self.worker)
        self.worker = None

    async def close(self):
        # Release any deliberate evaluation wait before asking children to shut down.
        controls = self.e.directory / "controls"
        for path in controls.glob("*.entered.json"):
            write_json(
                path.with_name(path.name.removesuffix(".entered.json") + ".release.json"), {"release": True}
            )
        selection = controls / "read-hold.json"
        if selection.exists():
            write_json(controls / (read_json(selection)["token"] + ".release.json"), {"release": True})
        await self.processes.stop_all()
        await self.api.close()
        if self.context is not None:
            await self.context.close()

    async def kb(self, label, model):
        if label in self.e.state["kbs"]:
            return self.e.state["kbs"][label]
        name = "eval-" + self.e.state["run_id"] + "-" + label
        existing = await self.api.request("GET", "/kbs", params={"limit": 1000})
        item = next((value for value in existing if value["name"] == name), None)
        if item is None:
            item = await self.api.request("POST", "/kbs", expected=201, json={"name": name, "model": model})
        kb_id = item["kb_id"]
        self.e.state["kbs"][label] = kb_id
        self.e.state.setdefault("collections", {})[kb_id] = {
            kind: self.context.vectors.collection(kb_id, kind) for kind in ("objects", "chunks")
        }
        self.e.save()
        return kb_id

    async def snapshot(self, kb_id, label, *, with_vectors=False):
        c = self.context
        entities, relations = await c.graph.all(kb_id)
        value = {
            "label": label,
            "kb": await c.metadata.kb(kb_id),
            "entities": entities,
            "relations": relations,
            "contributions": await c.metadata.contributions(kb_id),
            "documents": await c.metadata.list_documents(kb_id, None, 1000, 0),
            "chunks": await c.vectors.all(kb_id, "chunks", with_vectors=with_vectors, max_points=10000),
            "objects": await c.vectors.all(kb_id, "objects", with_vectors=with_vectors, max_points=10000),
        }
        record = self.e.record("snapshots", {"diagnostic_direct_storage_read": True, **value})
        return value, record

    async def wait_file(self, path, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.e.stopped()
            if path.exists():
                return read_json(path)
            await asyncio.sleep(0.1)
        raise CaseFailure("Evaluation control point was not reached: " + path.name)

    async def wait_state(self, job_id, state, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await self.api.job(job_id)
            if value["state"] == state:
                return value
            if value["state"] in {"PREPARE_FAILED", "SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
                raise CaseFailure(f"Job reached {value['state']} before {state}")
            await asyncio.sleep(0.1)
        raise CaseFailure("Job did not reach " + state)

    def model_calls(self):
        return sorted(
            str(path.relative_to(self.e.directory))
            for path in (self.e.directory / "model_calls").glob("*.json")
        )
