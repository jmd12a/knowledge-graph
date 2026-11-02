"""解析文本、PDF 和结构化记录，切分内容并保留精确来源坐标。"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from pypdf import PdfReader, __version__ as pypdf_version
from pypdf.errors import PyPdfError

from ...domain import KGError, canonical, new_id, select_mapping

PARSER_PROFILE = {"name": "source-spans", "revision": 1, "coordinates": "unicode_code_points_half_open"}


def pointer(value, path):
    """沿 JSON Pointer 读取值，将缺失路径或非法数组索引转为映射错误。"""
    if path == "":
        return value
    if not path.startswith("/"):
        raise KGError("INVALID_MODEL", "JSON pointer must be empty or begin with /")
    current = value
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    raise ValueError("Invalid array index")
                current = current[int(part)]
            else:
                current = current[part]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise KGError("MAPPING_FIELD_MISSING", "JSON pointer is missing", pointer=path) from exc
    return current


class Parser:
    def __init__(self, settings):
        self.settings = settings

    def _chunk_text(
        self, text, origin, document_id, domain_ids, *, overlap=True, spans=None, section_path=None
    ):
        """按长度与边界切片，将原文或字段坐标映射到切片内的半开区间。"""
        chunks, start = [], 0
        previous_start, prefix_newlines = 0, 0
        max_length = self.settings.chunk_max
        while start < len(text):
            end = min(start + max_length, len(text))
            if end < len(text):
                boundary = max(
                    text.rfind("\n\n", start + self.settings.chunk_target // 2, end),
                    text.rfind("\n", start + self.settings.chunk_target // 2, end),
                )
                if boundary > start:
                    end = boundary + 1
            body = text[start:end]
            mapped = []
            # 普通原文直接映射坐标；结构化记录按字段来源片段裁剪。
            if spans is None:
                segment_origin = dict(origin)
                segment_origin["char_range"] = [start, end]
                if origin["format"] in {"txt", "md"}:
                    prefix_newlines += text.count("\n", previous_start, start)
                    previous_start = start
                    segment_origin["line_range"] = [
                        prefix_newlines + 1,
                        prefix_newlines + text.count("\n", start, end) + 1,
                    ]
                mapped = [
                    {
                        "chunk_range": [0, len(body)],
                        "origin": segment_origin,
                        "role": "source",
                        "transform": "identity",
                    }
                ]
            else:
                for span in spans:
                    left, right = span["chunk_range"]
                    a, b = max(start, left), min(end, right)
                    if a >= b:
                        continue
                    item = dict(span)
                    item["chunk_range"] = [a - start, b - start]
                    item["origin"] = dict(span.get("origin", {}))
                    if span["role"] == "source":
                        value_range = span["origin"].get("value_range", [0, right - left])
                        item["origin"]["value_range"] = [value_range[0] + a - left, value_range[0] + b - left]
                    mapped.append(item)

            headings = section_path or []
            if origin["format"] == "md":
                headings = [m.group(2).strip() for m in re.finditer(r"(?m)^(#{1,6})\s+(.+)$", text[:end])][
                    -6:
                ]
            chunks.append(
                {
                    "chunk_id": new_id(),
                    "document_id": document_id,
                    "text": body,
                    "locator": origin,
                    "source_spans": mapped,
                    "section_path": headings,
                    "parser_profile": PARSER_PROFILE
                    | {
                        "format": origin["format"],
                        **({"pypdf_version": pypdf_version} if origin["format"] == "pdf" else {}),
                    },
                    "domain_ids": domain_ids,
                }
            )

            if end == len(text):
                break
            next_start = (
                end - min(self.settings.chunk_overlap, max(0, (end - start) // 4)) if overlap else end
            )
            start = max(start + 1, next_start)
        return chunks

    def parse(self, doc, model):
        """解析已登记原文，并将编码、格式和文件访问异常转换为领域错误。"""
        try:
            return self._parse(doc, model)
        except (UnicodeError, LookupError) as exc:
            raise KGError(
                "PARSE_ENCODING_ERROR", "Document cannot be decoded with its declared encoding"
            ) from exc
        except (csv.Error, PyPdfError, ValueError) as exc:
            raise KGError("PARSE_FAILED", "Document syntax or PDF content could not be parsed") from exc
        except OSError as exc:
            raise KGError("RAW_FILE_UNAVAILABLE", "Registered raw document could not be read", 503) from exc

    def _parse(self, doc, model):
        """按资料格式生成切片及结构化字段证据，区分原文和合成格式内容。"""
        filename, media_type = Path(doc["raw_path"]), doc["media_type"]
        domains = doc["source"].get("domain_ids", [])
        mapping_id, mapping = select_mapping(model, media_type, doc.get("mapping_id"))
        if media_type in {"txt", "md"}:
            text = filename.read_bytes().decode(doc["source"].get("encoding", "utf-8"))
            if not text.strip():
                raise KGError("EMPTY_DOCUMENT", "Document contains no text")
            return {
                "chunks": self._chunk_text(text, {"format": media_type}, doc["document_id"], domains),
                "records": [],
                "mapping_id": None,
            }

        if media_type == "pdf":
            chunks = []
            reader = PdfReader(filename)
            if reader.is_encrypted:
                raise KGError("UNSUPPORTED_PDF", "Encrypted PDF is not supported")
            for page_index, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if not text.strip():
                    raise KGError(
                        "PDF_TEXT_UNAVAILABLE",
                        "PDF page has no extractable text; OCR is required",
                        page=page_index,
                    )
                if text.count("\ufffd") > max(5, len(text) // 100):
                    raise KGError(
                        "PDF_TEXT_INVALID",
                        "PDF extraction contains excessive replacement characters",
                        page=page_index,
                    )
                chunks += self._chunk_text(
                    text, {"format": "pdf", "page": page_index}, doc["document_id"], domains
                )
            return {"chunks": chunks, "records": [], "mapping_id": None}

        encoding = mapping.get("records", {}).get("encoding", "utf-8")
        decoded = filename.read_bytes().decode(encoding)
        if media_type == "csv":
            if mapping.get("records", {}).get("header", True) is not True:
                raise KGError("INVALID_MODEL", "CSV requires a header row")
            reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise KGError("PARSE_FAILED", "CSV has no header or duplicate column names")
            records = []
            for row_index, record in enumerate(reader, 1):
                if None in record or any(value is None for value in record.values()):
                    raise KGError("PARSE_FAILED", "CSV row width does not match header", row=row_index)
                records.append((row_index, record, None))
        else:
            raw = json.loads(decoded)
            record_pointer = mapping.get("records", {}).get("pointer", "")
            selected = pointer(raw, record_pointer)
            if not isinstance(selected, list):
                raise KGError("PARSE_FAILED", "JSON record pointer must select an array")
            records = [
                (index, record, record_pointer + "/" + str(index)) for index, record in enumerate(selected)
            ]

        all_chunks, output_records = [], []
        for index, record, prefix in records:
            spans, text, fields = [], "", {}
            for entity in list(mapping["entities"].values()) + mapping.get("relations", []):
                for field in entity.get("fields", {}).values():
                    key = field.get("column") if media_type == "csv" else field.get("pointer", "")
                    if key in fields:
                        continue
                    try:
                        value = record[key] if media_type == "csv" else pointer(record, key)
                    except (KeyError, KGError):
                        fields[key] = {"missing": True, "value": None}
                        continue
                    rendered = value if isinstance(value, str) else canonical(value)
                    # 字段标签与换行只用于展示，不得成为原文证据。
                    label = str(key) + ": "
                    spans.append(
                        {
                            "chunk_range": [len(text), len(text) + len(label)],
                            "origin": {},
                            "role": "synthetic",
                            "transform": "format",
                        }
                    )
                    text += label
                    begin = len(text)
                    text += rendered
                    origin = (
                        {"format": media_type, "row": index, "column": key, "value_range": [0, len(rendered)]}
                        if media_type == "csv"
                        else {
                            "format": "json",
                            "pointer": prefix + key,
                            "record_index": index,
                            "value_range": [0, len(rendered)],
                            "value_type": type(value).__name__,
                        }
                    )
                    spans.append(
                        {
                            "chunk_range": [begin, len(text)],
                            "origin": origin,
                            "role": "source",
                            "transform": "identity" if isinstance(value, str) else "json_serialize",
                        }
                    )
                    fields[key] = {"value": value, "rendered": rendered, "origin": origin}
                    spans.append(
                        {
                            "chunk_range": [len(text), len(text) + 1],
                            "origin": {},
                            "role": "synthetic",
                            "transform": "format",
                        }
                    )
                    text += "\n"
            chunks = self._chunk_text(
                text,
                {"format": media_type, "row": index}
                if media_type == "csv"
                else {"format": "json", "pointer": prefix, "record_index": index},
                doc["document_id"],
                domains,
                overlap=False,
                spans=spans,
            )

            # 从切片中的 source 区间回填字段证据，保持 quote 与坐标一致。
            for field in fields.values():
                field["evidence"] = []
                if field.get("missing"):
                    continue
                for chunk in chunks:
                    for span in chunk["source_spans"]:
                        origin = span["origin"]
                        match = (
                            origin.get("column") == field["origin"].get("column")
                            if media_type == "csv"
                            else origin.get("pointer") == field["origin"].get("pointer")
                        )
                        if span["role"] == "source" and match:
                            a, b = span["chunk_range"]
                            field["evidence"].append(
                                {
                                    "chunk_id": chunk["chunk_id"],
                                    "start": a,
                                    "end": b,
                                    "quote": chunk["text"][a:b],
                                }
                            )
            all_chunks.extend(chunks)
            output_records.append({"index": index, "fields": fields})
        if not all_chunks:
            raise KGError("EMPTY_DOCUMENT", "No structured records found")
        return {"chunks": all_chunks, "records": output_records, "mapping_id": mapping_id}
