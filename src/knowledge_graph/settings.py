"""定义运行配置及校验规则，按文件与环境变量的优先级加载配置。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import Field, model_validator

from .domain.model import EmbeddingProfile, StrictModel
from .domain.source import DEFAULT_STRUCTURE_WINDOW_CHARS


class LanguageProfile(StrictModel):
    provider: Literal["openai", "bailian"] = "openai"
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "z-ai/glm-5.3"
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout: float = Field(default=60, gt=0)
    max_input_chars: int = Field(default=60000, ge=1000)
    max_output_tokens: int = Field(default=12000, ge=1)
    max_context_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    structured_output: Literal["json_object", "json_schema"] = "json_object"
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe(self):
        if (
            "@" in self.base_url
            or "?" in self.base_url
            or not self.base_url.startswith(("https://", "http://"))
        ):
            raise ValueError("LLM base URL must not contain credentials or query parameters")

        if self.max_context_tokens is not None and self.max_context_tokens <= self.max_output_tokens:
            raise ValueError("LLM context budget must reserve space for input as well as output")

        # 扩展参数不能覆盖请求结构和预算，也不能携带凭据字段。
        reserved = {
            "model",
            "messages",
            "response_format",
            "max_tokens",
            "max_completion_tokens",
            "stream",
            "tools",
            "tool_choice",
        }
        if set(self.extra_body) & reserved:
            raise ValueError("LLM extra_body cannot override request structure or token budgets")
        if any(
            "key" in key.lower() or "secret" in key.lower() or "token" in key.lower()
            for key in self.extra_body
        ):
            raise ValueError("LLM secrets belong in the credential environment")
        return self


class WorkerDistribution(StrictModel):
    redis_url_env: str = "REDIS_URL"
    stream: str = "kg:{prepare}:stream"
    group: str = "prepare"
    heartbeat_seconds: float = Field(default=10, gt=0)
    claim_idle_seconds: float = Field(default=60, gt=0)
    minio_endpoint: str
    minio_bucket: str
    minio_access_key_env: str = "MINIO_ACCESS_KEY"
    minio_secret_key_env: str = "MINIO_SECRET_KEY"

    @model_validator(mode="after")
    def validate_endpoints(self):
        from urllib.parse import urlsplit

        url = urlsplit(self.minio_endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.query
            or url.fragment
        ):
            raise ValueError("MinIO endpoint must be an HTTP URL without credentials")
        if not self.stream or not self.group or self.claim_idle_seconds <= 2 * self.heartbeat_seconds:
            raise ValueError("Redis requires a stream, group and an idle timeout over twice the heartbeat")
        return self


class Settings(StrictModel):
    data_root: str = "/srv/knowledge-graph/data"
    postgres_dsn_env: str = "POSTGRES_DSN"
    worker_distribution: WorkerDistribution | None = None
    chunk_similarity_threshold: float = Field(default=0.97, ge=0, le=1)
    chunk_decision_model: str = "typesafe/jev-1.13"
    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password_env: str = "NEO4J_PASSWORD"
    neo4j_database: str = "neo4j"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key_env: str = "QDRANT_API_KEY"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    upload_limit: int = Field(default=20 * 1024 * 1024, ge=1)
    chunk_target: int = Field(default=1200, ge=100)
    chunk_max: int = Field(default=2000, ge=100)
    chunk_overlap: int = Field(default=150, ge=0)
    extraction_scope_policy: Literal["whole", "structure"] = "whole"
    extraction_structure_window_chars: int = Field(default=DEFAULT_STRUCTURE_WINDOW_CHARS, ge=1)
    drain_timeout: float = Field(default=30, gt=0)
    poll_interval: float = Field(default=1, gt=0)
    graph_timeout: float = Field(default=3, gt=0)
    retrieval_timeout: float = Field(default=15, gt=0)
    retrieval_rerank_snippet_chars: int = Field(default=2000, ge=80, le=12000)
    rerank_enabled: bool = True
    rerank_candidate_limit: int = Field(default=80, ge=1, le=100)
    rerank_timeout: float = Field(default=45, gt=0)
    candidate_scan_limit: int = Field(default=5000, ge=1, le=10000)
    maintenance_scan_limit: int = Field(default=100000, ge=1)
    detail_max_chars: int = Field(default=1000000, ge=1)
    checkpoint_retention_hours: float = Field(default=24, ge=0)
    llm: LanguageProfile = Field(default_factory=LanguageProfile)
    construction_llm: LanguageProfile | None = None
    embedding: EmbeddingProfile = Field(default_factory=EmbeddingProfile)

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.chunk_target > self.chunk_max or self.chunk_overlap >= self.chunk_max:
            raise ValueError("Chunk overlap and target must fit inside the maximum chunk size")

        root = Path(self.data_root)
        if not root.is_absolute() or str(root).startswith("/mnt/"):
            raise ValueError("The data root must be an absolute path on the local Linux filesystem")
        return self

    @classmethod
    def load(cls, filename: str | None = None):
        """先补充未设置的环境变量，再加载配置文件并应用 KG_* 覆盖。"""
        load_dotenv(os.getenv("KG_ENV_FILE", ".env"), override=False)

        filename = filename or os.getenv("KG_CONFIG")
        data = yaml.safe_load(Path(filename).read_text("utf-8")) if filename else {}
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError("Application configuration must be a mapping")

        for key in cls.model_fields:
            value = os.getenv("KG_" + key.upper())
            if value is not None:
                data[key] = yaml.safe_load(value)
        return cls.model_validate(data)
