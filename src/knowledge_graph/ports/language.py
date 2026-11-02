from __future__ import annotations

from typing import Protocol

from ..settings import LanguageProfile


class LanguagePort(Protocol):
    """提供结构化语言结果与向量；请求预算由适配器统一执行。"""

    profile: LanguageProfile

    async def close(self) -> None: ...
    def json_request_chars(self, system: str, data: dict, schema: dict | None = None) -> int:
        """Pure first-request character count, identical to the adapter's send-time budget measure."""
        ...

    async def json(self, system: str, data: dict, schema: dict | None = None) -> dict: ...
    async def embed(self, texts: list[str], profile_data: dict) -> list[list[float]]: ...
