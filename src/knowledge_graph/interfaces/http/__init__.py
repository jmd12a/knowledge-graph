from __future__ import annotations

import json
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Header, Query, Request, UploadFile
from pydantic import Field
from starlette.responses import StreamingResponse

from ...domain import StrictModel, valid_id
from ...domain.errors import KGError
from ...services.contracts import GraphRequest, RetrievalResult, RetrieveRequest, SearchRequest
from .protocol import retrieval_result


class CreateKnowledgeBase(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    model_data: dict[str, Any] = Field(alias="model")
    embedding_profile: dict[str, Any] | None = None


class ApplyModel(StrictModel):
    model_data: dict[str, Any] = Field(alias="model")


class Reindex(StrictModel):
    embedding_profile: dict[str, Any]


def metadata_result(value):
    """从管理结果移除内部文件路径与原始存储字段。"""
    if isinstance(value, list):
        return [metadata_result(item) for item in value]
    hidden = {
        "raw_path",
        "plan_path",
        "model_json",
        "embedding_profile_json",
        "source_json",
        "request_json",
        "input_json",
        "error_json",
        "counts_json",
    }
    return {key: item for key, item in value.items() if key not in hidden}


class LockedContentResponse(StreamingResponse):
    """接管内容流和已进入的读锁，响应结束或中断时一并释放。"""

    def __init__(self, stream, lock, **kwargs):
        self.stream, self.lock = stream, lock
        super().__init__(self.iter_bytes(), **kwargs)

    async def iter_bytes(self):
        while block := await self.stream.read(1024 * 1024):
            yield block

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self.stream.close()
            finally:
                await self.lock.__aexit__(None, None, None)


async def content_response(context, kb_id, document_id):
    """打开受读门禁保护的下载流，并将释放责任转交给响应对象。"""
    kb_id, document_id = valid_id(kb_id), valid_id(document_id)
    lock = context.gate.read(kb_id)
    await lock.__aenter__()
    stream = None
    try:
        content = await context.management.content(kb_id, document_id)
        stream = await context.files.open_content(content["raw_path"], content["byte_length"])
        size = content["byte_length"]
        media_type = {
            "txt": "text/plain; charset=utf-8",
            "md": "text/markdown; charset=utf-8",
            "csv": "text/csv; charset=utf-8",
            "json": "application/json",
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }.get(content["media_type"], "application/octet-stream")
        return LockedContentResponse(
            stream,
            lock,
            media_type=media_type,
            headers={
                "Content-Length": str(size),
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(content["original_name"], safe=""),
                "Cache-Control": "no-store",
            },
        )
    except BaseException:
        # 返回响应之前失败，资源所有权仍在此函数，取消也必须走清理。
        try:
            if stream is not None:
                await stream.close()
        finally:
            await lock.__aexit__(None, None, None)
        raise


def create_router(context):
    """将管理与检索服务绑定到 HTTP 路由，共用检索结果契约。"""
    router = APIRouter(prefix="/api/v1")
    management, retrieval = context.management, context.retrieval
    result_options = {"response_model": RetrievalResult}

    async def core(operation, request):
        result = await retrieval_result(operation)
        request.state.request_id = result.request_id
        return result

    @router.post("/kbs", status_code=201)
    async def create_kb(body: CreateKnowledgeBase):
        return metadata_result(await management.create_kb(body.name, body.model_data, body.embedding_profile))

    @router.get("/kbs")
    async def list_kbs(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return metadata_result(await management.list_kbs(limit, offset))

    @router.post("/kbs/{kb_id}/model", status_code=202)
    async def apply_model(
        kb_id: str,
        body: ApplyModel,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ):
        return metadata_result(await management.change_model(kb_id, body.model_data, idempotency_key))

    @router.post("/kbs/{kb_id}/documents", status_code=202)
    async def upload(
        kb_id: str,
        file: UploadFile = File(),
        source: str = Form("{}"),
        mapping_id: str | None = Form(None),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ):
        try:
            source_data = json.loads(source)
        except ValueError as exc:
            raise KGError("INVALID_ARGUMENT", "source must be a JSON object") from exc
        if not isinstance(source_data, dict):
            raise KGError("INVALID_ARGUMENT", "source must be a JSON object")
        try:
            return await management.upload(kb_id, file, source_data, mapping_id, idempotency_key)
        finally:
            await file.close()

    @router.get("/kbs/{kb_id}/documents")
    async def list_documents(
        kb_id: str,
        active: bool | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        return metadata_result(await management.list_documents(kb_id, active, limit, offset))

    @router.get("/kbs/{kb_id}/documents/{document_id}")
    async def document(kb_id: str, document_id: str):
        value = metadata_result(await management.document(kb_id, document_id))
        jobs = await management.document_jobs(kb_id, document_id, 101, 0)
        value["jobs"] = metadata_result(jobs[:100])
        value["jobs_page"] = {"limit": 100, "offset": 0, "has_more": len(jobs) > 100}
        return value

    @router.get("/kbs/{kb_id}/documents/{document_id}/jobs")
    async def document_jobs(
        kb_id: str, document_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)
    ):
        return metadata_result(await management.document_jobs(kb_id, document_id, limit, offset))

    @router.get("/kbs/{kb_id}/documents/{document_id}/content")
    async def content(kb_id: str, document_id: str):
        return await content_response(context, kb_id, document_id)

    @router.delete("/kbs/{kb_id}/documents/{document_id}", status_code=202)
    async def delete_document(
        kb_id: str,
        document_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ):
        return metadata_result(await management.delete_document(kb_id, document_id, idempotency_key))

    @router.post("/kbs/{kb_id}/reindex", status_code=202)
    async def reindex(
        kb_id: str,
        body: Reindex,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ):
        return metadata_result(await management.reindex(kb_id, body.embedding_profile, idempotency_key))

    @router.get("/jobs/{job_id}")
    async def job(job_id: str):
        return metadata_result(await management.job(job_id))

    @router.get("/jobs/{job_id}/issues")
    async def issues(job_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        return await management.issues(job_id, limit, offset)

    @router.post("/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, generation: int = Query(ge=0)):
        return metadata_result(await management.retry(job_id, generation))

    @router.get("/kbs/{kb_id}/model", **result_options)
    async def describe_model(
        kb_id: str,
        request: Request,
        entity_types: list[str] = Query(default=[]),
        domain_ids: list[str] = Query(default=[]),
    ):
        return await core(retrieval.describe_model(kb_id, entity_types, domain_ids), request)

    @router.post("/kbs/{kb_id}/knowledge/search", **result_options)
    async def search_knowledge(kb_id: str, request: Request, body: SearchRequest):
        return await core(retrieval.search_knowledge(kb_id, body), request)

    @router.get("/kbs/{kb_id}/knowledge/{kind}/{object_id}", **result_options)
    async def get_knowledge(
        kb_id: str,
        kind: Literal["entity", "relation", "chunk"],
        object_id: str,
        request: Request,
        evidence_limit: int = Query(100, ge=0, le=1000),
    ):
        return await core(retrieval.get_knowledge(kb_id, kind, object_id, evidence_limit), request)

    @router.post("/kbs/{kb_id}/graph/query", **result_options)
    async def query_graph(kb_id: str, request: Request, body: GraphRequest):
        return await core(retrieval.query_graph(kb_id, body), request)

    @router.post("/kbs/{kb_id}/retrieve", **result_options)
    async def retrieve(kb_id: str, request: Request, body: RetrieveRequest = Body()):
        return await core(retrieval.retrieve(kb_id, body), request)

    @router.get("/health/live")
    async def live():
        return {"status": "live"}

    @router.get("/health/ready")
    async def ready():
        return await context.ready()

    return router
