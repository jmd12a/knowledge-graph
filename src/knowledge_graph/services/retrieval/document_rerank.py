"""按来源位置识别原文重排候选中的重复切片。"""

from __future__ import annotations

from ..construction.source_references import origin_key

type Locations = dict[str, tuple[tuple[str, str], int, int]]


def document_rerank_payload(data: dict, keys: dict[str, str], records: dict) -> tuple[dict, Locations]:
    sources, locations, candidates = {}, {}, []
    for candidate in data["candidates"]:
        key = keys[candidate["id"]]
        chunk = records[key]
        spans = chunk["source_spans"]
        # 只有完整且已经过原文校验的连续切片才可以按位置比较。
        if candidate["snippets"] and len(spans) == 1 and spans[0]["chunk_range"] == [0, len(chunk["text"])]:
            origin = spans[0]["origin"]
            if "char_range" in origin and "value_range" not in origin:
                source = (chunk["document_id"], origin_key(origin))
                start, end = origin["char_range"]
                local = sources.setdefault(source, len(sources) + 1)
                locations[key] = (source, start, end)
                candidate = candidate | {"location": [local, start, end]}
        candidates.append(candidate)
    return data | {
        "candidates": candidates,
        "ranking_limit": min(len(keys), 2 * data["final_limit"]),
    }, locations


def select_document_candidates(ranked: list[str], locations: Locations, limit: int) -> list[str]:
    selected, repeated, covered = [], [], []
    for key in ranked:
        location = locations.get(key)
        if location is not None:
            source, start, end = location
            # 完全包含才视为重复，保留部分重叠切片中的续段。
            if any(source == previous and left <= start < end <= right for previous, left, right in covered):
                repeated.append(key)
                continue
            covered.append(location)
        selected.append(key)
        if len(selected) == limit:
            break
    return (selected + repeated)[:limit]
