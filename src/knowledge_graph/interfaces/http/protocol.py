from __future__ import annotations

import logging

from pydantic import ValidationError

from ...domain import new_id
from ...domain.errors import KGError
from ...services.contracts import ErrorResult, RetrievalResult

log = logging.getLogger(__name__)


async def retrieval_result(operation):
    """两个协议共用完整结果校验，保留知识对象的完整字段。"""
    value = await operation
    try:
        return RetrievalResult.model_validate(value)
    except ValidationError as exc:
        log.error("retrieval_result_invalid error_type=%s", type(exc).__name__)
        raise KGError("INTERNAL_ERROR", "The service could not produce a valid result", 500) from exc


def error_result(error, request_id=None):
    """将领域或协议异常映射为稳定错误结构，不暴露输入与内部异常详情。"""
    if isinstance(error, KGError):
        return ErrorResult.model_validate(error.as_dict(request_id))
    if isinstance(error, ValidationError):
        return ErrorResult(
            request_id=request_id or new_id(),
            code="INVALID_ARGUMENT",
            message="Request parameters do not match the interface contract",
            retryable=False,
            details={
                "violations": [
                    {"location": list(item["loc"]), "type": item["type"]}
                    for item in error.errors(include_input=False, include_url=False)
                ]
            },
        )
    if isinstance(error, TimeoutError):
        return ErrorResult.model_validate(
            KGError("TIMEOUT", "The request exceeded its time budget", 504, retryable=True).as_dict(
                request_id
            )
        )
    log.error("request_failed request_id=%s error_type=%s", request_id, type(error).__name__)
    return ErrorResult(
        request_id=request_id or new_id(),
        code="INTERNAL_ERROR",
        message="The service could not complete the request",
        retryable=False,
        details={},
    )
