"""组装 HTTP 与 MCP 入口，共用服务实例、错误协议和应用生命周期。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .bootstrap import Context
from .domain import new_id
from .domain.errors import KGError
from .interfaces.http import create_router
from .interfaces.http.limits import UPLOAD_LIMIT_SCOPE_KEY, UploadBodyLimit, upload_body_error
from .interfaces.http.protocol import error_result
from .interfaces.mcp import create_mcp

log = logging.getLogger(__name__)


def create_app(context=None):
    # 外部注入的 Context 由调用方管理生命周期。
    """创建 ASGI 应用；仅在内部创建 Context 时负责关闭其资源。"""
    owned = context is None
    context = context or Context()
    mcp = create_mcp(context.retrieval)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        try:
            # 进程锁与 MCP 会话管理器覆盖整个应用运行期。
            async with context.gate.server():
                async with mcp.session_manager.run():
                    yield
        finally:
            if owned:
                await context.close()

    app = FastAPI(title="Configurable knowledge graph", version="0.1.0", lifespan=lifespan)
    app.state.context, app.state.mcp = context, mcp
    app.add_middleware(UploadBodyLimit, file_limit=context.settings.upload_limit)

    @app.middleware("http")
    async def request_identity(request, call_next):
        request.state.request_id = new_id()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(KGError)
    async def domain_error(request: Request, exc: KGError):
        value = error_result(exc, getattr(request.state, "request_id", None))
        headers = {"Retry-After": "1"} if exc.code == "KB_MAINTENANCE" else {}
        return JSONResponse(value.model_dump(mode="json"), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # 仅返回校验位置与类型，避免回传请求中的敏感原始值。
        details = {
            "violations": [{"location": list(item["loc"]), "type": item["type"]} for item in exc.errors()]
        }
        value = error_result(
            KGError("INVALID_ARGUMENT", "Request parameters do not match the interface contract", **details),
            getattr(request.state, "request_id", None),
        )
        return JSONResponse(value.model_dump(mode="json"), status_code=422)

    @app.exception_handler(HTTPException)
    async def protocol_error(request: Request, exc: HTTPException):
        if UPLOAD_LIMIT_SCOPE_KEY in request.scope:
            return await domain_error(request, upload_body_error(request.scope[UPLOAD_LIMIT_SCOPE_KEY]))
        code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_ARGUMENT"
        message = (
            "HTTP resource does not exist" if exc.status_code == 404 else "HTTP request is not supported"
        )
        value = error_result(
            KGError(code, message, exc.status_code), getattr(request.state, "request_id", None)
        )
        return JSONResponse(value.model_dump(mode="json"), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        value = error_result(exc, getattr(request.state, "request_id", None))
        status = 504 if value.code == "TIMEOUT" else 500
        return JSONResponse(value.model_dump(mode="json"), status_code=status)

    mcp_app.add_exception_handler(HTTPException, protocol_error)
    app.include_router(create_router(context))
    # 子应用自身提供 /mcp，因此挂到根路径；会话管理器仅由父生命周期运行一次。
    app.mount("/", mcp_app)
    return app
