"""在单 Worker 锁下恢复并串行执行维护任务，定期清理过期中间文件。"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .bootstrap import Context
from .domain import KGError, SUCCESS_STATES, WRITE_STATES, canonical
from .ports.resources import Resources
from .runtime import blocking

log = logging.getLogger(__name__)


class ConstructionService(Protocol):
    async def prepare(self, job: dict) -> dict: ...
    async def apply(self, job: dict) -> None: ...


class Worker:
    def __init__(self, resources: Resources, construction: ConstructionService):
        self.resources = resources
        self.construction = construction
        self.last_cleanup = 0.0

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
            try:
                current = await c.metadata.job(job["job_id"])
                kb = await c.metadata.kb(job["kb_id"])
            except Exception:
                # 最终提交是否成功尚不明确，不能反向写入失败状态。
                log.error("job_outcome_unknown %s", canonical({"job_id": job["job_id"]}))
                return
            if current["state"] in SUCCESS_STATES and kb["maintenance_state"] == "READY":
                return

            error = (
                exc.as_dict()
                if isinstance(exc, KGError)
                else KGError(
                    "EXECUTION_FAILED",
                    "An internal execution error occurred",
                    503,
                    retryable=True,
                    exception_type=type(exc).__name__,
                ).as_dict()
            )

            # 已进入写入或计划失效时，优先保留维护门禁并转入修复。
            if (
                current["state"] in WRITE_STATES
                or kb["maintenance_state"] in {"APPLYING", "REPAIR_REQUIRED"}
                or error["code"] == "PLAN_INVALID"
            ):
                state = "NEEDS_REPAIR"
                await c.metadata.maintenance(job["kb_id"], "REPAIR_REQUIRED", job["job_id"])
            elif current["state"] in {"PREPARED", "DRAINING"}:
                state = "PREPARED"
            elif error["retryable"] and current["attempt"] < 3:
                state = "PREPARING"
            else:
                state = "PREPARE_FAILED"

            await c.metadata.update_job(
                job["job_id"],
                state=state,
                error_json=canonical(error),
                next_attempt_at=time.time() + min(30, 2 ** current["attempt"]),
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
        for job in await c.metadata.expired_checkpoints(threshold):
            try:
                await blocking(c.files.remove, str(c.files.job_dir(job["job_id"])))
                await c.metadata.update_job(job["job_id"], plan_path=None, cleanup_pending=0)
            except OSError:
                await c.metadata.update_job(job["job_id"], cleanup_pending=1)

        # 排他锁使引用快照与文件回收不会撞上尚未登记完成的上传。
        async with c.gate.source_files(cleanup=True) as acquired:
            if acquired:
                referenced = await c.metadata.referenced_raw_paths()
                cutoff = time.time() - c.settings.checkpoint_retention_hours * 3600
                for path in await blocking(c.files.orphan_files, referenced, cutoff):
                    await blocking(c.files.remove, path)

    async def run(self, once=False):
        """取得进程锁后先恢复中断任务，再持续领取并执行可调度任务。"""
        c = self.resources
        if not await c.metadata.ready():
            raise KGError("STORAGE_NOT_INITIALIZED", "Run kg init-storage before starting the worker", 503)

        async with c.gate.worker():
            # 必须先确认旧 Worker 不再运行，才能接管其中断状态。
            await c.metadata.reconcile_interrupted()
            while True:
                job = await c.metadata.claim(time.time())
                if job:
                    await self.execute(job)

                if time.monotonic() - self.last_cleanup > 60:
                    await self.cleanup()
                    self.last_cleanup = time.monotonic()

                if once:
                    return
                if not job:
                    await asyncio.sleep(c.settings.poll_interval)


async def run_worker(settings=None, once=False):
    """创建 Worker 专用应用资源，并在运行结束或取消时统一关闭。"""
    context = Context(settings)
    try:
        await Worker(context.resources, context.construction).run(once)
    finally:
        await context.close()
