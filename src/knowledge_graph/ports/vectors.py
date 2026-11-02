from __future__ import annotations

from typing import Protocol

from ..domain.source import ScanPage, VectorPoint


class VectorsPort(Protocol):
    """管理图对象索引与文档切片索引；kind 分别为 objects 或 chunks。"""

    async def ready(self) -> bool: ...
    async def close(self) -> None: ...
    async def exists(self, kb_id: str, kind: str) -> bool: ...
    async def ensure(self, kb_id: str, profile_data: dict, recreate: bool = False) -> None: ...
    async def scan(
        self,
        kb_id: str,
        kind: str,
        *,
        limit: int,
        offset: str | int | None = None,
        with_vectors: bool = False,
        document_ids: list[str] | None = None,
    ) -> ScanPage:
        """读取一页数据；next_offset 用于续扫，complete 表示扫描已结束。"""
        ...

    async def all(
        self,
        kb_id: str,
        kind: str,
        with_vectors: bool = True,
        document_ids: list[str] | None = None,
        *,
        max_points: int,
    ) -> list[dict]:
        """完整扫描受 max_points 约束；超限抛错，不返回伪装完整的部分结果。"""
        ...

    async def read(self, kb_id: str, kind: str, ids: list[str]) -> list[dict]: ...
    async def upsert(self, kb_id: str, kind: str, points: list[VectorPoint]) -> None: ...
    async def delete(
        self,
        kb_id: str,
        kind: str,
        ids: list[str] | None = None,
        document_ids: list[str] | None = None,
        all_points: bool = False,
    ) -> None: ...
    async def search(self, kb_id: str, kind: str, vector: list[float], limit: int, filters) -> list[dict]: ...
    async def keyword(self, kb_id: str, query: str, limit: int, filters) -> tuple[list[dict], bool]:
        """返回切片关键词候选及截断标记，布尔值为真表示候选未穷尽。"""
        ...
