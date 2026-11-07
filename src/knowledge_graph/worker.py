"""在单 Worker 锁下恢复并串行执行维护任务，定期清理过期中间文件。"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .bootstrap import Context
from .domain import KGError, SUCCESS_STATES, WRITE_STATES, canonical
from .ports.resources import Resources
from .runtime import blocking
from .services.construction.preparation_worker import PreparationDispatcher, PreparationWorker, execution_error

log = logging.getLogger(__name__)


class ConstructionService(Protocol):
    async def prepare(self, job: dict) -> dict: ...
    async def apply(self, job: dict) -> None: ...


class Worker:
    def __init__(self, resources: Resources, construction: ConstructionService):
        self.resources = resources
        self.construction = construction
        self.last_cleanup = 0.0
        self.artifact_cursor = None

    async def execute(self, job):
        """执行准备与写入，按持久化状态区分准备重试和写入修复。"""
        c = self.resources
        try:
            if job["state"] == "PREPARING":
                await self.construction.prepare(job)
                job = await c.metadata.job(job["job_id"])

            await self.construction.apply(job)
            log.info("job_complete %s", canonical({"job_id": job["job_id"], "kb_id": job["kb_id"]}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, KGError) and exc.code == "PUBLICATION_REPLACED":
                return
            try:
                current = await c.metadata.job(job["job_id"])
                kb = await c.metadata.kb(job["kb_id"])
            except Exception:
                # 交给下次 Worker 启动恢复，避免继续领取任务时遗留停读门禁。
                log.error("job_outcome_unknown %s", canonical({"job_id": job["job_id"]}))
                raise
            if current["state"] in SUCCESS_STATES and kb["maintenance_state"] == "READY":
                return

            error = execution_error(exc)

            if current["state"] == "CLEANING":
                state = "CLEANUP_FAILED" if current["attempt"] >= 3 else "CLEANING"
            # 已进入写入或计划失效时，优先保留维护门禁并转入修复。
            elif (
                current["state"] in WRITE_STATES
                or kb["maintenance_state"] in {"APPLYING", "REPAIR_REQUIRED"}
                or error["code"] == "PLAN_INVALID"
            ):
                state = "NEEDS_REPAIR"
                await c.metadata.maintenance(job["kb_id"], "REPAIR_REQUIRED", job["job_id"])
            elif current["state"] in {"PREPARED", "DRAINING"}:
                if current["state"] == "DRAINING":
                    await c.metadata.cancel_publication(current)
                state = "PREPARED"
            elif error["retryable"] and current["attempt"] < 3:
                state = "PREPARING"
            else:
                state = "PREPARE_FAILED"

            fields = {}
            if c.settings.worker_distribution is not None and state == "PREPARED":
                fields = {"last_step": "publish_failed", "attempt": max(1, current["attempt"])}
            await c.metadata.update_job(
                job["job_id"],
                state=state,
                error_json=canonical(error),
                next_attempt_at=time.time() + min(30, 2 ** max(1, current["attempt"])),
                **fields,
            )
            log.error(
                "job_failed %s",
                canonical(
                    {
                        "job_id": job["job_id"],
                        "kb_id": job["kb_id"],
                        "state": state,
                        "code": error["code"],
                        "exception_type": type(exc).__name__,
                    }
                ),
            )

    async def cleanup(self):
        """先回收已完成任务的检查点，再尝试清理无引用的过期来源文件。"""
        c = self.resources
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=c.settings.checkpoint_retention_hours)
        ).isoformat()
        cutoff = time.time() - c.settings.checkpoint_retention_hours * 3600
        for job in await c.metadata.expired_checkpoints(threshold):
            try:
                await blocking(c.files.remove, str(c.files.job_dir(job["job_id"])))
                await c.metadata.update_job(job["job_id"], plan_path=None, cleanup_pending=0)
            except (OSError, KGError) as exc:
                log.warning("Checkpoint cleanup failed for %s (%s)", job["job_id"], type(exc).__name__)
                await c.metadata.update_job(job["job_id"], cleanup_pending=1)

        if c.settings.worker_distribution is not None:
            await self.cleanup_retired_attempts(cutoff)

        # 排他锁使引用快照与文件回收不会撞上尚未登记完成的上传。
        async with c.gate.source_files(cleanup=True) as acquired:
            if acquired:
                referenced = await c.metadata.referenced_raw_paths()
                for path in await blocking(c.files.orphan_files, referenced, cutoff):
                    await blocking(c.files.remove, path)

    async def cleanup_retired_attempts(self, cutoff):
        c = self.resources
        cursor, attempts = await blocking(c.files.attempts_page, self.artifact_cursor)
        job_ids = sorted({path.parts[1] for path, modified in attempts.items() if modified < cutoff})
        jobs = await c.metadata.artifact_jobs(job_ids) if job_ids else {}
        for path, modified in attempts.items():
            if modified >= cutoff:
                continue
            job = jobs.get(path.parts[1])
            if job and str(job["queue_seq"]) == path.parts[2] and (
                job["plan_path"] or job["state"] not in SUCCESS_STATES | {"INVALIDATED"}
            ):
                continue
            await blocking(c.files.retire_attempt, path)
        self.artifact_cursor = cursor

    async def run(self, once=False, *, dispatcher=None, stop=None):
        """持有正式写入进程锁。分布式模式只领取已准备的计划。"""
        c = self.resources
        if not await c.metadata.ready():
            raise KGError("STORAGE_NOT_INITIALIZED", "Run kg init-storage before starting the worker", 503)

        async with c.gate.worker():
            publication_only = c.settings.worker_distribution is not None
            await c.metadata.reconcile_interrupted(publication_only=publication_only)
            dispatch_task = None
            if dispatcher is not None and not once:
                dispatch_task = asyncio.create_task(dispatcher.run())
            try:
                if dispatcher is not None and once:
                    await dispatcher.tick()
                while not (stop and stop.is_set()):
                    job = await c.metadata.claim(time.time(), publication_only=publication_only)
                    if job:
                        await self.execute(job)
                    if time.monotonic() - self.last_cleanup > 60:
                        try:
                            await self.cleanup()
                        except KGError as exc:
                            log.warning("Maintenance cleanup unavailable (%s)", exc.code)
                        self.last_cleanup = time.monotonic()
                    if once:
                        return
                    if not job:
                        await asyncio.sleep(c.settings.poll_interval)
            finally:
                if dispatch_task is not None:
                    dispatch_task.cancel()
                    await asyncio.gather(dispatch_task, return_exceptions=True)


async def run_worker(settings=None, once=False, role=None):
    """按配置运行本地 Worker 或分布式准备、写入角色。"""
    context = Context(settings)
    queue = None
    signals = ()
    loop = asyncio.get_running_loop()
    try:
        distribution = context.settings.worker_distribution
        resources = context.construction_resources
        worker = Worker(resources, context.construction)
        if not await context.metadata.ready():
            raise KGError("STORAGE_NOT_INITIALIZED", "Run kg init-storage before starting the worker", 503)
        await context.check_storage_mode()
        if distribution is None:
            if role is not None:
                raise KGError("INVALID_ARGUMENT", "A Worker role requires worker_distribution")
            await worker.run(once)
            return
        if role not in {"prepare", "publish"}:
            raise KGError("INVALID_ARGUMENT", "Select --role prepare or --role publish")
        from .adapters.redis_stream import PreparationQueue

        await blocking(context.files.initialize)
        queue = PreparationQueue(distribution)
        stop = asyncio.Event()
        if not once:
            signals = (signal.SIGINT, signal.SIGTERM)
            for signum in signals:
                loop.add_signal_handler(signum, stop.set)
        if role == "prepare":
            await PreparationWorker(resources, context.construction, queue).run(once=once, stop=stop)
        else:
            await worker.run(once, dispatcher=PreparationDispatcher(resources, queue), stop=stop)
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)
        if queue is not None:
            await queue.close()
        await context.close()
