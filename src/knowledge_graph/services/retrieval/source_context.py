"""从已校验的原文切片中补齐操作段和事实所在段落。"""

from __future__ import annotations

import re
from collections import defaultdict

from ...domain.source import source_evidence
from ..construction.source_references import origin_key

_SECTION = re.compile(r"^(?:[ \t]*#{1,6}[ \t]+\S[^\r\n]*|[A-Z][A-Z0-9 \t()&/-]{2,})[ \t]*\r?$", re.M)
_STEP = re.compile(r"^(?P<indent>[ \t]*)(?P<number>[1-9]\d{0,2})(?P<marker>[.)])[ \t]+(?=\S)", re.M)
_BREAK = re.compile(r"\r?\n(?:[ \t]*\r?\n)+")
_INDENT = re.compile(r"[ \t]*")
_EXAMPLE = re.compile(r"(?:example|for example|示例|例如|例子)[ \t]*[:：]", re.I)
_MAX_CONTEXT_CHARS = 1200
_MAX_PARAGRAPH_CHARS = 2000
_TOTAL_CONTEXT_CHARS = 2400


def _ordered_ranges(text: str, left: int, right: int, complete_right: bool) -> list[tuple[int, int]]:
    items = list(_STEP.finditer(text, left, right))
    starts = [i for i, item in enumerate(items) if item["number"] == "1"]
    ranges = []
    for first, stop in zip(starts, starts[1:] + [len(items)]):
        group = items[first:stop]
        opener = group[0]
        if len(group) < 2 or any(
            int(item["number"]) != i
            or item["indent"] != opener["indent"]
            or item["marker"] != opener["marker"]
            for i, item in enumerate(group, 1)
        ):
            continue
        boundary = items[stop].start() if stop < len(items) else right
        if boundary == right and not complete_right:
            continue
        last = group[-1]
        content_column = len(last[0].expandtabs(4))
        end = boundary
        for blank in _BREAK.finditer(text, last.end(), boundary):
            indentation = _INDENT.match(text, blank.end(), boundary)[0]
            if len(indentation.expandtabs(4)) < content_column:
                end = blank.start()
                break
        ranges.append((opener.start(), end))
    return ranges


def _paragraphs(text: str, left: int, right: int) -> list[tuple[int, int]]:
    breaks = list(_BREAK.finditer(text, left, right))
    return list(zip([left, *(m.end() for m in breaks)], [*(m.start() for m in breaks), right], strict=True))


def _example_ranges(
    text: str, paragraphs: list[tuple[int, int]], right: int, complete_right: bool
) -> list[tuple[int, int]]:
    return [
        (previous[0], following[1])
        for previous, label, following in zip(paragraphs, paragraphs[1:], paragraphs[2:])
        if _EXAMPLE.fullmatch(text[slice(*label)].strip())
        and text[slice(*previous)].strip()
        and text[slice(*following)].strip()
        and (following[1] < right or complete_right)
    ]


def _context_ranges(chunk: dict) -> list[tuple[int, int, bool]]:
    text = chunk["text"]
    spans = chunk["source_spans"]
    # 只使用可逆的连续原文，避免跨表格字段、合成分隔符或未看到的切片边界。
    if (
        len(spans) != 1
        or spans[0]["chunk_range"] != [0, len(text)]
        or spans[0]["role"] != "source"
        or spans[0]["transform"] != "identity"
        or "char_range" not in spans[0]["origin"]
        or "block_range" not in chunk
        or "block_length" not in chunk.get("parser_profile", {})
    ):
        return []
    start, end = chunk["block_range"]
    complete_right = end == chunk["parser_profile"]["block_length"]
    headings = list(_SECTION.finditer(text))
    starts = [0, *(heading.end() for heading in headings)]
    ends = [*(heading.start() for heading in headings), len(text)]
    structured, prose = [], []
    for left, right in zip(starts, ends, strict=True):
        section_end = right < len(text) or complete_right
        paragraphs = _paragraphs(text, left, right)
        structured.extend(_ordered_ranges(text, left, right, section_end))
        structured.extend(_example_ranges(text, paragraphs, right, section_end))
        for a, b in paragraphs:
            if b == right and not section_end:
                continue
            body = text[a:b].strip()
            if body and not (_STEP.match(body) or _EXAMPLE.fullmatch(body)):
                prose.append((a, b))

    trimmed = []
    for ranges, is_prose, maximum in (
        (structured, False, _MAX_CONTEXT_CHARS),
        (prose, True, _MAX_PARAGRAPH_CHARS),
    ):
        for left, right in ranges:
            if left == 0 and start > 0:
                continue
            part = text[left:right]
            left += len(part) - len(part.lstrip())
            right -= len(part) - len(part.rstrip())
            if 0 < right - left <= maximum:
                trimmed.append((left, right, is_prose))
    return trimmed


def source_context(evidence: list[dict], chunks: dict, entity_ids: set[str], slots: int) -> list[dict]:
    """在余量内附加独立的上下文引用，保留原有声明和证据下标。"""
    if slots <= 0 or not entity_ids:
        return []
    existing = defaultdict(list)
    for reference in evidence:
        for span in reference["source_spans"]:
            origin = span["origin"]
            if "char_range" in origin:
                existing[reference["document_id"], origin_key(origin)].append(origin["char_range"])
    ranges_by_chunk = {}
    wanted = {}
    for reference in evidence:
        owners = entity_ids.intersection(reference["fact_ids"])
        if reference.get("role") == "context" or not owners:
            continue
        key = reference["chunk_id"]
        if key not in ranges_by_chunk:
            ranges_by_chunk[key] = _context_ranges(chunks[key])
        a, b = reference["chunk_range"]
        for start, end, is_prose in ranges_by_chunk[key]:
            if not start <= a < b <= end:
                continue
            chunk = chunks[key]
            origin = chunk["source_spans"][0]["origin"]
            source = (chunk["document_id"], origin_key(origin))
            offset = origin["char_range"][0]
            left, right = start + offset, end + offset
            if not any(a <= left and right <= b for a, b in existing[source]):
                signature = (*source, left, right)
                previous = wanted.get(signature)
                if previous is None or (previous[0] and not is_prose):
                    wanted[signature] = (is_prose, key, start, end, owners)
                    if previous is not None:
                        owners.update(previous[-1])
                else:
                    previous[-1].update(owners)
            break

    result, remaining = [], _TOTAL_CONTEXT_CHARS
    for _, key, start, end, owners in sorted(wanted.values(), key=lambda item: item[0]):
        if len(result) >= slots:
            break
        length = end - start
        if length > remaining:
            continue
        chunk = chunks[key]
        result.append(
            source_evidence(chunk, start, end, role="context")
            | {
                "text": chunk["text"][start:end],
                "fact_ids": sorted(owners),
                "contribution_ids": [],
                "field_paths": [],
            }
        )
        remaining -= length
    return result
