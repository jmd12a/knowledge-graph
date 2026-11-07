from __future__ import annotations

from typing import Protocol

from ..domain.knowledge import Entity, Relation


class GraphPort(Protocol):
    """当前图知识的存取协议；业务身份及来源合并由服务层决定。"""

    async def ready(self) -> bool: ...
    async def close(self) -> None: ...
    async def all(self, kb_id: str) -> tuple[list[Entity], list[Relation]]: ...
    async def get(self, kb_id: str, kind: str, object_id: str) -> dict | None: ...
    async def find_identity(self, kb_id: str, entity_type: str, identity: dict) -> str | None: ...
    async def by_id(self, kb_id: str, kind: str, ids: list[str]) -> list[dict]: ...
    async def by_identity(self, kb_id: str, kind: str, keys: list) -> list[dict]: ...
    async def relation_groups(self, kb_id: str, groups: list[list[str]]) -> list[Relation]: ...
    async def counts(self, kb_id: str) -> dict[str, int]: ...
    async def candidates(
        self,
        kb_id: str,
        text: str,
        scan_limit: int,
        entity_types: list[str] | None = None,
        domain_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> tuple[list[Entity], list[Entity]]:
        """返回精确与关键词两组候选；各组可多取一项以判定扫描截断。"""
        ...

    async def fulltext(
        self,
        kb_id: str,
        text: str,
        scan_limit: int,
        entity_types: list[str] | None = None,
        domain_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[Entity]:
        """返回已应用范围过滤的全文候选，最多 scan_limit + 1 项。"""
        ...

    async def adjacent(
        self, kb_id: str, node_id: str, direction: str, relation_types: list[str], limit: int
    ) -> list[tuple[Entity, Relation]]:
        """按方向和关系类型枚举邻接实体与边，结果受 limit 限制。"""
        ...

    async def apply(
        self,
        kb_id: str,
        model_data: dict,
        entities: list[Entity],
        relations: list[Relation],
        delete_entities: list[str],
        delete_relations: list[str],
        rebuild: bool = False,
    ) -> None:
        """应用已准备的幂等图补丁；rebuild 时先清理该知识库。"""
        ...
