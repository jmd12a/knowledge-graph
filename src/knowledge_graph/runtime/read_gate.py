"""用跨进程文件锁协调知识查询、维护写入与来源文件回收。"""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import asynccontextmanager

from ..domain import KGError, valid_id
from .worker_lock import process_lock


class ReadGate:
    def __init__(self, files, metadata):
        self.files, self.metadata = files, metadata

    def open_lock(self, kb_id):
        path = self.files.path("locks", valid_id(kb_id) + ".lock")
        return os.open(path, os.O_CREAT | os.O_RDWR, 0o600)

    @asynccontextmanager
    async def read(self, kb_id):
        """持共享锁检查维护状态，并在整个知识查询期间保留锁。"""
        fd = self.open_lock(kb_id)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise KGError(
                    "KB_MAINTENANCE", "Knowledge base is being maintained", 503, retryable=True
                ) from exc

            # 先取得共享锁再读持久化门禁，避免与维护写入交错。
            kb = await self.metadata.kb(kb_id)
            if kb["maintenance_state"] != "READY":
                raise KGError(
                    "KB_MAINTENANCE",
                    "Knowledge base is being maintained",
                    503,
                    retryable=True,
                    job_id=kb["maintenance_job_id"],
                    maintenance_state=kb["maintenance_state"],
                )

            # 共享锁覆盖整个查询，维护排他锁会等待在途查询结束。
            yield kb
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @asynccontextmanager
    async def exclusive(self, kb_id, timeout):
        """等待在途查询释放共享锁，在超时前取得维护排他锁。"""
        fd, started = self.open_lock(kb_id), time.monotonic()
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - started >= timeout:
                        raise KGError(
                            "DRAIN_TIMEOUT",
                            "Existing reads did not finish before drain timeout",
                            503,
                            retryable=True,
                        )
                    await asyncio.sleep(0.05)

            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def worker(self):
        return process_lock(self.files, "worker")

    def server(self):
        return process_lock(self.files, "server")

    @asynccontextmanager
    async def source_files(self, *, cleanup=False):
        """上传持共享锁；回收尝试排他锁，忙时返回 False 让本轮跳过。"""
        fd = os.open(self.files.path("locks", "source-files.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            while not acquired:
                try:
                    fcntl.flock(fd, (fcntl.LOCK_EX if cleanup else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if cleanup:
                        # 回收不等待上传；保留文件，交给后续清理轮次。
                        yield False
                        return
                    await asyncio.sleep(0.05)
            yield True
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
