"""按原文边界规划抽取窗口。"""

from __future__ import annotations

import re
from collections import defaultdict

from ...domain import KGError
from .source_references import units_by_chunk

_PARAGRAPH_END = re.compile(r"(?>\r\n|\n|\r)[ \t]*(?>\r\n|\n|\r)+")
# 压平布局后，句界、编号和短标题一起出现才推断行内标题。
_INLINE_START = r"(?:^[ \t]*|(?<=[.!?。！？])[ \t]+)"
_NUMBERED_HEADING = re.compile(
    _INLINE_START + r"(?P<heading>[1-9]\d{0,2}(?:\.\d{1,3})*\.[ \t]+"
    r"[A-Z][A-Z0-9]*(?:[ \t/&,;–-]+[A-Z][A-Z0-9]*){0,12})"
    r"(?=[.:](?:\s|$)|[ \t]+(?:[a-z]\.|\([a-z]\))[ \t]|[ \t]*$)",
    re.M,
)
_TITLE_TEXT = r"[A-Z][A-Za-z0-9]+(?:[ \t/&,;–-]+(?:[A-Z][A-Za-z0-9]+|and|or|for|in|on|of|by|to|the)){0,12}"
_TITLE_ITEM = r"(?P<heading>(?:[a-z]\.|\([a-z]\))[ \t]+" + _TITLE_TEXT + r")(?=[.:](?:\s|$))"
_TITLED_ITEM = re.compile(_INLINE_START + _TITLE_ITEM, re.M)
_AFTER_NUMBERED_HEADING = re.compile(r"[ \t]+" + _TITLE_ITEM)
# 裸小节编号只有后面是独立的一行标题时才分块，避免误认小数开头的正文。
_STANDALONE_SUBHEADING = re.compile(
    r"^[ \t]*[1-9]\d{0,2}(?:\.\d{1,3})+[ \t]+" + _TITLE_TEXT + r":?[ \t]*\r?$",
    re.M,
)
_LINE_ITEM = re.compile(r"^[ \t]*(?:(?:\d+\.)+|\([a-z]\)|[a-z]\.)[ \t]+(?=\S)", re.M)


def source_block_ranges(
    text: str, *, inline_headings: bool, parser_boundaries: tuple[int, ...] = ()
) -> list[tuple[int, int]]:
    """返回连续且无损的来源块坐标。"""
    starts = {0, len(text), *parser_boundaries}
    starts.update(match.end() for match in _PARAGRAPH_END.finditer(text))
    for pattern in (_LINE_ITEM, _STANDALONE_SUBHEADING):
        starts.update(match.start() for match in pattern.finditer(text))
    if inline_headings:
        for pattern in (_NUMBERED_HEADING, _TITLED_ITEM):
            for match in pattern.finditer(text):
                start = match.start()
                if start and text[start - 1] not in "\r\n":
                    start = match.start("heading")
                starts.add(start)
                if pattern is _NUMBERED_HEADING:
                    child = _AFTER_NUMBERED_HEADING.match(text, match.end())
                    if child:
                        starts.add(child.start("heading"))
    points = sorted(starts)
    return list(zip(points, points[1:]))


def visible_source_chars(scope: dict) -> int:
    """统计按原文坐标去重后的窗口字符数。"""
    return sum(len(part["text"]) for view in scope["unit_views"] for part in view["visible_parts"])


def plan_source_windows(chunks, units, make_scope, fits_budget, *, target_source_chars: int) -> list[dict]:
    """按目标大小组窗，仅在硬预算不足时拆开来源块。"""
    positions = {key: index for index, key in enumerate(chunks)}
    unit_chunks = defaultdict(set)
    by_chunk = units_by_chunk(units, chunks)
    if by_chunk is None:
        for index, unit in enumerate(units):
            unit_chunks[index].update(ref["chunk_id"] for ref in unit["evidence"])
    else:
        for key, indexes in by_chunk.items():
            for index in indexes:
                unit_chunks[index].add(key)

    scopes, current = [], set()
    scheduled = set().union(*unit_chunks.values())

    def as_scope(keys, *, key=None):
        window = {item: chunks[item] for item in sorted(keys, key=positions.__getitem__)}
        indexes = sorted({i for item in window for i in by_chunk[item]}) if by_chunk is not None else None
        return make_scope(window, units if indexes is None else [units[i] for i in indexes], key=key)

    def flush():
        if current:
            scopes.append(as_scope(current, key=f"batch/{len(scopes)}"))

    def add(keys):
        nonlocal current
        combined = current | keys
        if combined == current:
            return
        scope = as_scope(combined)
        if (not current or visible_source_chars(scope) <= target_source_chars) and fits_budget(scope):
            current = combined
            return
        if current:
            flush()
            current = set()
            add(keys)
        elif len(keys) == 1:
            raise KGError("MODEL_INPUT_TOO_LARGE", "One source chunk exceeds the extraction request budget")
        else:
            for key in sorted(keys, key=positions.__getitem__):
                add({key})

    for keys in unit_chunks.values() if by_chunk is None else (unit_chunks[i] for i in range(len(units))):
        if keys:
            add(keys)
    for key in chunks:
        if key not in scheduled:
            add({key})
    flush()
    return scopes


def open_source_edges(parts, views, catalog) -> set[tuple[str, int]]:
    """只在选区接到未送入请求的同一来源块时标记续段。"""
    edges = set()
    for part in parts:
        key = part.get("unit_key")
        view = views.get(key)
        if not view or view["fully_supplied"] or "start" not in part or "end" not in part:
            continue
        start, end = part["start"], part["end"]
        for visible in view["visible_parts"]:
            left, right = visible["unit_range"]
            if not left <= start < end <= right:
                continue
            text = visible["text"]
            if left > 0 and not text[: start - left].strip():
                edges.add((key, left - 1))
            if right < len(catalog[key]["text"]) and not text[end - left :].strip():
                edges.add((key, right))
    return edges
