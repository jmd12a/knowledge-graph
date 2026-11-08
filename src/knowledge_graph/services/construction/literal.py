"""Resolve model-selected literal ranges against visible, reversible parser text."""

from __future__ import annotations

from pydantic import Field

from ...domain import KGError, StrictModel
from ...domain.source import source_evidence
from .source_references import origin_key, unit_range_evidence, unit_visible_parts


class SourceSelection(StrictModel):
    unit_key: str = Field(min_length=1)
    quote: str | None = Field(default=None, min_length=1)


def _selection_ranges(parts, catalog, chunks, unit_keys):
    if not parts or "start" in parts[0]:
        return parts
    selections = []
    for part in parts:
        key = part["unit_key"]
        if key not in unit_keys or key not in catalog:
            raise KGError("LITERAL_RANGE_INVALID", "A literal selection cites an unavailable source block")
        quote = part.get("quote")
        if quote is not None:
            selections.extend(_unique_visible_match(quote, catalog, chunks, [key]))
        else:
            visible = unit_visible_parts(catalog[key], chunks)
            if len(visible) != 1:
                raise KGError("LITERAL_RANGE_INVALID", "A whole-block selection requires one visible range")
            start, end = visible[0]["unit_range"]
            selections.append({"unit_key": key, "start": start, "end": end})
    return selections


def _offsets(part, catalog, allowed, chunks, *, allow_open_edges):
    if not isinstance(part, dict) or set(part) != {"unit_key", "start", "end"}:
        raise KGError("LITERAL_RANGE_INVALID", "A literal part requires a source unit and offsets")
    key, start, end = part["unit_key"], part["start"], part["end"]
    if not isinstance(key, str) or key not in allowed or key not in catalog:
        raise KGError("LITERAL_RANGE_INVALID", "A literal part cites a unit outside the model input")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end <= len(catalog[key]["text"])
    ):
        raise KGError("LITERAL_RANGE_INVALID", "A literal part has invalid source-unit offsets")
    source = catalog[key]["text"]
    broken_start = start > 0 and _same_ascii_word(source[start - 1], source[start])
    broken_end = end < len(source) and _same_ascii_word(source[end - 1], source[end])
    if broken_start or broken_end:
        open_edge = allow_open_edges and any(
            (not broken_start or part["unit_range"][0] == start)
            and (not broken_end or part["unit_range"][1] == end)
            for part in unit_visible_parts(catalog[key], chunks)
        )
        if not open_edge:
            raise KGError(
                "LITERAL_BOUNDARY_UNRESOLVED",
                "A literal range starts or ends inside a source word",
            )
    evidence = unit_range_evidence(catalog[key], start, end, chunks)
    return evidence


def _same_ascii_word(left, right):
    return left.isascii() and right.isascii() and left.isalnum() and right.isalnum()


def _text_cells(chunks):
    cells = {}
    for chunk_id, chunk in chunks.items():
        for span in chunk["source_spans"]:
            if span.get("role") != "source":
                continue
            origin = span["origin"]
            if origin.get("format") not in {"txt", "md"}:
                raise KGError("LITERAL_RANGE_INVALID", "Text literal mixes parser formats")
            start, end = origin["char_range"]
            left, right = span["chunk_range"]
            if end - start != right - left:
                raise KGError("LITERAL_RANGE_INVALID", "Literal source coordinates are not reversible")
            for offset in range(right - left):
                position = start + offset
                cell = (chunk["text"][left + offset], chunk_id, left + offset, "source")
                if position in cells and cells[position][0] != cell[0]:
                    raise KGError("LITERAL_RANGE_INVALID", "Overlapping source windows disagree")
                cells.setdefault(position, cell)
    return cells


def _parser_blocks(chunks, source_format):
    blocks, lengths = {}, {}
    for chunk_id, chunk in chunks.items():
        profile = chunk["parser_profile"]
        order, length = profile.get("block_order"), profile.get("block_length")
        interval = chunk.get("block_range")
        if (
            profile.get("revision", 0) < 2
            or profile.get("format") != source_format
            or not isinstance(order, int)
            or isinstance(order, bool)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length <= 0
            or not isinstance(interval, list)
            or len(interval) != 2
            or interval[1] - interval[0] != len(chunk["text"])
            or not 0 <= interval[0] < interval[1] <= length
        ):
            raise KGError("LITERAL_RANGE_INVALID", "Literal parser block metadata is unavailable")
        if order in lengths and lengths[order] != length:
            raise KGError("LITERAL_RANGE_INVALID", "Literal parser block lengths disagree")
        lengths[order] = length
        cells = blocks.setdefault(order, {})
        for span in chunk["source_spans"]:
            left, right = span["chunk_range"]
            if not 0 <= left < right <= len(chunk["text"]):
                raise KGError("LITERAL_RANGE_INVALID", "Literal parser span is invalid")
            if span.get("role") not in {"source", "synthetic"}:
                raise KGError("LITERAL_RANGE_INVALID", "Literal parser span has an unknown role")
            for offset in range(left, right):
                position = interval[0] + offset
                cell = (chunk["text"][offset], chunk_id, offset, span["role"])
                if position in cells and (cells[position][0], cells[position][3]) != (
                    cell[0],
                    cell[3],
                ):
                    raise KGError("LITERAL_RANGE_INVALID", "Overlapping parser windows disagree")
                cells.setdefault(position, cell)
    return blocks, lengths


def _position(evidence, chunks, source_format):
    item = evidence[0]
    chunk = chunks[item["chunk_id"]]
    if source_format in {"txt", "md"}:
        return (0, item["source_spans"][0]["origin"]["char_range"][0])
    return (
        chunk["parser_profile"]["block_order"],
        chunk["block_range"][0] + item["chunk_range"][0],
    )


def _end_position(evidence, chunks, source_format):
    item = evidence[-1]
    chunk = chunks[item["chunk_id"]]
    if source_format in {"txt", "md"}:
        return (0, item["source_spans"][-1]["origin"]["char_range"][1])
    return (
        chunk["parser_profile"]["block_order"],
        chunk["block_range"][0] + item["chunk_range"][1],
    )


def resolve_literal(parts, catalog, chunks, input_unit_keys, *, allow_open_edges=False):
    """Return parser-derived value, source-only citations and display provenance."""
    if not isinstance(parts, list) or not parts:
        raise KGError("LITERAL_RANGE_MISSING", "A literal field requires ordered source ranges")
    formats = {chunk["parser_profile"].get("format") for chunk in chunks.values()}
    if len(formats) != 1 or next(iter(formats)) not in {"txt", "md", "docx", "pdf"}:
        raise KGError("LITERAL_RANGE_INVALID", "Literal source format is unavailable")
    source_format = next(iter(formats))
    allowed = set(input_unit_keys)
    selected = [_offsets(part, catalog, allowed, chunks, allow_open_edges=allow_open_edges) for part in parts]
    endpoints = [
        (_position(items, chunks, source_format), _end_position(items, chunks, source_format))
        for items in selected
    ]
    if any(start >= end for start, end in endpoints) or any(
        previous[1] > current[0] for previous, current in zip(endpoints, endpoints[1:])
    ):
        raise KGError("LITERAL_RANGE_INVALID", "Literal parts overlap or are out of source order")

    first, last = endpoints[0][0], endpoints[-1][1]
    if source_format in {"txt", "md"}:
        source = _text_cells(chunks)
        ordered = [source.get(position) for position in range(first[1], last[1])]
    else:
        blocks, lengths = _parser_blocks(chunks, source_format)
        ordered = []
        for block in range(first[0], last[0] + 1):
            if block not in lengths:
                raise KGError("LITERAL_RANGE_INVALID", "An intermediate parser block was not visible")
            start = first[1] if block == first[0] else 0
            end = last[1] if block == last[0] else lengths[block]
            ordered.extend(blocks[block].get(position) for position in range(start, end))
            if block < last[0]:
                ordered.extend((character, None, None, "synthetic") for character in "\n\n")
    if not ordered or any(cell is None for cell in ordered):
        raise KGError("LITERAL_RANGE_INVALID", "Literal wording includes source not visible to the model")

    value = "".join(cell[0] for cell in ordered)
    references, source_parts, synthetic_parts = [], [], []
    index = 0
    while index < len(ordered):
        start = index
        character, chunk_id, offset, role = ordered[index]
        index += 1
        if role == "synthetic":
            while index < len(ordered) and ordered[index][3] == "synthetic":
                index += 1
            synthetic_parts.append({"display_range": [start, index], "text": value[start:index]})
            continue
        while (
            index < len(ordered)
            and ordered[index][3] == "source"
            and ordered[index][1] == chunk_id
            and ordered[index][2] == offset + index - start
        ):
            index += 1
        citation = source_evidence(chunks[chunk_id], offset, offset + index - start)
        references.append(
            {
                "chunk_id": chunk_id,
                "start": offset,
                "end": offset + index - start,
                "quote": value[start:index],
            }
        )
        source_parts.append(
            {
                "display_range": [start, index],
                "text": value[start:index],
                "source_spans": citation["source_spans"],
            }
        )
    if not source_parts:
        raise KGError("LITERAL_RANGE_INVALID", "Literal value has no source-backed text")
    return {
        "value": value,
        "evidence": references,
        "fidelity": "source_exact" if source_format in {"txt", "md"} else "parser_normalized",
        "source_parts": source_parts,
        "synthetic_parts": synthetic_parts,
    }


def _unique_visible_match(value, catalog, chunks, unit_keys):
    """Locate an omitted range only when one whole model value occurs in visible source text."""
    if not isinstance(value, str) or not value:
        raise KGError("LITERAL_RANGE_MISSING", "A literal field has no source range or exact text")
    matches = []
    for key in unit_keys:
        unit = catalog.get(key)
        if unit is None:
            raise KGError("LITERAL_RANGE_INVALID", "A literal clause cites an unavailable unit")
        for part in unit_visible_parts(unit, chunks):
            offset = 0
            while (found := part["text"].find(value, offset)) >= 0:
                start = part["unit_range"][0] + found
                matches.append({"unit_key": key, "start": start, "end": start + len(value)})
                if len(matches) > 1:
                    raise KGError("LITERAL_RANGE_AMBIGUOUS", "Literal text has multiple visible positions")
                offset = found + 1
    if not matches:
        raise KGError("LITERAL_RANGE_MISSING", "Literal text has no exact visible source match")
    return matches


def ground_literal_field(
    item: dict,
    field: str,
    values: str,
    catalog: dict,
    chunks: dict,
    unit_keys: list[str],
    *,
    fallback_units: list[str] | None = None,
    allow_open_edges: bool = False,
) -> dict:
    """定位并写入一个逐字字段，让修复只触及失败字段。"""
    parts = item.get("literal_parts", {}).get(field)
    method = item.get("literal_provenance", {}).get(field, {}).get("selection_method") or "model_range"
    if parts is None:
        method = "unique_visible_match"
        parts = _unique_visible_match(
            item[values].get(field), catalog, chunks, unit_keys if fallback_units is None else fallback_units
        )
    elif parts and "start" not in parts[0]:
        method = "source_selection"
        parts = _selection_ranges(parts, catalog, chunks, unit_keys)
    result = resolve_literal(parts, catalog, chunks, unit_keys, allow_open_edges=allow_open_edges)
    item.setdefault("literal_parts", {})[field] = parts
    item[values][field] = result["value"]
    item["evidence"][field] = result["evidence"]
    item.setdefault("literal_provenance", {})[field] = {"selection_method": method} | {
        key: result[key] for key in ("fidelity", "source_parts", "synthetic_parts")
    }
    return result


def normalize_candidate_literals(
    candidate, definition, catalog, chunks, input_unit_keys, *, group_scopes=None
):
    """Replace configured literal values and citations with verified parser output."""
    if candidate.get("mapping_id"):
        return
    literal = {
        field
        for field, property_def in definition.properties.items()
        if property_def.source_mode == "literal"
    }
    atomic = {field for group in definition.atomic_groups for field in group.fields}
    parts_by_field = candidate.get("literal_parts", {})
    if set(parts_by_field) - (literal - atomic):
        raise KGError("LITERAL_RANGE_INVALID", "Entity literal_parts names a nonliteral or atomic field")
    for field in literal - atomic:
        if field not in candidate["properties"] and field not in parts_by_field:
            continue
        ground_literal_field(candidate, field, "properties", catalog, chunks, input_unit_keys)
    groups = {group.name: group for group in definition.atomic_groups}
    for name, declaration in candidate.get("atomic_groups", {}).items():
        group = groups.get(name)
        if group is None:
            continue
        literal_fields = set(group.fields) & literal
        selected = declaration.get("literal_parts", {})
        if set(selected) - literal_fields:
            raise KGError("LITERAL_RANGE_INVALID", "Atomic literal_parts names a nonliteral field")
        local_chunks, local_units = (
            group_scopes[name] if group_scopes and name in group_scopes else (chunks, input_unit_keys)
        )
        for field in literal_fields:
            result = ground_literal_field(
                declaration,
                field,
                "fields",
                catalog,
                local_chunks,
                local_units,
                fallback_units=declaration.get("rule_units", []),
            )
            covered = []
            for source_part in result["source_parts"]:
                for span in source_part["source_spans"]:
                    origin = span["origin"]
                    coordinate = "value_range" if "value_range" in origin else "char_range"
                    covered.append((origin_key(origin), coordinate, origin[coordinate]))
            required_units = {
                unit["unit_key"]
                for unit in catalog.values()
                if unit["unit_key"] in local_units
                and any(
                    region["origin"] == anchor
                    and region["coordinate"] == coordinate
                    and region["range"][0] < interval[1]
                    and interval[0] < region["range"][1]
                    for region in unit["regions"]
                    for anchor, coordinate, interval in covered
                )
            }
            declaration["rule_units"] = sorted(
                set(declaration.get("rule_units", [])) | required_units,
                key=lambda key: catalog[key]["order"] if key in catalog else len(catalog),
            )
