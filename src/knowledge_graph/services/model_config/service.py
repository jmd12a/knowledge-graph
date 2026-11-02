from __future__ import annotations

from ...domain import BusinessModel, EmbeddingProfile, KGError, valid_id
from ...ports.resources import Resources


class ModelConfiguration:
    """校验模型与向量配置，将后续调整登记为维护任务。"""

    def __init__(self, resources: Resources):
        self.c = resources

    @staticmethod
    def key(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise KGError(
                "INVALID_ARGUMENT", "Idempotency-Key is required and must be at most 200 characters"
            )
        return value

    @staticmethod
    def validate(model_data):
        try:
            return BusinessModel.model_validate(model_data)
        except ValueError as exc:
            raise KGError(
                "INVALID_MODEL", "Business model does not satisfy the configuration contract"
            ) from exc

    @staticmethod
    def profile(profile_data):
        try:
            return EmbeddingProfile.model_validate(profile_data)
        except ValueError as exc:
            raise KGError(
                "INVALID_MODEL", "Embedding profile does not satisfy the configuration contract"
            ) from exc

    async def create_kb(self, name, model_data, profile_data=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise KGError("INVALID_ARGUMENT", "Knowledge base name must contain 1 to 200 characters")
        model = self.validate(model_data)
        default = self.c.settings.embedding.model_dump() if profile_data is None else profile_data
        profile = self.profile(default)
        return await self.c.metadata.create_kb(name.strip(), model.model_dump(), profile.model_dump())

    async def change_model(self, kb_id, model_data, key):
        """登记整库重建；新模型在维护成功前不会成为有效配置。"""
        kb_id, key = valid_id(kb_id), self.key(key)
        await self.c.metadata.kb(kb_id)
        model = self.validate(model_data).model_dump()
        job_id = await self.c.metadata.enqueue(
            kb_id, "REBUILD", None, {"model": model}, key, {"model": model}
        )
        return await self.c.metadata.job(job_id)

    async def reindex(self, kb_id, profile_data, key):
        """登记索引重建，并区分调用方参数与任务生成的修订标识。"""
        kb_id, key = valid_id(kb_id), self.key(key)
        await self.c.metadata.kb(kb_id)
        profile = self.profile(profile_data).model_dump()
        # 自动生成的修订标识属于持久化任务，不参与调用方请求的幂等比较。
        request_profile = {field: value for field, value in profile.items() if field != "revision_id"}
        if isinstance(profile_data, dict) and "revision_id" in profile_data:
            request_profile["revision_id"] = profile["revision_id"]
        job_id = await self.c.metadata.enqueue(
            kb_id,
            "REINDEX",
            None,
            {"embedding_profile": profile},
            key,
            {"embedding_profile": request_profile},
        )
        return await self.c.metadata.job(job_id)
