"""构造可复用的批次输入，并将模型引用映射回当前来源。"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from itertools import groupby

from ...domain import canonical


def request_fits(language, system, request, schema) -> bool:
    if getattr(language.profile, "max_context_tokens", None) is not None:
        return language.json_request_fits(system, request, schema)
    return language.json_request_chars(system, request, schema) <= language.profile.max_input_chars


class ExtractionBatch:
    def __init__(self, scope, chunks, model, *, scope_policy="whole", units_only=False):
        units_only = units_only or scope_policy == "structure"
        self.chunk_ids = (
            {} if units_only else {f"chunk/{i}": key for i, key in enumerate(scope["input_chunk_ids"])}
        )
        self.unit_keys = {f"unit/{index}": key for index, key in enumerate(scope["input_unit_keys"])}
        tables = {}

        def local_origin(origin):
            position_fields = {"char_range", "line_range", "value_range", "paragraph", "table"}
            local = {key: value for key, value in origin.items() if key not in position_fields}
            if "table" in origin:
                local["table"] = tables.setdefault(origin["table"], len(tables))
            return local

        # 保留顺序、章节、页码、表格结构和可引用范围，绝对坐标留在来源映射中。
        local_chunks = []
        for alias, key in self.chunk_ids.items():
            chunk = chunks[key]
            local_chunks.append(
                {
                    "chunk_id": alias,
                    "text": chunk["text"],
                    "locator": local_origin(chunk["locator"]),
                    "source_spans": [
                        span | {"origin": local_origin(span["origin"])} for span in chunk["source_spans"]
                    ],
                    "section_path": chunk.get("section_path", []),
                    "parser_profile": chunk["parser_profile"],
                    **({"block_range": chunk["block_range"]} if "block_range" in chunk else {}),
                    "domain_ids": chunk["domain_ids"],
                }
            )
        self.request = {
            "business_model": model,
            "scope_policy": scope_policy,
            **({"chunks": local_chunks} if not units_only else {}),
            "rule_units": [
                view | {"unit_key": alias, "order": index}
                for index, (alias, view) in enumerate(zip(self.unit_keys, scope["unit_views"], strict=True))
            ],
        }

    def request_key(self, language, system, schema, *, request=None):
        profile = language.profile.model_dump(
            exclude={"api_key_env", "timeout", "max_input_chars", "max_context_tokens"}
        )
        # 请求中的字段顺序也会进入模型看到的 JSON 文本。
        prompt = json.dumps(
            [self.request if request is None else request, schema], ensure_ascii=False, allow_nan=False
        )
        return sha256(canonical([profile, system, prompt]).encode("utf-8")).hexdigest()

    def bind(self, output):
        """只替换契约中的来源引用，保留业务值和模型给出的实体引用。"""
        output = deepcopy(output)
        all_known = True

        def source_key(value, ids):
            nonlocal all_known
            if value not in ids:
                all_known = False
                # 未提供的局部引用不能碰巧命中当前文档中的真实标识。
                return "unsupplied/" + value
            return ids[value]

        def bind_refs(refs):
            for ref in refs:
                if "chunk_id" in ref:
                    ref["chunk_id"] = source_key(ref["chunk_id"], self.chunk_ids)
                else:
                    ref["unit_key"] = source_key(ref["unit_key"], self.unit_keys)

        for entity in output["entities"]:
            bind_refs(entity.get("mention_evidence", []))
            if entity.get("identity_equivalence"):
                bind_refs(entity["identity_equivalence"]["evidence"])
            for refs in entity["evidence"].values():
                bind_refs(refs)
            for declaration in entity["atomic_groups"].values():
                for parts in declaration.get("literal_parts", {}).values():
                    for part in parts:
                        part["unit_key"] = source_key(part["unit_key"], self.unit_keys)
                for refs in declaration["evidence"].values():
                    bind_refs(refs)
                declaration["rule_units"] = [
                    source_key(key, self.unit_keys) for key in declaration["rule_units"]
                ]
                section = declaration["section_key"]
                declaration["section_key"] = self.unit_keys.get(section, section)
                bind_refs(declaration["section_evidence"])
                for reference in declaration["resolved_references"]:
                    bind_refs(reference["reference"])
                    bind_refs(reference["target"])
            for parts in entity.get("literal_parts", {}).values():
                for part in parts:
                    part["unit_key"] = source_key(part["unit_key"], self.unit_keys)
        for relation in output["relations"]:
            bind_refs(relation["evidence"])
            for refs in relation["property_evidence"].values():
                bind_refs(refs)
        return output, all_known


def chapter_groups(chunks):
    """按相邻的章节路径划分长文档，保持原文顺序。"""
    for _, group in groupby(chunks.items(), key=lambda item: item[1].get("section_path", [])):
        yield dict(group)


def reusable_batches(documents):
    """只导出已经通过本轮证据检查的局部响应。"""
    for entry in documents.values():
        for key, batch in entry.get("extraction_batches", {}).items():
            if key.startswith("batch/") and batch.get("reusable") is True:
                yield batch["request_key"], batch["result"]
