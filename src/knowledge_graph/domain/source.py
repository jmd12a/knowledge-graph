from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal
from typing_extensions import NotRequired, TypedDict


class SourceSpan(TypedDict):
    chunk_range: list[int]
    origin: dict[str, Any]
    role: Literal["source", "synthetic"]
    transform: str


class Chunk(TypedDict):
    chunk_id: str
    document_id: str
    text: str
    locator: dict[str, Any]
    source_spans: list[SourceSpan]
    section_path: list[str]
    parser_profile: dict[str, Any]
    domain_ids: list[str]


class Evidence(TypedDict):
    document_id: str
    chunk_id: str
    chunk_range: list[int]
    role: NotRequired[Literal["fact", "context"]]
    quote: NotRequired[str]
    quote_truncated: NotRequired[bool]
    quote_range: NotRequired[list[int]]
    source_mapping_verified: NotRequired[bool]
    locator: dict[str, Any]
    source_spans: list[SourceSpan]


class Contribution(TypedDict):
    contribution_id: str
    kb_id: str
    document_id: str
    chunk_id: str
    target_kind: Literal["entity", "relation"]
    target_id: str
    field_path: str
    claim: dict[str, Any]
    value: Any
    evidence: list[Evidence]
    job_id: str


class ParsedDocument(TypedDict):
    chunks: list[Chunk]
    records: list[dict[str, Any]]
    mapping_id: str | None
    sections: NotRequired[list[dict[str, Any]]]


class VectorPoint(TypedDict):
    id: str
    payload: dict[str, Any]
    vector: list[float]


class ScanPage(TypedDict):
    items: list[dict[str, Any]]
    next_offset: str | int | None
    complete: bool
    scanned: int


QUOTE_LIMIT = 500


def _character_range(value, maximum):
    from .errors import KGError

    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or not 0 <= value[0] < value[1] <= maximum
    ):
        raise KGError("EVIDENCE_INVALID", "Evidence requires a valid Unicode character range")
    return value[0], value[1]


def source_evidence(
    chunk: dict, start: int, end: int, *, role: str = "fact", quote_range: list[int] | None = None
) -> Evidence:
    """Build one canonical stored citation after checking its complete source mapping."""
    from .errors import KGError

    try:
        text = chunk["text"]
        if not isinstance(text, str):
            raise KGError("EVIDENCE_INVALID", "Evidence source text is unavailable")
        start, end = _character_range([start, end], len(text))
        if role not in {"legacy", "fact", "context"}:
            raise KGError("EVIDENCE_INVALID", "Unknown evidence role")
        # 完整证据区间必须逐段映射到原文；展示用短引文可以只覆盖其中一段。
        segments, covered = [], start
        for span in sorted(chunk["source_spans"], key=lambda value: value["chunk_range"]):
            left, right = _character_range(span["chunk_range"], len(text))
            a, b = max(start, left), min(end, right)
            if a >= b:
                continue
            if a > covered or span.get("role") != "source":
                raise KGError("EVIDENCE_SYNTHETIC", "Evidence must map entirely to source content")
            origin = deepcopy(span.get("origin", {}))
            coordinate = "value_range" if "value_range" in origin else "char_range"
            source_range = origin.get(coordinate)
            if (
                not isinstance(source_range, list)
                or len(source_range) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in source_range)
                or source_range[0] < 0
                or source_range[1] - source_range[0] != right - left
            ):
                raise KGError("EVIDENCE_INVALID", "Evidence lacks a reversible source coordinate mapping")
            origin[coordinate] = [source_range[0] + a - left, source_range[0] + b - left]
            segments.append(
                {"chunk_range": [a, b], "origin": origin, "role": "source", "transform": span["transform"]}
            )
            covered = max(covered, b)
        if covered != end:
            raise KGError("EVIDENCE_INVALID", "Evidence does not map to the complete source range")
        result = {
            "document_id": chunk["document_id"],
            "chunk_id": chunk["chunk_id"],
            "chunk_range": [start, end],
            "locator": deepcopy(chunk["locator"]),
            "source_spans": segments,
        }

        # 上下文保存完整来源映射；事实证据另保存有长度上限的精确引文。
        if role == "context":
            return result | {"role": "context", "source_mapping_verified": True}
        if role == "legacy":
            return result | {
                "quote": text[start : min(end, start + QUOTE_LIMIT)],
                "quote_truncated": end - start > QUOTE_LIMIT,
            }
        a, b = _character_range(
            quote_range if quote_range is not None else [start, min(end, start + QUOTE_LIMIT)], len(text)
        )
        if not start <= a < b <= end or b - a > QUOTE_LIMIT:
            raise KGError(
                "EVIDENCE_INVALID", "Fact quote must be a bounded excerpt inside its evidence range"
            )
        return result | {
            "role": "fact",
            "quote": text[a:b],
            "quote_range": [a, b],
            "quote_truncated": [a, b] != [start, end],
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise KGError("EVIDENCE_INVALID", "Evidence source mapping has an invalid structure") from exc


def validate_stored_evidence(evidence: dict, chunk: dict) -> str:
    """Check legacy or role-tagged storage without rewriting it; return its full source text."""
    from .errors import KGError

    if not isinstance(evidence, dict):
        raise KGError("EVIDENCE_INVALID", "Stored evidence must be an object")
    if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str):
        raise KGError("EVIDENCE_INVALID", "Stored evidence source text is unavailable")
    role = evidence.get("role", "legacy")
    if "role" in evidence and role not in {"fact", "context"}:
        raise KGError("EVIDENCE_INVALID", "Unknown stored evidence role")
    if role == "context" and evidence.get("source_mapping_verified") is not True:
        raise KGError("EVIDENCE_INVALID", "Context evidence requires a verified complete source mapping")
    if role != "context" and not isinstance(evidence.get("quote_truncated"), bool):
        raise KGError("EVIDENCE_INVALID", "Fact excerpt truncation must be an explicit boolean")
    start, end = _character_range(evidence.get("chunk_range"), len(chunk.get("text", "")))
    if role == "fact" and "quote_range" not in evidence:
        raise KGError("EVIDENCE_INVALID", "Fact evidence requires an explicit quote range")
    expected = source_evidence(
        chunk, start, end, role=role, quote_range=evidence.get("quote_range") if role == "fact" else None
    )
    if evidence != expected:
        raise KGError(
            "EVIDENCE_INVALID", "Stored evidence differs from its source range, excerpt, or mapping"
        )
    return chunk["text"][start:end]
