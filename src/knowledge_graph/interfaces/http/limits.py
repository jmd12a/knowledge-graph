from __future__ import annotations

from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse

from ...domain import KGError
from .protocol import error_result

UPLOAD_LIMIT_SCOPE_KEY = "knowledge_graph.upload_limit_exceeded"


def upload_body_error(max_bytes):
    return KGError(
        "UPLOAD_TOO_LARGE",
        "Upload request exceeds the file and multipart envelope budget",
        413,
        max_request_bytes=max_bytes,
    )


class UploadBodyLimit:
    """在 multipart 解析期间限制总请求体，覆盖缺失或不实的长度头。"""

    def __init__(self, app, file_limit):
        self.app = app
        # 文件预算之外留出 multipart 头与表单元数据的封装空间。
        self.max_bytes = file_limit + 64 * 1024

    async def __call__(self, scope, receive, send):
        parts = scope.get("path", "").rstrip("/").rsplit("/", 3)
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or len(parts) != 4
            or parts[-3] != "kbs"
            or parts[-1] != "documents"
        ):
            await self.app(scope, receive, send)
            return
        lengths = [value for name, value in scope.get("headers", []) if name.lower() == b"content-length"]
        if any(value.isdigit() and int(value) > self.max_bytes for value in lengths):
            result = error_result(upload_body_error(self.max_bytes), scope.get("state", {}).get("request_id"))
            await JSONResponse(result.model_dump(mode="json"), status_code=413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    scope[UPLOAD_LIMIT_SCOPE_KEY] = self.max_bytes
                    # Starlette 仅在此异常路径关闭已暂存的 multipart 文件。
                    raise MultiPartException("Upload request body exceeds its configured budget")
            return message

        await self.app(scope, bounded_receive, send)
