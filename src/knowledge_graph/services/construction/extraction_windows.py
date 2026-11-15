"""按原文边界规划抽取窗口。"""

from __future__ import annotations

from collections import defaultdict

from ...domain import KGError
from ...domain.source import DEFAULT_STRUCTURE_WINDOW_CHARS
from ...prompts.extraction import EXTRACTION_SYSTEM, STRUCTURE_EXTRACTION_SYSTEM
from .extraction_batches import ExtractionBatch, chapter_groups, request_fits
from .source_references import unit_visible_parts, units_by_chunk


def visible_source_chars(scope: dict) -> int:
    """统计按原文坐标去重后的窗口字符数。"""
    return sum(len(part["text"]) for view in scope["unit_views"] for part in view["visible_parts"])


def plan_source_windows(chunks, units, fits_budget, *, target_source_chars: int) -> list[dict]:
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
        return build_input_scope(window, units if indexes is None else [units[i] for i in indexes], key=key)

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


def _unit_prompt(unit, chunks):
    parts = unit_visible_parts(unit, chunks)
    return {
        "unit_key": unit["unit_key"],
        "order": unit["order"],
        "visible_parts": parts,
        "fully_supplied": len(parts) == 1 and parts[0]["unit_range"] == [0, len(unit["text"])],
    }


def build_input_scope(chunks, context, *, key=None):
    """按已选切片生成模型可见的原文范围。"""
    views = [_unit_prompt(unit, chunks) for unit in context]
    views = [view for view in views if view["visible_parts"]]
    scope = {
        "input_chunk_ids": list(chunks),
        "input_unit_keys": [view["unit_key"] for view in views],
        "unit_views": views,
    }
    if key is not None:
        scope["key"] = key
    return scope


def check_input_scope(scope, chunks, context) -> None:
    """恢复任务时校验保存的原文范围与解析坐标一致。"""
    catalog = {unit["unit_key"]: unit for unit in context}
    if (
        not isinstance(scope, dict)
        or not isinstance(scope.get("input_chunk_ids"), list)
        or not isinstance(scope.get("input_unit_keys"), list)
        or not isinstance(scope.get("unit_views"), list)
    ):
        raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "The saved model input scope is unavailable")
    if "key" in scope and not isinstance(scope["key"], str):
        raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "The saved model input batch key is invalid")
    chunk_ids, unit_keys = scope["input_chunk_ids"], scope["input_unit_keys"]
    if (
        any(not isinstance(key, str) or key not in chunks for key in chunk_ids)
        or len(chunk_ids) != len(set(chunk_ids))
        or not chunk_ids
        or any(not isinstance(key, str) or key not in catalog for key in unit_keys)
        or len(unit_keys) != len(set(unit_keys))
        or any(not isinstance(view, dict) for view in scope["unit_views"])
        or [view.get("unit_key") for view in scope["unit_views"]] != unit_keys
    ):
        raise KGError(
            "PREPARATION_SCOPE_UNAVAILABLE", "The saved model input scope does not match its parsed source"
        )
    local_chunks = {key: chunks[key] for key in chunk_ids}
    for view in scope["unit_views"]:
        expected = _unit_prompt(catalog[view["unit_key"]], local_chunks)
        if view != expected:
            raise KGError(
                "PREPARATION_SCOPE_UNAVAILABLE",
                "Saved visible source-unit text differs from its authorized chunks",
            )


def plan_extraction_scopes(
    language,
    chunks,
    context,
    model_data,
    schema,
    *,
    scope_policy="whole",
    structure_window_chars: int = DEFAULT_STRUCTURE_WINDOW_CHARS,
    system: str | None = None,
):
    """选择全文输入或按来源边界分窗，并检查模型请求预算。"""
    if system is None:
        system = STRUCTURE_EXTRACTION_SYSTEM if scope_policy == "structure" else EXTRACTION_SYSTEM

    def fits_budget(batch_chunks, scope):
        request = ExtractionBatch(scope, batch_chunks, model_data, scope_policy=scope_policy).request
        return request_fits(language, system, request, schema)

    if scope_policy == "structure":
        whole_limit = min(structure_window_chars, language.profile.max_input_chars)
        if sum(len(unit["text"]) for unit in context) <= whole_limit:
            whole = build_input_scope(chunks, context, key="batch/0")
            if fits_budget(chunks, whole):
                return [whole]
        return plan_source_windows(
            chunks,
            context,
            lambda scope: fits_budget(chunks, scope),
            target_source_chars=structure_window_chars,
        )

    # 单批能容纳全文时保留跨章节上下文；长文档再按章节独立装箱。
    if scope_policy != "whole":
        raise KGError("INVALID_ARGUMENT", "Unknown extraction strategy")
    if sum(len(chunk["text"]) for chunk in chunks.values()) <= language.profile.max_input_chars:
        whole = build_input_scope(chunks, context, key="batch/0")
        if fits_budget(chunks, whole):
            return [whole]

    unit_positions_by_chunk = units_by_chunk(context, chunks)

    def context_for_chunks(batch_chunks):
        if unit_positions_by_chunk is None:
            return context
        selected = set().union(*(unit_positions_by_chunk[chunk_id] for chunk_id in batch_chunks))
        return [context[position] for position in sorted(selected)]

    chapter_batches = []
    for chapter in chapter_groups(chunks):
        batch_chunks = {}
        for chunk_id, chunk in chapter.items():
            expanded_batch = batch_chunks | {chunk_id: chunk}
            if fits_budget(
                expanded_batch, build_input_scope(expanded_batch, context_for_chunks(expanded_batch))
            ):
                batch_chunks = expanded_batch
            else:
                if not batch_chunks:
                    raise KGError(
                        "MODEL_INPUT_TOO_LARGE", "One source chunk and model exceed the extraction budget"
                    )
                chapter_batches.append(batch_chunks)
                batch_chunks = {chunk_id: chunk}
        if batch_chunks:
            chapter_batches.append(batch_chunks)

    # 全文超过模型预算时，尽量把相邻的完整规则单元带入同一批。
    scopes = []
    for position, batch_chunks in enumerate(chapter_batches):
        for unit in context_for_chunks(batch_chunks):
            unit_chunks = {ref["chunk_id"]: chunks[ref["chunk_id"]] for ref in unit["evidence"]}
            if unit_chunks.keys() <= batch_chunks.keys():
                continue
            expanded_batch = batch_chunks | unit_chunks
            if fits_budget(
                expanded_batch, build_input_scope(expanded_batch, context_for_chunks(expanded_batch))
            ):
                batch_chunks = expanded_batch
        scope = build_input_scope(batch_chunks, context_for_chunks(batch_chunks), key=f"batch/{position}")
        if not fits_budget(batch_chunks, scope):
            raise KGError("MODEL_INPUT_TOO_LARGE", "Source context exceeds the extraction budget")
        scopes.append(scope)
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
