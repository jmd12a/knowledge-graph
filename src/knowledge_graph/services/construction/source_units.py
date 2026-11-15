"""由解析器的来源坐标生成可引用的原文单元。"""

from __future__ import annotations

import json
import re

from ...domain import KGError
from .source_references import origin_key

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


_CONTINUATION = re.compile(
    r"^(?:but\b|however\b|except\b|provided\b|unless\b|notwithstanding\b|subject\s+to\b|"
    r"for\s+the\s+avoidance\b|and\b|or\b|但|但是|然而|除外|除非|例外|前款|上述|并且|其中)",
    re.I,
)


def build_rule_context(parsed, *, structured=False):
    """把完整来源段落及其续段提供给抽取流程。"""
    if parsed.get("mapping_id"):
        return []
    sources = {}
    chunks = parsed["chunks"]
    for chunk in chunks:
        for span in chunk["source_spans"]:
            if span.get("role") != "source":
                continue
            origin = span["origin"]
            key = "value_range" if "value_range" in origin else "char_range"
            a, b = span["chunk_range"]
            start, end = origin[key]
            text = chunk["text"][a:b]
            if len(text) != end - start:
                raise KGError("PARSE_FAILED", "Source span has a non-reversible coordinate mapping")
            anchor = origin_key(origin)
            entry = sources.setdefault(
                anchor,
                {
                    "coordinate": key,
                    "pieces": [],
                    "boundaries": set(),
                    "spans": [],
                },
            )
            entry["pieces"].append((start, end, text))
            entry["spans"].append((chunk, span))
            native_section = origin["format"] == "md" or (
                origin["format"] == "docx" and "paragraph" in origin and chunk.get("section_path")
            )
            if structured and native_section and a == 0 and chunk.get("block_range", [None])[0] == 0:
                entry["boundaries"].add(start)
    units = []
    for anchor, source in sources.items():
        text, base, cursor = "", None, None
        for start, end, piece in sorted(source["pieces"]):
            if base is None:
                base, cursor = start, start
            if start > cursor:
                raise KGError("PARSE_FAILED", "Source context has an unmapped gap")
            overlap = min(cursor - start, len(piece))
            if overlap and text[start - base : start - base + overlap] != piece[:overlap]:
                raise KGError("PARSE_FAILED", "Overlapping source windows disagree")
            if end > cursor:
                text += piece[overlap:]
                cursor = end
        origin = json.loads(anchor) if structured else {}
        ranges = (
            source_block_ranges(
                text,
                inline_headings=origin["format"] in {"txt", "pdf"} or "paragraph" in origin,
                parser_boundaries=tuple(position - base for position in source["boundaries"]),
            )
            if structured
            else [match.span() for match in re.finditer(r"[\s\S]+?(?:\r?\n[ \t]*\r?\n+|$)", text)]
        )
        for left, right in ranges:
            body = text[left:right]
            region = {
                "origin": anchor,
                "coordinate": source["coordinate"],
                "range": [base + left, base + right],
            }
            if units and base + left not in source["boundaries"] and _CONTINUATION.match(body.lstrip()):
                units[-1]["regions"].append(region)
                units[-1]["text"] += body
            else:
                units.append(
                    {
                        "unit_key": "rule-unit/" + str(len(units)),
                        "order": len(units),
                        "regions": [region],
                        "text": body,
                    }
                )
    for unit in units:
        unit["evidence"] = _region_references(unit["regions"], sources)
    return units


def _region_references(regions, sources):
    references = []
    for region in regions:
        remaining = [region["range"][:]]
        # 索引保留解析器的切片顺序，同一坐标系内优先引用先出现的切片。
        for chunk, span in sources[region["origin"]]["spans"]:
            key = region["coordinate"]
            start, end = span["origin"][key]
            next_remaining = []
            for left, right in remaining:
                a, b = max(left, start), min(right, end)
                if a >= b:
                    next_remaining.append([left, right])
                    continue
                begin = span["chunk_range"][0] + a - start
                finish = begin + b - a
                references.append(
                    {
                        "chunk_id": chunk["chunk_id"],
                        "start": begin,
                        "end": finish,
                        "quote": chunk["text"][begin:finish],
                    }
                )
                if left < a:
                    next_remaining.append([left, a])
                if b < right:
                    next_remaining.append([b, right])
            remaining = next_remaining
            if not remaining:
                break
        if remaining:
            raise KGError("PARSE_FAILED", "A complete rule context cannot be mapped to chunks")
    return references
