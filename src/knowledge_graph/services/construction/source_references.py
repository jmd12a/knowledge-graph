from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict

from ...domain import KGError, canonical
from ...domain.source import source_evidence


def origin_key(origin):
    return canonical(
        {
            key: value
            for key, value in origin.items()
            if key not in {"char_range", "value_range", "line_range"}
        }
    )


def evidence_positions(evidence):
    grouped = defaultdict(list)
    for item in evidence:
        for span in item["source_spans"]:
            origin = span["origin"]
            coordinate = "value_range" if "value_range" in origin else "char_range"
            grouped[(item["document_id"], origin_key(origin), coordinate)].append(origin[coordinate])
    result = []
    for (document_id, anchor, coordinate), intervals in sorted(grouped.items()):
        merged = []
        for left, right in sorted(intervals):
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(right, merged[-1][1])
            else:
                merged.append([left, right])
        result.append(
            {"document_id": document_id, "origin": anchor, "coordinate": coordinate, "ranges": merged}
        )
    return result


def _range(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
        or not 0 <= value[0] < value[1]
    ):
        raise KGError("EVIDENCE_INVALID", "A source unit has an invalid coordinate range")
    return value[0], value[1]


def units_by_chunk(units, chunks):
    """Index overlapping unit regions; unusual inputs retain the full validating path."""
    intervals, chunk_spans, coordinates = defaultdict(list), {}, defaultdict(set)
    try:
        if len({chunk["document_id"] for chunk in chunks.values()}) > 1:
            return None
        for chunk_id, chunk in chunks.items():
            spans = []
            for span in chunk["source_spans"]:
                if span.get("role") != "source":
                    continue
                origin = span["origin"]
                keys = {key for key in ("char_range", "value_range") if key in origin}
                if len(keys) != 1:
                    return None
                coordinate = next(iter(keys))
                left, right = _range(origin[coordinate])
                start, end = _range(span["chunk_range"])
                if right - left != end - start or end > len(chunk["text"]):
                    return None
                anchor = origin_key(origin)
                coordinates[anchor].add(coordinate)
                spans.append((anchor, coordinate, left, right))
            chunk_spans[chunk_id] = spans
        for index, unit in enumerate(units):
            if not isinstance(unit["text"], str) or not unit["text"]:
                return None
            length = 0
            for region in unit.get("regions", []):
                left, right = _range(region["range"])
                anchor, coordinate = region["origin"], region["coordinate"]
                if coordinate not in {"char_range", "value_range"} or coordinates[anchor] - {coordinate}:
                    return None
                intervals[(anchor, coordinate)].append((left, right, index))
                length += right - left
            if length != len(unit["text"]):
                return None
    except (KGError, KeyError, TypeError, AttributeError, ValueError):
        return None
    catalog = {}
    for key, entries in intervals.items():
        entries.sort()
        ends, furthest = [], 0
        for _, right, _ in entries:
            furthest = max(furthest, right)
            ends.append(furthest)
        catalog[key] = (entries, [entry[0] for entry in entries], ends)
    result = {}
    for chunk_id, spans in chunk_spans.items():
        selected = set()
        for anchor, coordinate, left, right in spans:
            entries, starts, ends = catalog.get((anchor, coordinate), ([], [], []))
            first, stop = bisect_right(ends, left), bisect_left(starts, right)
            selected.update(index for _, end, index in entries[first:stop] if end > left)
        result[chunk_id] = selected
    return result


def unit_visible_pieces(unit, chunks):
    """Map a catalog unit to nonoverlapping portions of the actually visible source chunks."""
    if not isinstance(unit, dict) or not isinstance(unit.get("text"), str) or not unit["text"]:
        raise KGError("EVIDENCE_INVALID", "A source unit lacks its original text")
    if len({chunk["document_id"] for chunk in chunks.values()}) > 1:
        raise KGError("EVIDENCE_INVALID", "Source-unit lookup cannot combine different documents")
    pieces, unit_offset = [], 0
    for region in unit.get("regions", []):
        left, right = _range(region.get("range"))
        coordinate = region.get("coordinate")
        if coordinate not in {"char_range", "value_range"}:
            raise KGError("EVIDENCE_INVALID", "A source unit lacks its coordinate type")
        matches = []
        for chunk_id, chunk in sorted(chunks.items()):
            for span in chunk["source_spans"]:
                if span.get("role") != "source" or origin_key(span.get("origin", {})) != region.get("origin"):
                    continue
                source_start, source_end = _range(span["origin"].get(coordinate))
                chunk_start, chunk_end = _range(span.get("chunk_range"))
                if source_end - source_start != chunk_end - chunk_start or chunk_end > len(chunk["text"]):
                    raise KGError(
                        "EVIDENCE_INVALID", "A source chunk has a non-reversible coordinate mapping"
                    )
                start, end = max(left, source_start), min(right, source_end)
                if start < end:
                    matches.append((start, end, chunk_id, chunk_start + start - source_start))
        cursor = left
        while cursor < right:
            available = [match for match in matches if match[0] <= cursor < match[1]]
            if not available:
                cursor = min((match[0] for match in matches if match[0] > cursor), default=right)
                continue
            # An overlap represents the same original text, not a second occurrence of a quote.
            start, end, chunk_id, chunk_start = min(available, key=lambda item: (-item[1], item[2], item[3]))
            end = min(end, right)
            a, b = chunk_start + cursor - start, chunk_start + end - start
            x, y = unit_offset + cursor - left, unit_offset + end - left
            if chunks[chunk_id]["text"][a:b] != unit["text"][x:y]:
                raise KGError(
                    "EVIDENCE_INVALID", "Source unit text disagrees with its visible original coordinates"
                )
            piece = {"unit_range": [x, y], "chunk_id": chunk_id, "chunk_range": [a, b]}
            if (
                pieces
                and pieces[-1]["chunk_id"] == chunk_id
                and pieces[-1]["unit_range"][1] == x
                and pieces[-1]["chunk_range"][1] == a
            ):
                pieces[-1]["unit_range"][1] = y
                pieces[-1]["chunk_range"][1] = b
            else:
                pieces.append(piece)
            cursor = end
        unit_offset += right - left
    if unit_offset != len(unit["text"]):
        raise KGError("EVIDENCE_INVALID", "Source unit regions do not cover its original text")
    return pieces


def unit_visible_parts(unit, chunks):
    parts = []
    for piece in unit_visible_pieces(unit, chunks):
        start, end = piece["unit_range"]
        if parts and parts[-1]["unit_range"][1] == start:
            parts[-1]["unit_range"][1] = end
            parts[-1]["text"] += unit["text"][start:end]
        else:
            parts.append({"unit_range": [start, end], "text": unit["text"][start:end]})
    return parts


def unit_range_evidence(unit, start, end, chunks, *, role="fact"):
    """Resolve a known unit interval without adding any unavailable chunk or synthetic text."""
    _range([start, end])
    if end > len(unit["text"]):
        raise KGError("EVIDENCE_INVALID", "A source-unit range exceeds its original text")
    output, cursor = [], start
    for piece in unit_visible_pieces(unit, chunks):
        left, right = piece["unit_range"]
        a, b = max(start, left), min(end, right)
        if a >= b:
            continue
        if a > cursor:
            raise KGError(
                "EVIDENCE_NOT_FOUND", "The quoted source-unit range was not fully visible in this model call"
            )
        source_start = piece["chunk_range"][0] + a - left
        source_end = source_start + b - a
        output.append(source_evidence(chunks[piece["chunk_id"]], source_start, source_end, role=role))
        cursor = b
    if cursor != end:
        raise KGError(
            "EVIDENCE_NOT_FOUND", "The quoted source-unit range was not fully visible in this model call"
        )
    if len({item["document_id"] for item in output}) != 1:
        raise KGError("EVIDENCE_INVALID", "A source-unit quote crosses document boundaries")
    return output


def unit_context_evidence(unit, chunks, *, visible_only=False):
    if visible_only:
        return [
            item
            for part in unit_visible_parts(unit, chunks)
            for item in unit_range_evidence(unit, *part["unit_range"], chunks, role="context")
        ]
    return unit_range_evidence(unit, 0, len(unit["text"]), chunks, role="context")


def _legacy_evidence(reference, chunks, role):
    chunk = chunks.get(reference.get("chunk_id"))
    if not chunk:
        raise KGError("EVIDENCE_NOT_FOUND", "Evidence references an unknown or unavailable chunk")
    left, right = reference.get("start"), reference.get("end")
    if (
        not isinstance(left, int)
        or isinstance(left, bool)
        or not isinstance(right, int)
        or isinstance(right, bool)
    ):
        raise KGError("EVIDENCE_INVALID", "Invalid Unicode character range")
    quote = reference.get("quote")
    if not isinstance(quote, str) or not quote:
        raise KGError("EVIDENCE_INVALID", "Evidence requires a nonempty exact source quote")
    if not (0 <= left < right <= len(chunk["text"]) and chunk["text"][left:right] == quote):
        left = chunk["text"].find(quote)
        if left < 0 or chunk["text"].find(quote, left + 1) >= 0:
            raise KGError(
                "EVIDENCE_INVALID", "A mismatched quote must occur exactly once in its supplied chunk"
            )
        right = left + len(quote)
    return [source_evidence(chunk, left, right, role=role)]


def verify_evidence(references, chunks, *, role="fact", units=None, input_unit_keys=None):
    """核实精确引文和调用可见范围，再映射为去重后的标准来源证据。"""
    if not references:
        raise KGError("EVIDENCE_MISSING", "Every adopted declaration requires source evidence")
    catalog = units if isinstance(units, dict) else {unit["unit_key"]: unit for unit in units or []}
    allowed = set(input_unit_keys) if input_unit_keys is not None else set()
    output, seen = [], set()
    for reference in references:
        if hasattr(reference, "model_dump"):
            reference = reference.model_dump()
        if not isinstance(reference, dict):
            raise KGError("EVIDENCE_INVALID", "An evidence reference must be an object")
        if "unit_key" in reference:
            if set(reference) != {"unit_key", "quote"}:
                raise KGError(
                    "EVIDENCE_INVALID", "Unit and chunk evidence protocols cannot be mixed in one reference"
                )
            key, quote = reference["unit_key"], reference["quote"]
            if not isinstance(key, str) or key not in allowed or key not in catalog:
                raise KGError("EVIDENCE_NOT_FOUND", "The source unit was not provided in this model call")
            if not isinstance(quote, str) or not quote:
                raise KGError("EVIDENCE_INVALID", "Evidence requires a nonempty exact source quote")
            unit = catalog[key]
            # Repeated exact wording is valid evidence at each visible occurrence.
            # Never choose an arbitrary occurrence or authorize unseen source text.
            verified = []
            for part in unit_visible_parts(unit, chunks):
                offset = 0
                while (found := part["text"].find(quote, offset)) >= 0:
                    start = part["unit_range"][0] + found
                    verified.extend(unit_range_evidence(unit, start, start + len(quote), chunks, role=role))
                    offset = found + 1
            if not verified:
                raise KGError(
                    "EVIDENCE_NOT_FOUND", "The exact quote does not occur in the supplied visible source unit"
                )
        else:
            verified = _legacy_evidence(reference, chunks, role)
        for item in verified:
            position = canonical(evidence_positions([item]))
            if position not in seen:
                seen.add(position)
                output.append(item)
    return output
