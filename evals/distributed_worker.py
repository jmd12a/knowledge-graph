"""Run the distributed Worker fault case against an isolated, configured test deployment."""

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

import httpx
import yaml

from knowledge_graph.adapters.redis_stream import PreparationQueue
from knowledge_graph.bootstrap import Context
from knowledge_graph.domain import EmbeddingProfile, KGError, new_id
from knowledge_graph.server import create_app
from knowledge_graph.services.construction.preparation_worker import PreparationDispatcher, PreparationWorker
from knowledge_graph.settings import Settings
from knowledge_graph.worker import Worker

CHILD = """
import asyncio, os, sys
from pathlib import Path
from knowledge_graph.adapters.redis_stream import PreparationQueue
from knowledge_graph.bootstrap import Context
from knowledge_graph.runtime import blocking
from knowledge_graph.services.construction.preparation_worker import PreparationWorker
from knowledge_graph.settings import Settings
async def main():
    settings = Settings.load().model_copy(update={"construction_llm":None})
    context = Context(settings)
    queue = PreparationQueue(settings.worker_distribution)
    async def hold(*args):
        Path(os.environ["KG_WORKER_CHECK_SIGNAL"]).touch()
        await asyncio.Event().wait()
    context.language.json=hold
    try:
        await blocking(context.files.initialize)
        await PreparationWorker(context.construction_resources,context.construction,queue).run()
    finally:
        await queue.close()
        await context.close()
asyncio.run(main())
"""


class Upload(BytesIO):
    filename = "guide.txt"

    async def read(self, size):
        return super().read(size)


def mock_model(context):
    async def embed(texts, profile):
        return [[1.0] + [0.0] * (profile["dimension"] - 1) for _ in texts]

    async def json(*args, **kwargs):
        return {"entities": [], "relations": []}

    context.language.embed, context.language.json = embed, json


async def wait_for(condition, seconds=20):
    for _ in range(seconds * 10):
        if condition():
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("Process did not reach its checkpoint")


async def main_case(base, root, minio):
    settings = base.model_copy(
        update={
            "data_root": str(root / "publish"),
            "worker_distribution": minio,
            "construction_llm": None,
            "chunk_decision_model": "",
            "embedding": EmbeddingProfile(dimension=8),
            "rerank_enabled": False,
            "poll_interval": 0.1,
        }
    )
    api = Context(settings)
    remote = Context(settings.model_copy(update={"data_root": str(root / "remote-b")}))
    queue, b = PreparationQueue(minio), PreparationQueue(minio)
    dispatcher = PreparationDispatcher(api.resources, queue)
    child = None
    try:
        await api.initialize()
        remote.files.initialize()
        mock_model(api)
        mock_model(remote)
        model = yaml.safe_load(Path("examples/service-catalog/model.yaml").read_text())
        kb1 = (await api.management.create_kb("distributed-A", model))["kb_id"]
        kb2 = (await api.management.create_kb("distributed-B", model))["kb_id"]
        data = b"The payments service is owned by the backend team."
        original = await api.management.upload(kb1, Upload(data), {}, None, "source-a")
        retry = await api.management.upload(kb1, Upload(data), {}, None, "source-a")
        assert retry["job_id"] == original["job_id"]
        await dispatcher.dispatch()

        signal_path = root / "prepared-draft"
        child_env = os.environ | {
            "KG_POSTGRES_DSN_ENV": settings.postgres_dsn_env,
            "KG_DATA_ROOT": str(root / "remote-a"),
            "KG_WORKER_DISTRIBUTION": minio.model_dump_json(),
            "KG_WORKER_CHECK_SIGNAL": str(signal_path),
        }
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD], env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await wait_for(signal_path.exists)
        old = await api.metadata.job(original["job_id"])
        assert old["attempt"] == 1 and old["state"] == "PREPARING"
        old_dir = api.files.attempt_dir(old)
        original_etag = api.files._head(old_dir / "preparation.json")["ETag"]
        assert api.files.read_json(old_dir / "preparation.json")["documents"]
        blocked = await api.management.upload(
            kb1, Upload(b"A second source in this knowledge base."), {}, None, "source-2"
        )
        other = await api.management.upload(
            kb2, Upload(b"The gateway belongs to the platform team."), {}, None, "source-b"
        )
        await dispatcher.dispatch()
        prep_b = PreparationWorker(remote.construction_resources, remote.construction, b)
        await prep_b.run(once=True)
        assert (await api.metadata.job(other["job_id"]))["state"] == "PREPARED"
        assert (await api.metadata.job(blocked["job_id"]))["state"] == "QUEUED"

        child.kill()
        await asyncio.to_thread(child.wait)
        child = None
        await asyncio.sleep(minio.claim_idle_seconds + 0.2)
        parse = remote.parser.parse

        def no_parse(*args):
            raise AssertionError("The saved draft should supply the parsed source")

        remote.parser.parse = no_parse
        await prep_b.run(once=True)
        remote.parser.parse = parse
        prepared = await api.metadata.job(original["job_id"])
        assert prepared["attempt"] == 2 and prepared["state"] == "PREPARED", prepared["error"]
        assert api.files.read_plan(prepared)["plan_id"] == prepared["plan_id"]
        assert api.files.exists(old_dir / "preparation.json")
        print(
            "MinIO checkpoints resumed after terminating a separate Worker process; different KBs prepared concurrently",
            flush=True,
        )

        publish = Worker(api.construction_resources, api.construction)
        upsert = api.vectors.upsert

        async def interrupted(kb_id, kind, points):
            if kb_id == kb1 and kind == "objects":
                raise asyncio.CancelledError()
            return await upsert(kb_id, kind, points)

        api.vectors.upsert = interrupted
        try:
            await publish.run(once=True, dispatcher=dispatcher)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("The simulated interrupted publication did not fire")
        finally:
            api.vectors.upsert = upsert
        partial = await api.metadata.job(original["job_id"])
        assert partial["state"] == "APPLYING"
        assert partial["plan_id"] == prepared["plan_id"]
        publish = Worker(api.construction_resources, api.construction)
        await publish.run(once=True, dispatcher=dispatcher)
        finished = await api.metadata.job(original["job_id"])
        assert finished["state"].startswith("SUCCEEDED") and finished["plan_id"] == prepared["plan_id"], (
            finished["error"]
        )
        print("Partial official write replayed the same sealed MinIO plan", flush=True)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(api)), base_url="http://local"
        ) as client:
            response = await client.get(f"/api/v1/kbs/{kb1}/documents/{original['document_id']}/content")
            assert response.status_code == 200 and response.content == data, response.status_code

        await dispatcher.dispatch()
        await prep_b.run(once=True)
        await publish.run(once=True, dispatcher=dispatcher)
        await publish.run(once=True, dispatcher=dispatcher)
        assert (await api.metadata.job(blocked["job_id"]))["state"].startswith("SUCCEEDED")
        assert (await api.metadata.job(other["job_id"]))["state"].startswith("SUCCEEDED")

        # An earlier full operation may have registered only its attempt marker.
        abandoned_id = await api.metadata.enqueue(kb1, "REBUILD", None, {"model": model}, "abandoned", {})
        abandoned = await api.metadata.claim_preparation(await api.metadata.job(abandoned_id), time.time())
        api.files.begin_preparation(abandoned)
        assert await api.metadata.update_preparation(abandoned, state="PREPARE_FAILED")
        abandoned_dir = api.files.attempt_dir(abandoned)

        remove = await api.management.delete_document(kb1, original["document_id"], "delete-source-a")
        await dispatcher.dispatch()
        await prep_b.run(once=True)
        await publish.run(once=True, dispatcher=dispatcher)
        deleted = await api.metadata.job(remove["job_id"])
        assert deleted["state"].startswith("SUCCEEDED"), deleted["error"]
        assert not api.files.exists(api.files.raw_path(kb1, original["document_id"], ".txt"))
        assert api.files.read_json(old_dir / "scope.json")
        for path, condition in (
            (old_dir / "preparation.json", {"IfMatch": original_etag}),
            (old_dir / "prepared.json", {"IfNoneMatch": "*"}),
            (abandoned_dir / "preparation.json", {"IfNoneMatch": "*"}),
        ):
            try:
                api.files._request("put_object", Key=str(path), Body=b"late old source", **condition)
            except KGError as exc:
                assert exc.code == "OBJECT_WRITE_CONFLICT", exc.code
            else:
                raise AssertionError("MinIO accepted the retired PUT")
        for kb, item in ((kb1, blocked), (kb2, other)):
            job = await api.management.delete_document(kb, item["document_id"], "delete-other")
            await dispatcher.dispatch()
            await prep_b.run(once=True)
            await publish.run(once=True, dispatcher=dispatcher)
            assert (await api.metadata.job(job["job_id"]))["state"].startswith("SUCCEEDED")
        for kb in (kb1, kb2):
            for kind in ("chunks", "chunk_vectors", "objects"):
                await api.vectors.client.delete_collection(api.vectors.collection(kb, kind))
        print("HTTP download, upload idempotency, deletion and stale PUT rejection verified", flush=True)
    finally:
        if child is not None:
            child.kill()
            child.wait()
        await queue.client.delete(*queue.keys)
        await queue.close()
        await b.close()
        await remote.close()
        await api.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Settings for disposable test stores")
    args = parser.parse_args()
    settings = Settings.load(str(args.config))
    if settings.worker_distribution is None:
        parser.error("worker_distribution is required")
    distribution = settings.worker_distribution.model_copy(
        update={
            "stream": "test:{" + new_id() + "}:prepare",
            "heartbeat_seconds": 0.2,
            "claim_idle_seconds": 0.85,
        }
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="kg-worker-check-") as temp:
        asyncio.run(main_case(settings, Path(temp), distribution))


if __name__ == "__main__":
    main()
