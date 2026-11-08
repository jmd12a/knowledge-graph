from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import islice

from ...domain.errors import KGError
from ...domain.source import source_evidence, validate_stored_evidence
from ...prompts.retrieval import COMPACT_RERANK_SYSTEM
from ..construction.extraction_batches import request_fits
from ..construction.source_references import origin_key
from ..contracts import SearchRequest

_WORD = re.compile(r"\w+")
_COMPARE = re.compile(
    r"\b(?:compare|comparison|contrast|differences?|versus)\b|比较|对比|对照|差异|分别", re.I
)
_MAX_SNIPPETS = 2
_MAX_REFERENCES = 6
_CONTEXT_CHARS = 240
_SCHEMA = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["ids"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Excerpt:
    document: str
    origin: str
    start: int
    text: str
    field: str = ""


def _terms(text: str) -> set[str]:
    return {word.casefold() for word in _WORD.findall(text) if len(word) >= 4}


def _focus(text: str, limit: int, terms: set[str]) -> tuple[int, str]:
    if len(text) <= limit:
        return 0, text
    anchors = (
        max(0, match.start() - limit // 3)
        for match in _WORD.finditer(text)
        if match.group().casefold() in terms
    )
    starts = [0, *islice(anchors, 32), len(text) - limit]
    start = max(starts, key=lambda offset: len(_terms(text[offset : offset + limit]) & terms))
    return start, text[start : start + limit]


def _excerpts(chunk: dict, reference: dict, field: str, limit: int, terms: set[str]) -> list[Excerpt]:
    result = []
    for span in reference["source_spans"]:
        start, end = span["chunk_range"]
        origin = span["origin"]
        coordinate = "value_range" if "value_range" in origin else "char_range"
        offset, text = _focus(chunk["text"][start:end], limit, terms)
        result.append(
            Excerpt(chunk["document_id"], origin_key(origin), origin[coordinate][0] + offset, text, field)
        )
    return result


def _prefer_sources(items, source, comparison_sources, limit):
    if not comparison_sources:
        return items[:limit]
    preferred = {}
    for item in items:
        document = source(item)
        if document in comparison_sources:
            preferred.setdefault(document, item)
    selected = list(preferred.values())
    return (selected + [item for item in items if item not in selected])[:limit]


def _pick_excerpts(excerpts: list[Excerpt], terms: set[str], comparison_sources: set[str]) -> list[Excerpt]:
    excerpts = list(dict.fromkeys(excerpts))
    excerpts.sort(
        key=lambda item: (
            not item.field.startswith("/atomic_groups/"),
            -len(_terms(item.text) & terms),
            not item.field.startswith("/properties/"),
            -len(item.text),
            item.document,
            item.origin,
            item.start,
        )
    )
    distinct = []
    for item in excerpts:
        if not any(
            item.document == prev.document
            and item.origin == prev.origin
            and prev.start <= item.start
            and item.start + len(item.text) <= prev.start + len(prev.text)
            for prev in distinct
        ):
            distinct.append(item)
    limit = max(_MAX_SNIPPETS, min(len(comparison_sources), _MAX_REFERENCES))
    selected = _prefer_sources(distinct, lambda item: item.document, comparison_sources, limit)
    if len(selected) == _MAX_SNIPPETS:
        covered = set().union(*(_terms(item.text) for item in selected))
        documents = {item.document for item in selected}
        for item in distinct:
            matches = _terms(item.text) & terms
            if item not in selected and matches and (matches - covered or item.document not in documents):
                selected.append(item)
                break
    return selected


async def _candidate_excerpts(r, kb_id, ids, records, component, scope, terms, comparison_sources):
    limit = r.settings.retrieval_rerank_snippet_chars
    excerpts = defaultdict(list)
    invalid = 0
    if component == "documents":
        for key in ids:
            chunk = records[key]
            for span in chunk["source_spans"]:
                if span.get("role") != "source":
                    continue
                try:
                    reference = source_evidence(chunk, *span["chunk_range"])
                    excerpts[key].extend(_excerpts(chunk, reference, "", limit, terms))
                except KGError:
                    invalid += 1
        return excerpts, invalid

    wanted = set(ids)
    claims = (
        scope["contributions"]
        if scope is not None
        else await r.metadata.target_contributions(kb_id, [("entity", key) for key in ids], active_only=True)
    )
    refs = defaultdict(dict)
    for claim in claims:
        key = claim["target_id"]
        if claim["target_kind"] != "entity" or key not in wanted:
            continue
        for ref in claim["evidence"]:
            if ref.get("role", "fact") != "fact":
                continue
            if ref["document_id"] != claim["document_id"]:
                invalid += 1
                continue
            signature = (ref["chunk_id"], tuple(ref["chunk_range"]))
            entry = (claim["field_path"], ref)
            previous = refs[key].get(signature)
            if previous is None or _ref_order(entry, terms) < _ref_order(previous, terms):
                refs[key][signature] = entry
    # 每个候选只取少量有希望的引用。只在选定引用后读取切片正文。
    picked = {
        key: _prefer_sources(
            sorted(items.values(), key=lambda item: _ref_order(item, terms)),
            lambda item: item[1]["document_id"],
            comparison_sources,
            _MAX_REFERENCES,
        )
        for key, items in refs.items()
    }
    chunk_ids = sorted({ref["chunk_id"] for items in picked.values() for _, ref in items})
    chunks = {point["id"]: point["payload"] for point in await r.vectors.read(kb_id, "chunks", chunk_ids)}
    for key, items in picked.items():
        for field, ref in items:
            try:
                chunk = chunks.get(ref["chunk_id"])
                validate_stored_evidence(ref, chunk)
                excerpts[key].extend(_excerpts(chunk, ref, field, limit, terms))
            except KGError:
                invalid += 1
    return excerpts, invalid


def _ref_order(entry, terms):
    field, ref = entry
    return (
        not field.startswith("/atomic_groups/"),
        -len(_terms(ref.get("quote", "")) & terms),
        not field.startswith("/properties/"),
        ref["document_id"],
        ref["chunk_range"],
        ref["chunk_id"],
    )


async def _compact_payload(
    r,
    kb_id: str,
    request: SearchRequest,
    ordered: list[str],
    records: dict,
    component: str,
    scope: dict | None,
    documents: dict,
) -> tuple[dict, dict[str, str], dict]:
    """在重排输入预算内优先为每个候选分配一段核验过的原文。"""
    shortlist = ordered[: r.settings.rerank_candidate_limit]
    terms = _terms(request.query)
    compare = len(request.filters.document_ids) > 1 and bool(_COMPARE.search(request.query))
    comparison_sources = set(request.filters.document_ids) if compare else set()
    if compare:
        named = {
            key
            for key in comparison_sources
            if documents.get(key) and documents[key].casefold() in request.query.casefold()
        }
        if len(named) >= 2:
            comparison_sources = named
    raw, invalid_evidence = await _candidate_excerpts(
        r, kb_id, shortlist, records, component, scope, terms, comparison_sources
    )
    data = {
        "query": request.query,
        "comparison": compare,
        "final_limit": min(request.limit, len(shortlist)),
        "candidates": [],
        "snippets": {},
    }
    source_ids = {key: str(i) for i, key in enumerate(sorted(comparison_sources), 1)}
    if compare:
        data["sources"] = {local: documents.get(key, "")[:200] for key, local in source_ids.items()}
    candidate_keys, snippets, snippet_ids = {}, {}, {}

    def pack(candidate, key, excerpts, *, replace=False):
        local, additions = {}, {}
        for excerpt in excerpts:
            signature = (excerpt.document, excerpt.origin, excerpt.start, excerpt.text)
            number = snippet_ids.get(signature)
            if number is None:
                number = str(len(snippet_ids) + len(additions) + 1)
                source = source_ids.get(excerpt.document)
                additions[signature] = (
                    number,
                    [excerpt.text, source] if source is not None else [excerpt.text],
                )
            local[number] = excerpt
        candidate = candidate | {"snippets": list(local)}
        candidates = [*data["candidates"]]
        alias = candidate["id"]
        if replace:
            candidates[int(alias) - 1] = candidate
        else:
            candidates.append(candidate)
        proposed = data | {"candidates": candidates, "snippets": data["snippets"] | dict(additions.values())}
        if not request_fits(r.language, COMPACT_RERANK_SYSTEM, proposed, _SCHEMA):
            return False
        data.update(proposed)
        candidate_keys[alias] = key
        snippets[key] = local
        snippet_ids.update((sig, number) for sig, (number, _) in additions.items())
        return True

    without_evidence = 0
    pending = []
    for key in shortlist:
        excerpts = _pick_excerpts(raw[key], terms, comparison_sources)
        without_evidence += not excerpts
        item = records[key]
        candidate = {"id": str(len(candidate_keys) + 1)}
        if name := item.get("canonical_name"):
            candidate["name"] = name[:120]
        text = item.get("search_text", "") if component != "documents" else ""
        if not excerpts:
            text = item.get("text", text)
        _, context = _focus(text, _CONTEXT_CHARS, terms)
        if context and not any(context in excerpt.text for excerpt in excerpts):
            candidate["context"] = context
        if component == "documents":
            path = item.get("section_path") or []
            section = " > ".join(path[-2:])[:_CONTEXT_CHARS]
            if section and not any(section in excerpt.text for excerpt in excerpts):
                candidate["section"] = section
        if pack(candidate, key, excerpts[:1]) and len(excerpts) > 1:
            pending.append((candidate, key, excerpts))

    supplemental_skipped = 0
    # 先为每个候选保留主证据。补充片段按轮分配，避免前面的候选占满请求。
    for depth in range(1, max((len(excerpts) for _, _, excerpts in pending), default=1)):
        for candidate, key, excerpts in pending:
            if depth < len(excerpts):
                accepted = pack(candidate, key, [*snippets[key].values(), excerpts[depth]], replace=True)
                supplemental_skipped += not accepted
    metrics = {
        "rerank_shortlist": len(shortlist),
        "rerank_candidates": len(candidate_keys),
        "rerank_without_evidence": without_evidence,
        "rerank_payload_skipped": len(shortlist) - len(candidate_keys),
        "rerank_supplemental_skipped": supplemental_skipped,
        "rerank_unique_snippets": len(data["snippets"]),
        "invalid_rerank_evidence": invalid_evidence,
    }
    data["final_limit"] = min(request.limit, len(candidate_keys))
    return data, candidate_keys, metrics


async def compact_rerank(
    r,
    kb_id: str,
    request: SearchRequest,
    ordered: list[str],
    records: dict,
    component: str,
    scope: dict | None,
    documents: dict,
) -> tuple[list[str], set[str], dict]:
    """模型只选择候选 ID；不足请求数量时沿原融合顺序补齐。"""
    async with asyncio.timeout(min(r.settings.rerank_timeout, r.settings.retrieval_timeout / 3)):
        data, candidate_keys, metrics = await _compact_payload(
            r, kb_id, request, ordered, records, component, scope, documents
        )
        if len(candidate_keys) <= 1:
            return ordered, set(), metrics
        response = await r.language.json(COMPACT_RERANK_SYSTEM, data, _SCHEMA)
    ids = response["ids"]
    ranked = list(dict.fromkeys(candidate_keys[number] for number in ids if number in candidate_keys))
    selected = ranked[: data["final_limit"]]
    metrics["invalid_rerank_ids"] = len(ids) - len(ranked)
    metrics["rerank_selected"] = len(selected)
    selected_keys = set(selected)
    return (
        selected + [key for key in ordered if key not in selected_keys],
        set(candidate_keys.values()),
        metrics,
    )
