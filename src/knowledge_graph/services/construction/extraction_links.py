"""Bounded, source-authorized relation linking across extraction batches."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from hashlib import sha256

from ...domain import KGError, canonical
from ...domain.model import identity_value, is_alias_field
from ...prompts.extraction import CROSS_BATCH_LINK_SYSTEM
from .extraction_batches import ExtractionBatch, request_fits
from .source_references import verify_evidence


def _directory(parsed, model, scopes, combined, *, verified=False):
    document_id = parsed["chunks"][0]["document_id"]
    entries, refs = {}, {}
    ref_counts = Counter(candidate["ref"] for candidate in combined["entities"])
    chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
    for candidate in combined["entities"]:
        definition = model.node_types.get(candidate["type"])
        if definition is None or (verified and ref_counts[candidate["ref"]] != 1):
            continue
        try:
            identity = identity_value(definition, candidate["properties"], document_id)
            if verified:
                local = {key: chunks[key] for key in candidate["input_chunk_ids"]}
                for field in definition.identity.fields:
                    verify_evidence(
                        candidate["evidence"].get(field, []),
                        local,
                        units=parsed["rule_units"],
                        input_unit_keys=candidate["input_unit_keys"],
                    )
        except KGError:
            continue
        stable_id = "doc/" + sha256(canonical([candidate["type"], identity]).encode()).hexdigest()
        owner = next(
            (scope["key"] for scope in scopes if candidate["ref"].startswith(scope["key"] + "/")), None
        )
        if owner is None:
            continue
        if verified:
            names = _verified_names(candidate, definition, local, parsed["rule_units"])
            if not names:
                continue
        else:
            names = _candidate_names(candidate, definition)
        entry = entries.setdefault(
            stable_id,
            {
                "stable_id": stable_id,
                "type": candidate["type"],
                "name": str(candidate["properties"].get(definition.name_field, "")),
                "identity": identity,
                "batch_keys": set(),
                "surface_forms": set(),
            },
        )
        entry["batch_keys"].add(owner)
        entry["surface_forms"].update(
            value.strip() for value in names if isinstance(value, str) and len(value.strip()) >= 2
        )
        refs.setdefault(stable_id, candidate["ref"])
    for entry in entries.values():
        entry["surface_forms"] = sorted(entry["surface_forms"])
    return entries, refs


def _candidate_names(candidate, definition):
    names = []
    if candidate.get("evidence", {}).get(definition.name_field):
        names.append(candidate["properties"].get(definition.name_field))
    if candidate.get("mention_evidence"):
        names.append(candidate.get("mention"))
    for field, value in candidate["properties"].items():
        if is_alias_field(field) and candidate.get("evidence", {}).get(field):
            names.extend(value if isinstance(value, list) else [value])
    return names


def _verified_names(candidate, definition, chunks, units):
    sources = [(candidate.get("mention"), candidate.get("mention_evidence", []))]
    for field, value in candidate["properties"].items():
        if field == definition.name_field or is_alias_field(field):
            values = value if isinstance(value, list) else [value]
            sources.extend((name, candidate["evidence"].get(field, [])) for name in values)
    names = []
    for name, refs in sources:
        if not isinstance(name, str) or not name.strip() or not refs:
            continue
        for ref in refs:
            if not _mentioned(name.strip(), ref["quote"].casefold()):
                continue
            try:
                verify_evidence([ref], chunks, units=units, input_unit_keys=candidate["input_unit_keys"])
            except KGError:
                continue
            names.append(name)
            break
    return names


def _mentioned(name, visible):
    key = name.casefold()
    if name.isascii():
        return re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", visible) is not None
    return key in visible


async def link_batches(
    language,
    parsed,
    model,
    scopes,
    chunks,
    combined,
    checkpoint,
    persist,
    schema,
    validate,
    *,
    structured=False,
    reuse=None,
):
    """Link only cross-batch endpoint pairs with explicit mention in a supplied batch."""
    if len(scopes) < 2 or not model.relation_types:
        return [], []
    entries, refs = _directory(parsed, model, scopes, combined, verified=structured)
    system = CROSS_BATCH_LINK_SYSTEM
    if structured:
        system += "\nCite only rule_units using an exact quote and unit_key."
    by_batch = defaultdict(list)
    for entry in entries.values():
        for key in entry["batch_keys"]:
            by_batch[key].append(entry)
    relations, issues = [], []
    for scope in scopes:
        key = scope["key"]
        local = by_batch[key]
        if not local:
            continue
        local_chunks = {chunk_id: chunks[chunk_id] for chunk_id in scope["input_chunk_ids"]}
        texts = (
            (part["text"] for view in scope["unit_views"] for part in view["visible_parts"])
            if structured
            else (chunk["text"] for chunk in local_chunks.values())
        )
        visible = "\n".join(texts).casefold()
        pairs = set()
        for other in entries.values():
            if key in other["batch_keys"] or not any(
                (_mentioned(name, visible) if structured else name.casefold() in visible)
                for name in other["surface_forms"]
            ):
                continue
            for current in local:
                if current["stable_id"] == other["stable_id"]:
                    continue
                for relation_type, definition in model.relation_types.items():
                    if (
                        current["type"] in definition.source_types
                        and other["type"] in definition.target_types
                    ):
                        pairs.add((relation_type, current["stable_id"], other["stable_id"]))
                    if (
                        other["type"] in definition.source_types
                        and current["type"] in definition.target_types
                    ):
                        pairs.add((relation_type, other["stable_id"], current["stable_id"]))
        if not pairs:
            continue
        allowed = sorted(pairs)[:50]
        if len(pairs) > len(allowed):
            issues.append(
                {
                    "candidate": {"batch": key},
                    "reason_code": "LINK_CANDIDATE_BUDGET",
                    "details": {"candidate_pairs": len(pairs), "allowed_pairs": len(allowed)},
                }
            )
        batch = ExtractionBatch(scope, chunks, model.model_dump(), scope_policy="link", units_only=structured)
        directory_ids = {ref for _, source, target in allowed for ref in (source, target)}
        batch.request["endpoint_catalog"] = [
            {field: value for field, value in entries[stable_id].items() if field != "batch_keys"}
            for stable_id in sorted(directory_ids)
        ]
        batch.request["allowed_pairs"] = [
            {"type": relation_type, "source_ref": source, "target_ref": target}
            for relation_type, source, target in allowed
        ]
        request_key = batch.request_key(language, system, schema)
        cache_key = "link/" + key.split("/")[-1]
        if cache_key not in checkpoint:
            if not request_fits(language, system, batch.request, schema):
                issues.append(
                    {
                        "candidate": {"batch": key},
                        "reason_code": "LINK_INPUT_TOO_LARGE",
                        "details": {"candidate_pairs": len(allowed)},
                    }
                )
                continue
            try:
                response = (reuse or {}).get(request_key) if structured else None
                if response is None:
                    response = await language.json(system, batch.request, schema)
                response = validate(response, "Cross-batch link")
            except KGError as exc:
                if exc.code == "MODEL_QUOTA_EXHAUSTED" or (structured and exc.retryable):
                    raise
                issues.append({"candidate": {"batch": key}, "reason_code": exc.code, "details": {}})
                continue
            checkpoint[cache_key] = {
                "request_key": request_key,
                "input_scope": {
                    field: list(scope[field]) for field in ("input_chunk_ids", "input_unit_keys")
                },
                "result": response,
            }
            await persist()
        saved = checkpoint[cache_key]
        if saved.get("request_key") != request_key or saved.get("input_scope") != {
            field: list(scope[field]) for field in ("input_chunk_ids", "input_unit_keys")
        }:
            raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "Saved cross-batch link input changed")
        output = validate(saved["result"], "Checkpoint cross-batch link")
        if output["entities"]:
            issues.append(
                {"candidate": {"batch": key}, "reason_code": "LINK_NEW_ENTITY_FORBIDDEN", "details": {}}
            )
            continue
        bound, all_known = batch.bind(output)
        if not all_known:
            issues.append({"candidate": {"batch": key}, "reason_code": "EVIDENCE_NOT_FOUND", "details": {}})
            continue
        for relation in bound["relations"]:
            pair = (relation["type"], relation["source_ref"], relation["target_ref"])
            if pair not in pairs or pair not in allowed or relation.get("mapping_id"):
                issues.append(
                    {"candidate": relation, "reason_code": "INVALID_ENDPOINT", "details": {"batch": key}}
                )
                continue
            try:
                verify_evidence(
                    relation["evidence"],
                    local_chunks,
                    units=parsed["rule_units"],
                    input_unit_keys=scope["input_unit_keys"],
                )
                for field in relation["properties"]:
                    verify_evidence(
                        relation["property_evidence"].get(field, []),
                        local_chunks,
                        units=parsed["rule_units"],
                        input_unit_keys=scope["input_unit_keys"],
                    )
            except KGError as exc:
                issues.append({"candidate": relation, "reason_code": exc.code, "details": {"batch": key}})
                continue
            relations.append(
                relation
                | {
                    "source_ref": refs[relation["source_ref"]],
                    "target_ref": refs[relation["target_ref"]],
                    "input_chunk_ids": list(scope["input_chunk_ids"]),
                    "input_unit_keys": list(scope["input_unit_keys"]),
                }
            )
    return relations, issues
