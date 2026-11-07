"""组装适配器与服务，统一持有并释放应用级客户端资源。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .adapters.files import Files
from .adapters.metadata import Metadata
from .adapters.models import Language
from .adapters.neo4j import Graph
from .adapters.parsers import Parser
from .adapters.qdrant import Vectors
from .domain import KGError
from .ports.resources import Resources
from .runtime import ReadGate, blocking
from .settings import Settings


class Context:
    """应用组合根；创建共享资源并注入服务，不承载服务之间的业务调用。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        distribution = self.settings.worker_distribution
        if distribution is None:
            self.files = Files(self.settings.data_root)
        else:
            from .adapters.files.minio import MinioFiles

            self.files = MinioFiles(self.settings.data_root, distribution)
        self.metadata = Metadata(self.settings.postgres_dsn_env)
        self.graph, self.vectors, self.language = (
            Graph(self.settings),
            Vectors(self.settings),
            Language(self.settings),
        )
        self.parser = Parser(self.settings)
        self.gate = ReadGate(self.files, self.metadata)

        # Resources 仅传递共享引用，客户端的创建与关闭由 Context 负责。
        self.resources = Resources(
            settings=self.settings,
            metadata=self.metadata,
            graph=self.graph,
            vectors=self.vectors,
            files=self.files,
            language=self.language,
            gate=self.gate,
            parser=self.parser,
        )

        # 未配置独立构建模型时，两条调用路径复用同一个客户端。
        self.construction_language = self.language
        self.construction_resources = self.resources
        if self.settings.construction_llm is not None:
            construction_settings = self.settings.model_copy(update={"llm": self.settings.construction_llm})
            self.construction_language = Language(construction_settings)
            self.construction_resources = replace(
                self.resources, settings=construction_settings, language=self.construction_language
            )

        from .services.construction.pipeline import Construction
        from .services.documents.service import Management
        from .services.model_config.service import ModelConfiguration
        from .services.retrieval import Retrieval

        self.model_config = ModelConfiguration(self.resources)
        self.management = Management(self.resources)
        self.construction = Construction(self.construction_resources)
        self.retrieval = Retrieval(self.resources)

    async def initialize(self):
        """准备本地目录，并在 API 与 Worker 进程锁下初始化元数据和图约束。"""
        await blocking(self.files.initialize)
        async with self.gate.server():
            async with self.gate.worker():
                await blocking(self.metadata.initialize)
                await self.check_storage_mode()
                await self.graph.initialize()

    async def check_storage_mode(self):
        if not await self.metadata.storage_mode_matches(self.settings.worker_distribution is not None):
            raise KGError(
                "INCONSISTENT_STORAGE", "Registered files use a different storage mode; migrate references first", 503
            )

    async def ready(self):
        """检查元数据初始化状态，再汇总图与向量存储的可用性。"""
        if not await self.metadata.ready():
            raise KGError(
                "STORAGE_NOT_INITIALIZED", "Run kg init-storage before starting application processes", 503
            )
        checks = await asyncio.gather(self.graph.ready(), self.vectors.ready(), return_exceptions=True)
        dependencies = {"metadata": True, "graph": checks[0] is True, "vectors": checks[1] is True}
        if self.settings.worker_distribution is not None:
            try:
                dependencies["objects"] = await blocking(self.files.ready)
            except KGError:
                dependencies["objects"] = False
        if not all(dependencies.values()):
            raise KGError(
                "UPSTREAM_UNAVAILABLE",
                "Storage dependencies or schema initialization are unavailable",
                503,
                retryable=True,
                dependencies=dependencies,
            )
        return {"status": "ready", "dependencies": dependencies}

    async def close(self):
        """释放应用持有的客户端，共用的模型客户端只关闭一次。"""
        languages = [self.language]
        if self.construction_language is not self.language:
            languages.append(self.construction_language)

        closers = [self.vectors.close(), self.graph.close()]
        if self.settings.worker_distribution is not None:
            closers.append(blocking(self.files.close))
        await asyncio.gather(*(language.close() for language in languages), *closers)
