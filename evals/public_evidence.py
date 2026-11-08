"""用原文坐标分别统计入库覆盖、候选覆盖和最终证据覆盖。"""

from __future__ import annotations

import re
from collections import defaultdict

from knowledge_graph.bootstrap import Context

from .long_contract import chunk_ranges, definition_ranges, interval_coverage, reference_ranges

STATEMENT = "/properties/statement"
DEFINITION = "/atomic_groups/full_statement"


def locate(text: str, source: str) -> list:
    pattern = r"\s+".join(re.escape(word) for word in text.split())
    return [[match.start(), match.end()] for match in re.finditer(pattern, source, re.I)] if pattern else []


def complete(span: list, ranges: list, source: str, *, ignore_whitespace: bool) -> bool:
    coverage = interval_coverage({"char_range": span}, ranges)
    return coverage["full_span"] or (
        ignore_whitespace and all(not source[a:b].strip() for a, b in coverage["uncovered_ranges"])
    )


def anchors(question: dict, texts: dict) -> list:
    if "spans" in question:
        [doc] = texts
        return [(doc, [span]) for span in question["spans"]]
    output = []
    for support in question["supports"]:
        name = support["doc"]
        positions = locate(support["text"], texts[name])
        if not positions:
            raise ValueError(f"金标未在冻结原文中找到：{question['id']} / {name}")
        output.append((name, positions))
    return output


def by_document(groups) -> dict:
    output = defaultdict(list)
    for group in groups:
        for doc, spans in group.items():
            output[doc].extend(spans)
    return output


class PublicEvidence:
    def __init__(self, context: Context, kb: str, ids: dict, documents: dict, *, short: bool):
        self.context = context
        self.kb = kb
        self.ids = ids
        self.names = {doc_id: name for name, doc_id in ids.items()}
        self.texts = {name: doc["text"] for name, doc in documents.items()}
        self.short = short
        self.details = {}
        self.mapping_errors = []

    def grade(self, gold: list, ranges: dict) -> list[bool]:
        return [
            any(
                complete(span, ranges.get(doc, []), self.texts[doc], ignore_whitespace=self.short)
                for span in spans
            )
            for doc, spans in gold
        ]

    def reference(self, ref: dict, *, returned: bool = False) -> list:
        doc = ref["document_id"]
        return reference_ranges(ref, self.chunks, self.texts[self.names[doc]], doc, returned=returned)

    async def initialize(self) -> dict:
        c = self.context
        self.chunks = {
            point["id"]: point["payload"]
            for point in await c.vectors.all(self.kb, "chunks", with_vectors=False, max_points=2000)
        }
        self.raw = {
            key: {
                self.names[row["document_id"]]: chunk_ranges(
                    row, self.texts[self.names[row["document_id"]]], row["document_id"]
                )
            }
            for key, row in self.chunks.items()
        }
        entities, relations = await c.graph.all(self.kb)
        entity_ids = {row["entity_id"] for row in entities}
        self.literal = defaultdict(lambda: defaultdict(list))
        self.graph = defaultdict(lambda: defaultdict(list))
        claims = await c.metadata.contributions(self.kb, list(self.ids.values()))
        for claim in claims:
            key = claim["target_id"]
            if key not in entity_ids:
                continue
            name = self.names[claim["document_id"]]
            evidence = claim["evidence"]
            source = self.texts[name]
            self.graph[key][name].extend(span for ref in evidence for span in self.reference(ref))
            if claim["field_path"] == STATEMENT:
                support = [span for ref in evidence if ref["role"] == "fact" for span in self.reference(ref)]
                self.literal[key][name].extend(
                    span
                    for span in locate(claim["value"], source)
                    if complete(span, support, source, ignore_whitespace=self.short)
                )
            if claim["field_path"] == DEFINITION:
                value = claim["value"]
                support = [
                    span for ref in value["field_evidence"]["definition"] for span in self.reference(ref)
                ]
                try:
                    positions = definition_ranges(value, support, source, require_complete=False)
                except ValueError as exc:
                    # 保留诊断，允许其他已定位的证据继续计分。
                    self.mapping_errors.append(
                        {"contribution_id": claim["contribution_id"], "reason": str(exc)}
                    )
                else:
                    self.literal[key][name].extend(positions)
        return {
            "entities": len(entities),
            "relations": len(relations),
            "chunks": len(self.chunks),
            "claims": len(claims),
        }

    async def detail(self, key: str) -> tuple[dict, dict, dict]:
        if key not in self.details:
            result = await self.context.retrieval.get_knowledge(
                self.kb, "entity", key, evidence_limit=1000 if self.short else 100
            )
            before, added, literal = defaultdict(list), defaultdict(list), defaultdict(list)
            refs = result["evidence"]
            for ref in refs:
                derived = ref["role"] == "context" and not ref["contribution_ids"]
                (added if derived else before)[self.names[ref["document_id"]]].extend(
                    self.reference(ref, returned=True)
                )
            if self.short and result["item"]["entity_type"] == "Fact":
                for value in result["item"]["properties"]["statement"]["values"]:
                    ids = set(value["contribution_ids"])
                    for doc in result["item"]["document_ids"]:
                        name = self.names[doc]
                        support = [
                            span
                            for ref in refs
                            if ref["role"] == "fact"
                            and ref["document_id"] == doc
                            and STATEMENT in ref["field_paths"]
                            and ids.intersection(ref["contribution_ids"])
                            for span in self.reference(ref, returned=True)
                        ]
                        literal[name].extend(
                            span
                            for span in locate(value["value"], self.texts[name])
                            if complete(span, support, self.texts[name], ignore_whitespace=True)
                        )
            self.details[key] = before, added, literal
        return self.details[key]
