from __future__ import annotations

from .common import new_id


class KGError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, *, retryable: bool = False, **details):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.retryable, self.details = retryable, details

    def as_dict(self, request_id: str | None = None):
        return {
            "request_id": request_id or new_id(),
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
