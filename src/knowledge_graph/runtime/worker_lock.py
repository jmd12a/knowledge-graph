"""限制同一数据目录中的 API 与 Worker 各自只运行一个进程。"""

from __future__ import annotations

import fcntl
import os
from contextlib import asynccontextmanager

from ..domain import KGError
from ..ports.files import FilesPort


@asynccontextmanager
async def process_lock(files: FilesPort, name: str):
    """在应用生命周期内持有指定进程锁，重复启动立即报错。"""
    if name not in {"worker", "server"}:
        raise ValueError("Unknown application process lock")
    fd = os.open(files.path("locks", name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise KGError(
                "PROCESS_ALREADY_RUNNING", f"Another {name} process holds the application lock", 409
            ) from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
