"""按 Word 正文顺序读取段落和常规表格，保留原文位置。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError

from ...domain import KGError
from ...domain.source import SourceSpan
from .sections import chapter_depth


@dataclass
class WordBlock:
    text: str
    spans: list[SourceSpan]
    locator: dict
    section_path: list[str]
    overlap: bool


def _heading_level(paragraph: Paragraph) -> int | None:
    outline = paragraph._p.xpath("./w:pPr/w:outlineLvl/@w:val")
    if outline:
        level = int(outline[0])
        return level + 1 if 0 <= level < 9 else None
    style = paragraph.style
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        outline = style.element.xpath("./w:pPr/w:outlineLvl/@w:val")
        if outline:
            level = int(outline[0])
            return level + 1 if 0 <= level < 9 else None
        heading = re.fullmatch(r"Heading([1-9])", style.style_id or "")
        if heading:
            return int(heading.group(1))
        style = style.base_style
    return None


def _word_block(parts, locator, path, *, separator, overlap):
    texts, spans = [], []
    offset = 0
    for index, (text, origin) in enumerate(parts):
        if index:
            texts.append(separator)
            spans.append(
                {
                    "chunk_range": [offset, offset + len(separator)],
                    "origin": {},
                    "role": "synthetic",
                    "transform": "format",
                }
            )
            offset += len(separator)
        if text:
            texts.append(text)
            spans.append(
                {
                    "chunk_range": [offset, offset + len(text)],
                    "origin": origin | {"value_range": [0, len(text)]},
                    "role": "source",
                    "transform": "identity",
                }
            )
            offset += len(text)
    return WordBlock("".join(texts), spans, locator, [name for _, name in path], overlap)


def word_blocks(filename: Path):
    """标题确定章节，正文与表格按实际出现顺序输出。"""
    try:
        with filename.open("rb") as stream:
            document = Document(stream)
    except (BadZipFile, PackageNotFoundError, XMLSyntaxError, KeyError) as exc:
        raise KGError("PARSE_FAILED", "Word document could not be parsed") from exc

    items = [
        (item, _heading_level(item) if isinstance(item, Paragraph) and item.text.strip() else None)
        for item in document.iter_inner_content()
    ]
    depth = chapter_depth([level for _, level in items if level is not None])
    locator = {"format": "docx"}
    paragraphs, path = [], []
    paragraph_index = table_index = 0
    for item, level in items:
        if isinstance(item, Paragraph):
            paragraph_index += 1
            body = item.text
            if not body.strip():
                continue
            if level is not None and level <= depth:
                if paragraphs:
                    yield _word_block(paragraphs, locator, path, separator="\n\n", overlap=True)
                    paragraphs = []
                while path and path[-1][0] >= level:
                    path.pop()
                path.append((level, body.strip()))
            paragraphs.append((body, locator | {"paragraph": paragraph_index}))
            continue

        if paragraphs:
            yield _word_block(paragraphs, locator, path, separator="\n\n", overlap=True)
            paragraphs = []
        table_index += 1
        for row_index, row in enumerate(item.rows, 1):
            row_locator = locator | {"table": table_index, "row": row_index}
            cells = [
                (cell.text, row_locator | {"column": column})
                for column, cell in enumerate(row.cells, row.grid_cols_before + 1)
            ]
            if any(text.strip() for text, _ in cells):
                yield _word_block(cells, row_locator, path, separator="\t", overlap=False)
    if paragraphs:
        yield _word_block(paragraphs, locator, path, separator="\n\n", overlap=True)
