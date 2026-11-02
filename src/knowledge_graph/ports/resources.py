from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncContextManager, Protocol

from ..settings import Settings
from .files import FilesPort, ParserPort
from .graph import GraphPort
from .language import LanguagePort
from .metadata import MetadataPort
from .vectors import VectorsPort


class ReadGatePort(Protocol):
    """跨进程协调查询、维护与来源文件清理的上下文管理器。"""

    def read(self, kb_id: str) -> AsyncContextManager[dict]:
        """获取共享读锁并确认知识库就绪，进入上下文后返回有效元数据。"""
        ...

    def exclusive(self, kb_id: str, timeout: float) -> AsyncContextManager[None]:
        """等待既有读操作排空后持有排他锁，超过 timeout 时失败。"""
        ...

    def worker(self) -> AsyncContextManager[None]: ...
    def server(self) -> AsyncContextManager[None]: ...
    def source_files(self, *, cleanup: bool = False) -> AsyncContextManager[bool]:
        """上传等待共享锁；清理尝试排他锁，拿不到时返回 False。"""
        ...


@dataclass(frozen=True)
class Resources:
    settings: Settings
    metadata: MetadataPort
    graph: GraphPort
    vectors: VectorsPort
    files: FilesPort
    language: LanguagePort
    gate: ReadGatePort
    parser: ParserPort
