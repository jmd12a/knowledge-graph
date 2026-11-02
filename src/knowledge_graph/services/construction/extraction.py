from __future__ import annotations

import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import date
from typing import Any

from pydantic import Field, StrictBool, StrictInt, ValidationError

from ...domain import KGError, StrictModel, canonical
from ...domain.model import identity_value
from ...prompts.extraction import EXTRACTION_SYSTEM, RULE_ASSEMBLY_SYSTEM
from .source_references import evidence_positions as evidence_positions
from .source_references import origin_key, unit_visible_parts, units_by_chunk
from .source_references import verify_evidence as verify_evidence


class EvidenceRef(StrictModel):
    chunk_id: str = Field(min_length=1, description="ID of a supplied source chunk.")
    start: StrictInt = Field(description="Zero-based Unicode code-point offset, inclusive.")
    end: StrictInt = Field(description="Unicode code-point offset, exclusive.")
    quote: str = Field(min_length=1, description="Exact chunk text between start and end, without rewriting.")


class UnitEvidenceRef(StrictModel):
    unit_key: str = Field(min_length=1, description="Exact key of a source unit supplied in this call.")
    quote: str = Field(
        min_length=1,
        description="Exact nonempty quote from the visible text of that supplied unit; no offsets are needed.",
    )


InputEvidence = UnitEvidenceRef | EvidenceRef


class ResolvedReference(StrictModel):
    reference: list[InputEvidence] = Field(
        min_length=1, description="Source text containing the cross-reference."
    )
    target: list[InputEvidence] = Field(
        min_length=1, description="Referenced content resolved in the same document."
    )


class AtomicDeclaration(StrictModel):
    fields: dict[str, Any] = Field(
        description="Configured atomic-group field names mapped to raw business values."
    )
    evidence: dict[str, list[InputEvidence]] = Field(
        description="Each atomic field name mapped to its exact source evidence."
    )
    complete: StrictBool = Field(
        default=False,
        description="True when the clause wording itself is complete; independent of reference expansion.",
    )
    references_resolved: StrictBool = Field(
        default=False,
        description=(
            "True means no required reference to another source passage remains unresolved. "
            "It is true when there are no such references; "
            "false when a target is unavailable; keep the source-backed clause regardless."
        ),
    )
    section_key: str = Field(
        min_length=1,
        pattern=r"\S",
        description="Required stable source-clause key; use the first supplied rule-unit key for this clause, including partial declarations.",
    )
    rule_units: list[str] = Field(
        default_factory=list,
        description="Supplied rule-unit keys supporting the extracted clause text; context may contain neighboring clauses.",
    )
    section_evidence: list[InputEvidence] = Field(
        default_factory=list,
        description=(
            "Optional model context citations retained for audit. "
            "The program derives complete context evidence from selected rule_units; do not recopy it here."
        ),
    )
    resolved_references: list[ResolvedReference] = Field(
        default_factory=list,
        description=(
            "Referring words and actual target-source evidence for required passage references. "
            "Empty when none exist; this does not make references_resolved false."
        ),
    )
    segment_complete: StrictBool = Field(
        default=False,
        description="True only if this supplied segment has been fully examined; does not imply a complete rule.",
    )


class EntityCandidate(StrictModel):
    ref: str = Field(
        min_length=1, description="Unique local string referenced by relation source_ref and target_ref."
    )
    type: str = Field(min_length=1, description="Configured node-type name.")
    properties: dict[str, Any] = Field(
        description=(
            "Configured property names mapped directly to raw values, including identity fields; "
            "no value/evidence wrappers."
        )
    )
    evidence: dict[str, list[InputEvidence]] = Field(
        description=(
            "Property name to source-evidence list; prefer unit_key plus exact quote, "
            "separate from the raw properties map."
        )
    )
    identity_only: StrictBool = False
    domain_ids: list[str] = Field(default_factory=list)
    atomic_groups: dict[str, AtomicDeclaration] = Field(
        default_factory=dict,
        description="Configured atomic-group name to a complete or explicitly incomplete source declaration.",
    )


class RelationCandidate(StrictModel):
    type: str = Field(min_length=1, description="Configured relation-type name.")
    source_ref: str = Field(
        min_length=1, description="Exact local entity ref string, never an identity or evidence object."
    )
    target_ref: str = Field(
        min_length=1, description="Exact local entity ref string, never an identity or evidence object."
    )
    properties: dict[str, Any] = Field(
        default_factory=dict, description="Relation property names mapped directly to raw business values."
    )
    evidence: list[InputEvidence] = Field(description="Source statements supporting this relation.")
    property_evidence: dict[str, list[InputEvidence]] = Field(
        default_factory=dict, description="Each relation property name mapped to its source evidence."
    )
    mapping_id: str | None = Field(
        default=None,
        description="Null for language extraction; only deterministic structured mappings assign authorization IDs.",
    )


class Extraction(StrictModel):
    entities: list[EntityCandidate]
    relations: list[RelationCandidate]


def _validate_output(value, stage):
    try:
        return Extraction.model_validate(value).model_dump()
    except ValidationError as exc:
        violations = [
            {"path": list(error["loc"]), "type": error["type"]}
            for error in exc.errors(include_input=False)[:20]
        ]
        raise KGError(
            "INVALID_MODEL_RESPONSE",
            f"{stage} response does not match the extraction output contract",
            503,
            validation_errors=violations,
            error_count=exc.error_count(),
        ) from exc


def cast(value, kind):
    if kind == "string":
        return value if isinstance(value, str) else canonical(value)
    if kind == "integer":
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ValueError("Not an integer")
        return int(value)
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError("Not a number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Not a finite number")
        return result
    if kind == "boolean":
        if value is True or str(value).lower() in {"true", "1"}:
            return True
        if value is False or str(value).lower() in {"false", "0"}:
            return False
        raise ValueError("Not a boolean")
    if kind == "date":
        return date.fromisoformat(str(value)).isoformat()
    raise ValueError("Unsupported cast")


def _mapped_fields(record, definition, property_defs, media_type):
    values, evidence = {}, {}
    for field, spec in definition.get("fields", {}).items():
        key = spec["column"] if media_type == "csv" else spec["pointer"]
        cell = record["fields"].get(key)
        if not cell or cell.get("missing") or cell.get("value") is None or cell["value"] == "":
            if spec.get("empty", "error") == "omit" and not property_defs[field].required:
                continue
            raise KGError(
                "MAPPING_FIELD_MISSING",
                "A mapped field is missing or empty",
                field=field,
                record=record["index"],
            )
        values[field] = cast(cell["value"], spec.get("cast", "string"))
        evidence[field] = cell.get("evidence", [])
    return values, evidence


def mapped_extraction(parsed, model):
    """按显式字段映射生成候选与证据，逐项保留映射失败原因。"""
    mapping_id = parsed["mapping_id"]
    mapping = model.mappings[mapping_id]
    entities, relations, problems = [], [], []
    for record in parsed["records"]:
        refs = {}
        for name, definition in mapping["entities"].items():
            ref = str(record["index"]) + "/" + name
            try:
                node = model.node_types[definition["type"]]
                values, evidence = _mapped_fields(record, definition, node.properties, mapping["format"])
                groups = {}
                for group in node.atomic_groups:
                    groups[group.name] = {
                        "fields": {field: values[field] for field in group.fields if field in values},
                        "complete": all(field in values for field in group.fields),
                        "references_resolved": True,
                        "section_key": str(record["index"]),
                        "mapping_id": mapping_id,
                        "record_index": record["index"],
                        "evidence": {field: evidence[field] for field in group.fields if field in evidence},
                    }
                entity = {
                    "ref": ref,
                    "type": definition["type"],
                    "properties": values,
                    "evidence": evidence,
                    "identity_only": definition.get("identity_only", False),
                    "atomic_groups": groups,
                    "domain_ids": [],
                    "mapping_id": mapping_id,
                }
                entities.append(entity)
                refs[name] = entity
            except (KGError, ValueError, TypeError, OverflowError) as exc:
                problems.append(
                    {
                        "candidate": {"ref": ref, "definition": definition},
                        "reason_code": getattr(exc, "code", "MAPPING_CAST_ERROR"),
                        "details": {"record": record["index"], "message": str(exc)},
                    }
                )
        for definition in mapping.get("relations", []):
            try:
                if definition["source_ref"] not in refs or definition["target_ref"] not in refs:
                    raise KGError("INVALID_ENDPOINT", "A mapped relation endpoint was not adopted")
                source, target = refs[definition["source_ref"]], refs[definition["target_ref"]]
                evidence = []
                for endpoint in (source, target):
                    for field in model.node_types[endpoint["type"]].identity.fields:
                        evidence.extend(endpoint["evidence"].get(field, []))
                values, supports = _mapped_fields(
                    record, definition, model.relation_types[definition["type"]].properties, mapping["format"]
                )
                relations.append(
                    {
                        "type": definition["type"],
                        "source_ref": source["ref"],
                        "target_ref": target["ref"],
                        "properties": values,
                        "evidence": evidence,
                        "property_evidence": supports,
                        "mapping_id": mapping_id,
                    }
                )
            except (KGError, ValueError, TypeError, OverflowError) as exc:
                problems.append(
                    {
                        "candidate": definition,
                        "reason_code": getattr(exc, "code", "MAPPING_CAST_ERROR"),
                        "details": {"record": record["index"], "message": str(exc)},
                    }
                )
    return {"entities": entities, "relations": relations}, problems


_CONTINUATION = re.compile(
    r"^(?:but\b|however\b|except\b|provided\b|unless\b|notwithstanding\b|subject\s+to\b|"
    r"for\s+the\s+avoidance\b|and\b|or\b|但|但是|然而|除外|除非|例外|前款|上述|并且|其中)",
    re.I,
)


def build_rule_context(parsed):
    """Expose complete source paragraphs, including continuation paragraphs, to extraction."""
    if parsed.get("mapping_id"):
        return []
    sources = {}
    chunks = parsed["chunks"]
    for chunk in chunks:
        for span in chunk["source_spans"]:
            if span.get("role") != "source":
                continue
            origin = span["origin"]
            key = "value_range" if "value_range" in origin else "char_range"
            a, b = span["chunk_range"]
            start, end = origin[key]
            text = chunk["text"][a:b]
            if len(text) != end - start:
                raise KGError("PARSE_FAILED", "Source span has a non-reversible coordinate mapping")
            anchor = origin_key(origin)
            entry = sources.setdefault(
                anchor,
                {
                    "origin": {k: v for k, v in origin.items() if k not in {key, "line_range"}},
                    "coordinate": key,
                    "pieces": [],
                },
            )
            entry["pieces"].append((start, end, text))
    units = []
    for anchor, source in sources.items():
        text, base, cursor = "", None, None
        for start, end, piece in sorted(source["pieces"]):
            if base is None:
                base, cursor = start, start
            if start > cursor:
                raise KGError("PARSE_FAILED", "Source context has an unmapped gap")
            overlap = min(cursor - start, len(piece))
            if overlap and text[start - base : start - base + overlap] != piece[:overlap]:
                raise KGError("PARSE_FAILED", "Overlapping source windows disagree")
            if end > cursor:
                text += piece[overlap:]
                cursor = end
        paragraphs = list(re.finditer(r"[\s\S]+?(?:\r?\n[ \t]*\r?\n+|$)", text))
        for paragraph in paragraphs:
            body = paragraph.group()
            if not body:
                continue
            region = {
                "origin": anchor,
                "coordinate": source["coordinate"],
                "range": [base + paragraph.start(), base + paragraph.end()],
            }
            if units and _CONTINUATION.match(body.lstrip()):
                units[-1]["regions"].append(region)
                units[-1]["text"] += body
            else:
                units.append(
                    {
                        "unit_key": "rule-unit/" + str(len(units)),
                        "order": len(units),
                        "regions": [region],
                        "text": body,
                    }
                )
    for unit in units:
        unit["evidence"] = _region_references(unit["regions"], chunks)
    return units


def _region_references(regions, chunks):
    references = []
    for region in regions:
        remaining = [region["range"][:]]
        for chunk in chunks:
            for span in chunk["source_spans"]:
                if span.get("role") != "source" or origin_key(span["origin"]) != region["origin"]:
                    continue
                key = region["coordinate"]
                start, end = span["origin"][key]
                next_remaining = []
                for left, right in remaining:
                    a, b = max(left, start), min(right, end)
                    if a < b:
                        begin = span["chunk_range"][0] + a - start
                        finish = begin + b - a
                        references.append(
                            {
                                "chunk_id": chunk["chunk_id"],
                                "start": begin,
                                "end": finish,
                                "quote": chunk["text"][begin:finish],
                            }
                        )
                        if left < a:
                            next_remaining.append([left, a])
                        if b < right:
                            next_remaining.append([b, right])
                    else:
                        next_remaining.append([left, right])
                remaining = next_remaining
        if remaining:
            raise KGError("PARSE_FAILED", "A complete rule context cannot be mapped to chunks")
    return references


def _unit_prompt(unit, chunks):
    parts = unit_visible_parts(unit, chunks)
    return {
        "unit_key": unit["unit_key"],
        "order": unit["order"],
        "visible_parts": parts,
        "fully_supplied": len(parts) == 1 and parts[0]["unit_range"] == [0, len(unit["text"])],
    }


def _scope(chunks, context, *, key=None):
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


def _check_scope(scope, chunks, context):
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
    return scope


def _plan_scopes(language, chunks, context, model_data, schema):
    index = units_by_chunk(context, chunks)

    def local_context(group):
        if index is None:
            return context
        selected = set().union(*(index[chunk_id] for chunk_id in group))
        return [context[position] for position in sorted(selected)]

    groups, batch = [], {}
    for chunk_id, chunk in chunks.items():
        proposed = batch | {chunk_id: chunk}
        scope = _scope(proposed, local_context(proposed))
        request = {
            "business_model": model_data,
            "chunks": list(proposed.values()),
            "rule_units": scope["unit_views"],
        }
        if language.json_request_chars(EXTRACTION_SYSTEM, request, schema) > language.profile.max_input_chars:
            if not batch:
                raise KGError(
                    "MODEL_INPUT_TOO_LARGE", "One source chunk and model exceed the extraction budget"
                )
            groups.append(batch)
            batch = {chunk_id: chunk}
        else:
            batch = proposed
    if batch:
        groups.append(batch)
    # Include complete boundary units when they fit. Shared chunks may appear in
    # adjacent batches; persisted per-call scopes still define evidence authority.
    for position, group in enumerate(groups):
        for unit in local_context(group):
            required = {ref["chunk_id"]: chunks[ref["chunk_id"]] for ref in unit["evidence"]}
            if required.keys() <= group.keys():
                continue
            proposed = group | required
            scope = _scope(proposed, local_context(proposed))
            request = {
                "business_model": model_data,
                "chunks": list(proposed.values()),
                "rule_units": scope["unit_views"],
            }
            if (
                language.json_request_chars(EXTRACTION_SYSTEM, request, schema)
                <= language.profile.max_input_chars
            ):
                group = proposed
        groups[position] = group
    scopes = [
        _scope(group, local_context(group), key="batch/" + str(index)) for index, group in enumerate(groups)
    ]
    for scope in scopes:
        request = {
            "business_model": model_data,
            "chunks": [chunks[key] for key in scope["input_chunk_ids"]],
            "rule_units": scope["unit_views"],
        }
        if language.json_request_chars(EXTRACTION_SYSTEM, request, schema) > language.profile.max_input_chars:
            raise KGError("MODEL_INPUT_TOO_LARGE", "Source context exceeds the extraction budget")
    return scopes


async def extract_text(language, parsed, model, checkpoint, persist):
    """按持久化输入范围分批抽取，复用已校验的批次结果并组装完整规则。"""
    context = parsed.setdefault("rule_units", build_rule_context(parsed))
    chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
    schema, model_data = Extraction.model_json_schema(), model.model_dump()

    # 输入范围与模型响应一起固定，恢复时不能让旧响应获得额外来源授权。
    saved = checkpoint.get("_input_scopes")
    if saved is None:
        cached = {key for key in checkpoint if key.startswith("batch/")}
        if cached:
            if len(chunks) != 1 or cached != {"batch/0"}:
                raise KGError(
                    "PREPARATION_SCOPE_UNAVAILABLE",
                    "An older multi-chunk checkpoint lacks its original model input scopes; retry preparation with a fresh baseline",
                )
            scopes = [_scope(chunks, context, key="batch/0")]
        else:
            scopes = _plan_scopes(language, chunks, context, model_data, schema)
        checkpoint["_input_scopes"] = {"version": 1, "batches": scopes}
        await persist()
    else:
        if (
            not isinstance(saved, dict)
            or saved.get("version") != 1
            or not isinstance(saved.get("batches"), list)
        ):
            raise KGError(
                "PREPARATION_SCOPE_UNAVAILABLE", "The saved extraction input plan has an invalid structure"
            )
        scopes = saved["batches"]
    if not scopes:
        raise KGError(
            "PREPARATION_SCOPE_UNAVAILABLE", "The saved extraction input plan has no source batches"
        )
    for scope in scopes:
        _check_scope(scope, chunks, context)
    scoped_chunks = [key for scope in scopes for key in scope["input_chunk_ids"]]
    if set(scoped_chunks) != set(chunks):
        raise KGError(
            "PREPARATION_SCOPE_UNAVAILABLE",
            "Saved model input batches do not cover the parsed source",
        )
    expected_batches = {scope.get("key") for scope in scopes}
    if {key for key in checkpoint if key.startswith("batch/")} - expected_batches:
        raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "A cached model response lacks its saved input scope")

    combined = {"entities": [], "relations": []}
    for index, scope in enumerate(scopes):
        _check_scope(scope, chunks, context)
        key = scope.get("key")
        if key != "batch/" + str(index):
            raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "Saved extraction batch ownership is inconsistent")
        if key not in checkpoint:
            request = {
                "business_model": model_data,
                "chunks": [chunks[item] for item in scope["input_chunk_ids"]],
                "rule_units": scope["unit_views"],
            }
            if (
                language.json_request_chars(EXTRACTION_SYSTEM, request, schema)
                > language.profile.max_input_chars
            ):
                raise KGError(
                    "MODEL_INPUT_TOO_LARGE", "The saved model input exceeds the current request budget"
                )
            checkpoint[key] = _validate_output(
                await language.json(EXTRACTION_SYSTEM, request, schema), "Extraction"
            )
            await persist()
        data = _validate_output(checkpoint[key], "Checkpoint extraction")
        data = await _repair_evidence(
            language, data, scope, chunks, context, model_data, schema, checkpoint, persist
        )
        authorization = {field: list(scope[field]) for field in ("input_chunk_ids", "input_unit_keys")}
        # 局部引用加批次前缀，避免不同调用中同名 ref 相互连接。
        namespace = key + "/"
        for candidate in data["entities"]:
            combined["entities"].append(candidate | {"ref": namespace + candidate["ref"], **authorization})
        for candidate in data["relations"]:
            combined["relations"].append(
                candidate
                | {
                    "source_ref": namespace + candidate["source_ref"],
                    "target_ref": namespace + candidate["target_ref"],
                    **authorization,
                }
            )
    await _assemble_rules(language, parsed, model, combined, checkpoint, persist)
    return combined


async def _repair_evidence(language, data, scope, chunks, context, model_data, schema, checkpoint, persist):
    """One targeted correction for invalid citations; preserve every valid original candidate."""
    local_chunks = {key: chunks[key] for key in scope["input_chunk_ids"]}
    failures, bad_entities, bad_relations = [], [], []

    def check(refs):
        verify_evidence(refs, local_chunks, units=context, input_unit_keys=scope["input_unit_keys"])

    def check_entity_evidence(candidate):
        for field in candidate["properties"]:
            check(candidate["evidence"].get(field, []))
        for declaration in candidate.get("atomic_groups", {}).values():
            for field in declaration["fields"]:
                check(declaration["evidence"].get(field, []))

    def check_relation_evidence(candidate):
        check(candidate["evidence"])
        for field in candidate["properties"]:
            check(candidate["property_evidence"].get(field, []))

    for candidate in data["entities"]:
        try:
            check_entity_evidence(candidate)
        except KGError as exc:
            bad_entities.append(candidate)
            failures.append({"ref": candidate["ref"], "code": exc.code, "message": str(exc)})
    for candidate in data["relations"]:
        try:
            check_relation_evidence(candidate)
        except KGError as exc:
            bad_relations.append(candidate)
            failures.append(
                {
                    "relation": [candidate["type"], candidate["source_ref"], candidate["target_ref"]],
                    "code": exc.code,
                    "message": str(exc),
                }
            )
    if not failures:
        return data
    cache_key = "evidence-repair/" + scope["key"]
    if cache_key not in checkpoint:
        request = {
            "business_model": model_data,
            "rule_units": scope["unit_views"],
            "chunks": list(local_chunks.values()),
            "repair_entities": bad_entities,
            "repair_relations": bad_relations,
            "available_endpoints": [
                {"ref": e["ref"], "type": e["type"], "properties": e["properties"]} for e in data["entities"]
            ],
            "validation_errors": failures,
        }
        system = (
            EXTRACTION_SYSTEM
            + "\nCorrect only the listed repair_entities and repair_relations using the original source. "
            "Preserve their ref, type, identity and endpoint refs. "
            "Return only corrected candidates in the two arrays. "
            "Never omit supported clause wording to shorten output; repair its citations. "
            "Do not return other endpoints or new facts."
        )
        if language.json_request_chars(system, request, schema) > language.profile.max_input_chars:
            checkpoint[cache_key] = {"entities": [], "relations": [], "skipped": "input_budget"}
        else:
            try:
                checkpoint[cache_key] = _validate_output(
                    await language.json(system, request, schema), "Evidence repair"
                )
            except KGError as exc:
                if exc.code == "MODEL_QUOTA_EXHAUSTED":
                    raise
                checkpoint[cache_key] = {"entities": [], "relations": [], "skipped": exc.code}
        await persist()
    repair = checkpoint[cache_key]
    originals = {e["ref"]: e for e in bad_entities}
    entities = {}
    for replacement in repair["entities"]:
        original = originals.get(replacement["ref"])
        if original is None or replacement["type"] != original["type"]:
            continue
        identity_fields = (
            model_data["node_types"].get(original["type"], {}).get("identity", {}).get("fields", [])
        )
        if any(
            replacement["properties"].get(field) != original["properties"].get(field)
            for field in identity_fields
        ):
            continue
        try:
            check_entity_evidence(replacement)
        except KGError:
            continue
        entities[replacement["ref"]] = replacement

    def relation_key(relation):
        return relation["type"], relation["source_ref"], relation["target_ref"]

    relations = {}
    for replacement in repair["relations"]:
        try:
            check_relation_evidence(replacement)
        except KGError:
            continue
        relations[relation_key(replacement)] = replacement
    bad_refs = {e["ref"] for e in bad_entities}
    bad_keys = {relation_key(r) for r in bad_relations}
    return {
        "entities": [entities.get(e["ref"], e) if e["ref"] in bad_refs else e for e in data["entities"]],
        "relations": [
            relations.get(relation_key(r), r) if relation_key(r) in bad_keys else r for r in data["relations"]
        ],
    }


async def _assemble_rules(language, parsed, model, extracted, checkpoint, persist):
    """Reconcile bounded partial declarations only within one document and reliable identity."""
    schema = Extraction.model_json_schema()
    grouped = defaultdict(list)
    for candidate in extracted["entities"]:
        definition = model.node_types.get(candidate["type"])
        if not definition or not definition.atomic_groups:
            continue
        try:
            identity = identity_value(definition, candidate["properties"], parsed["chunks"][0]["document_id"])
        except KGError:
            continue
        for name, declaration in candidate.get("atomic_groups", {}).items():
            if not isinstance(declaration, dict) or declaration.get("complete") is True:
                continue
            section_key = declaration.get("section_key")
            if section_key and declaration.get("segment_complete") is True:
                grouped[(candidate["type"], canonical(identity), name, section_key)].append(candidate)
    units = {unit["unit_key"]: unit for unit in parsed.get("rule_units", [])}
    chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
    for index, ((type_name, identity, group_name, section_key), candidates) in enumerate(
        sorted(grouped.items())
    ):
        if len(candidates) < 2:
            continue
        selected = {
            key
            for candidate in candidates
            for key in candidate["atomic_groups"][group_name].get("rule_units", [])
        }
        if not selected or any(key not in units for key in selected):
            continue
        selected = sorted(selected, key=lambda key: units[key]["order"])
        orders = sorted(units[key]["order"] for key in selected)
        if orders != list(range(orders[0], orders[-1] + 1)):
            continue
        selected_chunks = sorted({ref["chunk_id"] for key in selected for ref in units[key]["evidence"]})
        cache_key = "assembly/" + str(index)
        scopes = checkpoint.setdefault("_assembly_scopes", {})
        if not isinstance(scopes, dict):
            raise KGError(
                "PREPARATION_SCOPE_UNAVAILABLE", "Saved rule-assembly scopes have an invalid structure"
            )
        anchor = {
            "entity_type": type_name,
            "identity": identity,
            "atomic_group": group_name,
            "section_key": section_key,
        }
        scope = scopes.get(cache_key)
        if scope is None:
            if cache_key in checkpoint and len(chunks) != 1:
                raise KGError(
                    "PREPARATION_SCOPE_UNAVAILABLE",
                    "An older multi-chunk rule assembly lacks its original model input scope",
                )
            scope = (
                _scope({key: chunks[key] for key in selected_chunks}, [units[key] for key in selected])
                | anchor
            )
            scopes[cache_key] = scope
            await persist()
        _check_scope(scope, chunks, list(units.values()))
        if (
            any(scope.get(key) != value for key, value in anchor.items())
            or scope["input_unit_keys"] != selected
        ):
            raise KGError(
                "PREPARATION_SCOPE_UNAVAILABLE",
                "Saved rule-assembly ownership differs from its source declaration",
            )
        data = {
            "business_model": model.model_dump(),
            "entity_type": type_name,
            "identity": identity,
            "atomic_group": group_name,
            "section_key": section_key,
            "parts": candidates,
            "chunks": [chunks[key] for key in scope["input_chunk_ids"]],
            "rule_units": scope["unit_views"],
        }
        if cache_key not in checkpoint:
            request_chars = language.json_request_chars(RULE_ASSEMBLY_SYSTEM, data, schema)
            if request_chars > language.profile.max_input_chars:
                raise KGError(
                    "RULE_CONTEXT_BUDGET_EXCEEDED",
                    "The complete rule assembly exceeds the model input budget",
                    422,
                    section_key=section_key,
                    required_chars=request_chars,
                    max_chars=language.profile.max_input_chars,
                )
            response = await language.json(RULE_ASSEMBLY_SYSTEM, data, schema)
            checkpoint[cache_key] = _validate_output(response, "Rule assembly")
            await persist()
        for candidate in _validate_output(checkpoint[cache_key], "Checkpoint rule assembly")["entities"]:
            if candidate["type"] != type_name:
                continue
            try:
                candidate_identity = identity_value(
                    model.node_types[type_name], candidate["properties"], parsed["chunks"][0]["document_id"]
                )
            except KGError:
                continue
            if canonical(candidate_identity) != identity:
                continue
            # Preserve the original refs and endpoint relationships; replace only this atomic statement.
            declaration = candidate.get("atomic_groups", {}).get(group_name)
            if declaration:
                for original in candidates:
                    original["atomic_groups"][group_name] = deepcopy(declaration)
                    original.setdefault("atomic_group_scopes", {})[group_name] = {
                        key: list(scope[key]) for key in ("input_chunk_ids", "input_unit_keys")
                    }
