"""消费准备队列，重投和故障接管都通过同一条准备入口。"""

from __future__ import annotations

import asyncio
import logging
import time

from redis.exceptions import RedisError

from ...domain import KGError, canonical


log = logging.getLogger(__name__)


def execution_error(exc):
    if isinstance(exc, KGError):
        return exc.as_dict()
    return KGError(
        "EXECUTION_FAILED", "An internal execution error occurred", 503, retryable=True,
        exception_type=type(exc).__name__,
    ).as_dict()


class PreparationWorker:
    def __init__(self, resources, construction, queue):
        self.resources = resources
        self.construction = construction
        self.queue = queue
        self.heartbeat_seconds = resources.settings.worker_distribution.heartbeat_seconds

    async def run(self, *, once=False, stop=None):
        while not (stop and stop.is_set()):
            try:
                await self.queue.initialize()
                message = await self.queue.receive(once=once)
                if message and not (stop and stop.is_set()):
                    await self.handle(message)
            except (RedisError, KGError) as exc:
                log.warning("Preparation queue unavailable (%s)", getattr(exc, "code", type(exc).__name__))
                if once:
                    raise
                await asyncio.sleep(self.resources.settings.poll_interval)
            if once:
                return

    async def handle(self, message):
        metadata = self.resources.metadata
        for _ in range(2):
            try:
                observed = await metadata.job(message.job_id)
            except KGError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                await self.queue.acknowledge(message, settled=True)
                return
            if observed["queue_seq"] != message.queue_seq or observed["state"] not in {"QUEUED", "PREPARING"}:
                await self.queue.acknowledge(message, settled=True)
                return
            if not await self.queue.owns(message):
                await self.queue.acknowledge(message)
                return
            now = time.time()
            if observed["next_attempt_at"] > now:
                await self.queue.renew(message, delay=observed["next_attempt_at"] - now)
                return
            claimed = await metadata.claim_preparation(observed, now)
            if claimed:
                if claimed["state"] == "PREPARING":
                    await self._prepare(message, claimed)
                else:
                    await self.queue.acknowledge(message, settled=True)
                return

        # Ownership or the database snapshot changed while claiming. Recheck before the next attempt.
        await self.queue.renew(message, delay=0)

    async def _heartbeat(self, message):
        while True:
            if not await self.queue.renew(message):
                return
            await asyncio.sleep(self.heartbeat_seconds)

    async def _prepare(self, message, job):
        task = asyncio.create_task(self.construction.prepare(job))
        heartbeat = asyncio.create_task(self._heartbeat(message))
        try:
            done, _ = await asyncio.wait((task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                heartbeat.result()
                return
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                task.result()
            except Exception as exc:
                await self._failed(message, job, exc)
            else:
                await self.queue.acknowledge(message, settled=True)
        finally:
            for running in (task, heartbeat):
                if not running.done():
                    running.cancel()
            await asyncio.gather(task, heartbeat, return_exceptions=True)

    async def _failed(self, message, job, exc):
        if not await self.queue.owns(message):
            return
        metadata = self.resources.metadata
        error = execution_error(exc)
        retry = error["retryable"] and job["attempt"] < 3
        delay = min(30, 2 ** job["attempt"]) if retry else 0
        if not await metadata.update_preparation(
            job, state="PREPARING" if retry else "PREPARE_FAILED", error_json=canonical(error),
            next_attempt_at=time.time() + delay,
        ):
            current = await metadata.job(job["job_id"])
            if current["queue_seq"] != job["queue_seq"] or current["state"] not in {"QUEUED", "PREPARING"}:
                await self.queue.acknowledge(message, settled=True)
            return
        log.warning("Preparation failed for %s on attempt %s (%s)", job["job_id"], job["attempt"], error["code"])
        if retry:
            await self.queue.renew(message, delay=delay)
        else:
            await self.queue.acknowledge(message, settled=True)


class PreparationDispatcher:
    def __init__(self, resources, queue):
        self.resources = resources
        self.queue = queue
        self.cursor = 0

    async def dispatch(self):
        await self.queue.initialize()
        rows = await self.resources.metadata.preparation_candidates(self.cursor, time.time())
        for row in rows:
            await self.queue.enqueue(row["job_id"], row["queue_seq"])
            self.cursor = row["queue_seq"]
        if len(rows) < 50:
            self.cursor = 0

    async def tick(self):
        try:
            await self.dispatch()
        except (RedisError, KGError) as exc:
            log.warning("Could not dispatch preparation (%s)", getattr(exc, "code", type(exc).__name__))

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(self.resources.settings.poll_interval)
