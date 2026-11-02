"""在线程中执行阻塞操作，并在取消时等到操作结束再释放外层资源。"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")


async def blocking(function: Callable[..., T], *args, **kwargs) -> T:
    """取消调用方时先排空线程工作，避免外层存储门禁提前释放。"""
    task = asyncio.create_task(asyncio.to_thread(partial(function, *args, **kwargs)))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 取消异步等待不会终止线程；线程排空前，外层锁仍须保持。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        # 读取线程异常以完成异常观察，对调用方仍保留取消结果。
        try:
            task.result()
        except BaseException:
            pass
        raise
