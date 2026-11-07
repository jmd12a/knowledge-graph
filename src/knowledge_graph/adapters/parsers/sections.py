"""从明确的标题结构确定章节边界，供文本切片共用。"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt


def count_line_breaks(text: str, start: int, end: int) -> int:
    """统计在字符区间内结束的换行，CRLF 只在 LF 处计一次。"""
    return (
        text.count("\n", start, end)
        + text.count("\r", start, end)
        - text.count("\r\n", start, end + 1)
    )


def chapter_depth(levels: list[int]) -> int:
    """用最高级标题划章，单一文档标题下可用下一层作为章界。"""
    top = min(levels, default=0)
    if levels and levels[0] == top and levels.count(top) == 1:
        return min((level for level in levels if level > top), default=top)
    return top


def markdown_sections(text: str):
    """返回原文起止位置、起始行偏移与章节路径，保留原文换行。"""
    tokens = MarkdownIt().parse(text)
    headings = [
        (token.map[0], int(token.tag[1:]), tokens[index + 1].content.strip())
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0 and token.map
    ]
    if not headings:
        yield 0, len(text), 0, []
        return

    line_starts = [0] + [match.end() for match in re.finditer(r"\r\n?|\n", text)]
    depth = chapter_depth([level for _, level, _ in headings])
    start, start_line = 0, 0
    path = []
    for line, level, title in headings:
        if level > depth:
            continue
        offset = line_starts[line]
        if offset > start:
            yield start, offset, start_line, [name for _, name in path]
        while path and path[-1][0] >= level:
            path.pop()
        path.append((level, title))
        start, start_line = offset, line
    if start < len(text):
        yield start, len(text), start_line, [name for _, name in path]
