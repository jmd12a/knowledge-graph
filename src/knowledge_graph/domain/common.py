from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4


def new_id() -> str:
    return str(uuid4())


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    """生成保留完整值的稳定 JSON 表示，供直接比较、持久化和身份键共用。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def valid_id(value: str) -> str:
    from .errors import KGError

    try:
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError("UUID must use canonical notation")
        return str(parsed)
    except (ValueError, TypeError, AttributeError) as exc:
        raise KGError("INVALID_ARGUMENT", "Expected a canonical UUID", 422) from exc
