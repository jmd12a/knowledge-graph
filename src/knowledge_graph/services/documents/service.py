from __future__ import annotations

import json
from pathlib import Path

from ...domain import BusinessModel, KGError, canonical, new_id, valid_id
from ...domain.model import select_mapping
from ...ports.resources import Resources
from ...runtime import blocking
from ..model_config.service import ModelConfiguration


class Management:
    """共用的资料与任务管理服务；维护请求登记后交由 Worker 执行。"""

    def __init__(self, resources: Resources):
        self.c = resources
        self.models = ModelConfiguration(resources)

    key = staticmethod(ModelConfiguration.key)

    async def create_kb(self, name, model_data, profile_data=None):
        return await self.models.create_kb(name, model_data, profile_data)

    async def change_model(self, kb_id, model_data, key):
        return await self.models.change_model(kb_id, model_data, key)

    async def reindex(self, kb_id, profile_data, key):
        return await self.models.reindex(kb_id, profile_data, key)

    @staticmethod
    def _page(limit, offset):
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise KGError("INVALID_ARGUMENT", "Pagination requires limit 1..1000 and a nonnegative offset")
        return limit, offset

    async def list_kbs(self, limit=100, offset=0):
        limit, offset = self._page(limit, offset)
        rows = await self.c.metadata.list_kbs()
        return rows[offset : offset + limit]

    async def list_documents(self, kb_id, active=None, limit=100, offset=0):
        kb_id = valid_id(kb_id)
        await self.c.metadata.kb(kb_id)
        limit, offset = self._page(limit, offset)
        if active is not None and not isinstance(active, bool):
            raise KGError("INVALID_ARGUMENT", "active must be a boolean")
        return await self.c.metadata.list_documents(kb_id, active, limit, offset)

    async def document(self, kb_id, document_id):
        return await self.c.metadata.document(valid_id(kb_id), valid_id(document_id))

    async def document_jobs(self, kb_id, document_id, limit=100, offset=0):
        kb_id, document_id = valid_id(kb_id), valid_id(document_id)
        await self.c.metadata.document(kb_id, document_id)
        limit, offset = self._page(limit, offset)
        rows = await self.c.metadata.document_jobs(kb_id, document_id)
        return rows[offset : offset + limit]

    async def content(self, kb_id, document_id):
        """读取下载元信息；调用方须持有读门禁直到响应流结束。"""
        document = await self.document(kb_id, document_id)
        if document["deleted_at"] or not document["raw_path"]:
            raise KGError("NOT_FOUND", "Document content has been deleted", 404)
        if not await blocking(self.c.files.exists, document["raw_path"]):
            raise KGError("SOURCE_UNAVAILABLE", "Registered document content is unavailable", 503)
        return {key: document[key] for key in ("raw_path", "original_name", "media_type", "byte_length")}

    async def job(self, job_id):
        return await self.c.metadata.job(valid_id(job_id))

    async def upload(self, kb_id, upload, source, mapping_id, key):
        """持有来源文件共享锁，保护暂存、发布及登记不被孤儿文件清理打断。"""
        async with self.c.gate.source_files():
            return await self._upload(kb_id, upload, source, mapping_id, key)

    async def _upload(self, kb_id, upload, source, mapping_id, key):
        """先保存原文再原子登记资料与任务，并处理同幂等键的并发提交。"""
        kb_id, key = valid_id(kb_id), self.key(key)
        kb = await self.c.metadata.kb(kb_id)
        if not isinstance(source, dict):
            raise KGError("INVALID_ARGUMENT", "Source metadata must be an object")
        try:
            canonical(source)
        except (ValueError, TypeError) as exc:
            raise KGError("INVALID_ARGUMENT", "Source metadata must contain valid JSON values") from exc
        original_name = (getattr(upload, "filename", None) or "document").replace("\\", "/").split("/")[-1]
        extension = Path(original_name).suffix.lower()
        formats = {
            ".txt": "txt",
            ".md": "md",
            ".markdown": "md",
            ".pdf": "pdf",
            ".csv": "csv",
            ".json": "json",
        }
        if extension not in formats:
            raise KGError("UNSUPPORTED_FORMAT", "Supported formats are TXT, Markdown, CSV, JSON and text PDF")
        # 重放沿用原请求的映射选择，不能由当前模型重新解释旧请求。
        requested_mapping_id = mapping_id
        old = await self.c.metadata.request(kb_id, "IMPORT", key)
        if not old:
            model = BusinessModel.model_validate(kb["model"])
            mapping_id, _ = select_mapping(model, formats[extension], mapping_id)
            domains = source.get("domain_ids", [])
            if (
                not isinstance(domains, list)
                or any(not isinstance(domain, str) for domain in domains)
                or set(domains) - {domain["id"] for domain in model.domains}
            ):
                raise KGError("INVALID_ARGUMENT", "Source domain hints must be configured domains")
        document_id = new_id()
        staged = await self.c.files.stage_upload(upload, self.c.settings.upload_limit)
        temporary, final_path = staged["path"], None
        try:
            request_data = {
                "original_name": original_name,
                "media_type": formats[extension],
                "source": source,
                "mapping_id": mapping_id,
                "byte_length": staged["byte_length"],
            }
            if old:
                return await self._same_upload(kb_id, old, request_data, temporary, requested_mapping_id)

            # 原文发布完成后才登记元数据，避免任务引用尚未保存的文件。
            final_path = await blocking(self.c.files.publish_upload, temporary, kb_id, document_id, extension)
            document = {
                "kb_id": kb_id,
                "document_id": document_id,
                "original_name": original_name,
                "media_type": formats[extension],
                "raw_path": final_path,
                "byte_length": staged["byte_length"],
                "source": source,
                "mapping_id": mapping_id,
            }
            registered = await self.c.metadata.register_upload(
                document, {"mapping_id": mapping_id}, key, request_data
            )
            # 唯一约束可能使另一请求先登记成功，核对原文后回收本次发布的副本。
            if registered["document_id"] != document_id:
                result = await self._same_upload(
                    kb_id, registered, request_data, final_path, requested_mapping_id
                )
                await blocking(self.c.files.remove, str(Path(final_path).parent))
                final_path = None
                return result
            job = await self.c.metadata.job(registered["job_id"])
            return self._accepted(document_id, job)
        except BaseException:
            if final_path:
                try:
                    # 登记结果不确定时先确认资料不存在，避免误删已登记成功的原文。
                    if not await self.c.metadata.document_exists(document_id):
                        await blocking(self.c.files.remove, str(Path(final_path).parent))
                except BaseException:
                    pass
            raise
        finally:
            await blocking(self.c.files.remove, temporary)

    @staticmethod
    def _accepted(document_id, job):
        return {
            "document_id": document_id,
            "job_id": job["job_id"],
            "state": job["state"],
            "status_url": "/api/v1/jobs/" + job["job_id"],
        }

    async def _same_upload(self, kb_id, previous, request_data, path, requested_mapping_id):
        """核对请求字段与原文字节；同键重放返回原任务，不能复活已删除资料。"""
        saved = previous.get("request")
        if saved is None:
            saved = json.loads(previous["request_json"])
        if requested_mapping_id is None:
            request_data = request_data | {"mapping_id": saved["mapping_id"]}
        if canonical(saved) != canonical(request_data):
            raise KGError("IDEMPOTENCY_CONFLICT", "Upload key belongs to a different request", 409)
        async with self.c.gate.read(kb_id):
            document = await self.c.metadata.document(kb_id, previous["document_id"])
            if document["deleted_at"] or not document["raw_path"]:
                raise KGError(
                    "IDEMPOTENCY_CONFLICT",
                    "A deleted upload key cannot recreate a document; use a new key",
                    409,
                )
            try:
                identical = await blocking(self.c.files.same_bytes, Path(document["raw_path"]), Path(path))
            except OSError as exc:
                raise KGError(
                    "SOURCE_UNAVAILABLE",
                    "Original upload bytes are unavailable for idempotency comparison",
                    503,
                ) from exc
            if not identical:
                raise KGError("IDEMPOTENCY_CONFLICT", "Upload bytes differ from the original request", 409)
            return self._accepted(document["document_id"], await self.c.metadata.job(previous["job_id"]))

    async def delete_document(self, kb_id, document_id, key):
        kb_id, document_id, key = valid_id(kb_id), valid_id(document_id), self.key(key)
        await self.c.metadata.document(kb_id, document_id)
        old = await self.c.metadata.request(kb_id, "DELETE", key)
        if old:
            request = old.get("request") or json.loads(old["request_json"])
            if request != {"document_id": document_id}:
                raise KGError("IDEMPOTENCY_CONFLICT", "Delete key belongs to a different document", 409)
            return await self.c.metadata.job(old["job_id"])
        job_id = await self.c.metadata.enqueue(
            kb_id, "DELETE", document_id, {"document_id": document_id}, key, {"document_id": document_id}
        )
        return await self.c.metadata.job(job_id)

    async def retry(self, job_id, key):
        job_id, key = valid_id(job_id), self.key(key)
        job = await self.c.metadata.job(job_id)
        if job["operation"] == "IMPORT":
            document = await self.c.metadata.document(job["kb_id"], job["document_id"])
            if document["deleted_at"]:
                raise KGError("INVALID_JOB_STATE", "Deleted document imports cannot be retried", 409)
        # 准备文件由 Worker 独占修改；准备失败后的重试通过 queue_seq 切换准备代次。
        await self.c.metadata.retry(job_id, key)
        return await self.c.metadata.job(job_id)

    async def issues(self, job_id, limit=100, offset=0):
        job_id = valid_id(job_id)
        await self.c.metadata.job(job_id)
        limit, offset = self._page(limit, offset)
        rows = await self.c.metadata.candidate_issues(job_id, limit, offset)
        for row in rows:
            for field in ("candidate", "details"):
                if field not in row and field + "_json" in row:
                    row[field] = json.loads(row.pop(field + "_json"))
        return rows
