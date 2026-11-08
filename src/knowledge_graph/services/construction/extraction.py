from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import date
from typing import Any

from pydantic import Field, StrictBool, StrictInt, ValidationError

from ...domain import KGError, StrictModel, canonical
from ...domain.model import NodeType, identity_value
from ...domain.source import DEFAULT_STRUCTURE_WINDOW_CHARS
from ...prompts.extraction import EXTRACTION_SYSTEM, RULE_ASSEMBLY_SYSTEM, STRUCTURE_EXTRACTION_SYSTEM
from .alignment import _atomic_statement
from .extraction_batches import ExtractionBatch, chapter_groups, request_fits
from .extraction_grounding import complete_windows, repair_window
from .extraction_windows import plan_source_windows, source_block_ranges
from .literal import SourceSelection, normalize_candidate_literals
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


class LiteralPart(StrictModel):
    unit_key: str = Field(min_length=1, description="Visible source-unit key supplied in this call.")
    start: StrictInt = Field(description="Start in the source unit's text, inclusive.")
    end: StrictInt = Field(description="End in the source unit's text, exclusive.")


class IdentityEquivalence(StrictModel):
    identity_fields: dict[str, Any] = Field(
        description="Complete identity values explicitly equated with the current mention in source text."
    )
    evidence: list[InputEvidence] = Field(min_length=1)


class AtomicDeclaration(StrictModel):
    fields: dict[str, Any] = Field(
        description="Configured atomic-group field names mapped to raw business values."
    )
    evidence: dict[str, list[InputEvidence]] = Field(
        description="Each atomic field name mapped to its exact source evidence."
    )
    literal_parts: dict[str, list[LiteralPart]] = Field(
        default_factory=dict,
        description="Ordered source-unit ranges for fields configured with source_mode=literal.",
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
    literal_parts: dict[str, list[LiteralPart]] = Field(
        default_factory=dict,
        description="Ordered source-unit ranges for non-atomic fields configured as literal.",
    )
    identity_only: StrictBool = False
    mention: str | None = None
    mention_evidence: list[InputEvidence] = Field(default_factory=list)
    identity_equivalence: IdentityEquivalence | None = None
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


class StructuredReference(ResolvedReference):
    reference: list[UnitEvidenceRef] = Field(min_length=1)
    target: list[UnitEvidenceRef] = Field(min_length=1)


class StructuredIdentityEquivalence(IdentityEquivalence):
    evidence: list[UnitEvidenceRef] = Field(min_length=1)


class StructuredAtomicDeclaration(AtomicDeclaration):
    evidence: dict[str, list[UnitEvidenceRef]]
    section_evidence: list[UnitEvidenceRef] = Field(default_factory=list)
    resolved_references: list[StructuredReference] = Field(default_factory=list)
    literal_parts: dict[str, list[SourceSelection]] = Field(default_factory=dict)


class StructuredEntityCandidate(EntityCandidate):
    evidence: dict[str, list[UnitEvidenceRef]]
    mention_evidence: list[UnitEvidenceRef] = Field(default_factory=list)
    identity_equivalence: StructuredIdentityEquivalence | None = None
    literal_parts: dict[str, list[SourceSelection]] = Field(default_factory=dict)
    atomic_groups: dict[str, StructuredAtomicDeclaration] = Field(default_factory=dict)


class StructuredRelationCandidate(RelationCandidate):
    evidence: list[UnitEvidenceRef]
    property_evidence: dict[str, list[UnitEvidenceRef]] = Field(default_factory=dict)


class StructuredExtraction(Extraction):
    entities: list[StructuredEntityCandidate]
    relations: list[StructuredRelationCandidate]


def _validate_output(value, stage, output_type=Extraction):
    try:
        return output_type.model_validate(value).model_dump()
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


def build_rule_context(parsed, *, structured=False):
    """把完整来源段落及其续段提供给抽取流程。"""
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
                    "coordinate": key,
                    "pieces": [],
                    "boundaries": set(),
                },
            )
            entry["pieces"].append((start, end, text))
            native_section = origin["format"] == "md" or (
                origin["format"] == "docx" and "paragraph" in origin and chunk.get("section_path")
            )
            if structured and native_section and a == 0 and chunk.get("block_range", [None])[0] == 0:
                entry["boundaries"].add(start)
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
        origin = json.loads(anchor) if structured else {}
        ranges = (
            source_block_ranges(
                text,
                inline_headings=origin["format"] in {"txt", "pdf"} or "paragraph" in origin,
                parser_boundaries=tuple(position - base for position in source["boundaries"]),
            )
            if structured
            else [match.span() for match in re.finditer(r"[\s\S]+?(?:\r?\n[ \t]*\r?\n+|$)", text)]
        )
        for left, right in ranges:
            body = text[left:right]
            region = {
                "origin": anchor,
                "coordinate": source["coordinate"],
                "range": [base + left, base + right],
            }
            if units and base + left not in source["boundaries"] and _CONTINUATION.match(body.lstrip()):
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
                    if a >= b:
                        next_remaining.append([left, right])
                        continue
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


def _check_scope(scope, chunks, context) -> None:
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


def _plan_scopes(
    language,
    chunks,
    context,
    model_data,
    schema,
    *,
    scope_policy="whole",
    structure_window_chars: int = DEFAULT_STRUCTURE_WINDOW_CHARS,
):
    system = STRUCTURE_EXTRACTION_SYSTEM if scope_policy == "structure" else EXTRACTION_SYSTEM

    def fits_budget(batch_chunks, scope):
        request = ExtractionBatch(scope, batch_chunks, model_data, scope_policy=scope_policy).request
        return request_fits(language, system, request, schema)

    if scope_policy == "structure":
        whole_limit = min(structure_window_chars, language.profile.max_input_chars)
        if sum(len(unit["text"]) for unit in context) <= whole_limit:
            whole = _scope(chunks, context, key="batch/0")
            if fits_budget(chunks, whole):
                return [whole]
        return plan_source_windows(
            chunks,
            context,
            _scope,
            lambda scope: fits_budget(chunks, scope),
            target_source_chars=structure_window_chars,
        )

    # 单批能容纳全文时保留跨章节上下文；长文档再按章节独立装箱。
    if scope_policy != "whole":
        raise KGError("INVALID_ARGUMENT", "Unknown extraction strategy")
    if sum(len(chunk["text"]) for chunk in chunks.values()) <= language.profile.max_input_chars:
        whole = _scope(chunks, context, key="batch/0")
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
            if fits_budget(expanded_batch, _scope(expanded_batch, context_for_chunks(expanded_batch))):
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
            if fits_budget(expanded_batch, _scope(expanded_batch, context_for_chunks(expanded_batch))):
                batch_chunks = expanded_batch
        scope = _scope(batch_chunks, context_for_chunks(batch_chunks), key=f"batch/{position}")
        if not fits_budget(batch_chunks, scope):
            raise KGError("MODEL_INPUT_TOO_LARGE", "Source context exceeds the extraction budget")
        scopes.append(scope)
    return scopes


async def extract_text(
    language,
    parsed,
    model,
    checkpoint,
    persist,
    *,
    reuse=None,
    scope_policy="whole",
    structure_window_chars: int = DEFAULT_STRUCTURE_WINDOW_CHARS,
):
    """按持久化输入范围分批抽取，复用已校验的批次结果并组装完整规则。"""
    structured = scope_policy == "structure"
    output_type = StructuredExtraction if structured else Extraction
    system = STRUCTURE_EXTRACTION_SYSTEM if structured else EXTRACTION_SYSTEM
    if "rule_units" not in parsed:
        parsed["rule_units"] = build_rule_context(parsed, structured=structured)
    context = parsed["rule_units"]
    chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
    schema, model_data = output_type.model_json_schema(), model.model_dump()

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
            scopes = _plan_scopes(
                language,
                chunks,
                context,
                model_data,
                schema,
                scope_policy=scope_policy,
                structure_window_chars=structure_window_chars,
            )
        checkpoint["_input_scopes"] = {
            "version": 1 if cached else 2,
            "scope_policy": scope_policy,
            "batches": scopes,
        }
        await persist()
    else:
        if (
            not isinstance(saved, dict)
            or saved.get("version") not in (1, 2)
            or not isinstance(saved.get("batches"), list)
        ):
            raise KGError(
                "PREPARATION_SCOPE_UNAVAILABLE", "The saved extraction input plan has an invalid structure"
            )
        scopes = saved["batches"]
        if saved.get("scope_policy", "whole") != scope_policy:
            raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "Saved extraction strategy changed")
    if not scopes:
        raise KGError(
            "PREPARATION_SCOPE_UNAVAILABLE", "The saved extraction input plan has no source batches"
        )
    by_chunk = units_by_chunk(context, chunks) if structured else None
    for scope in scopes:
        _check_scope(scope, chunks, context)
        if structured:
            selected = set(scope["input_unit_keys"])
            if by_chunk is None:
                local = {key: chunks[key] for key in scope["input_chunk_ids"]}
                expected = set(_scope(local, context)["input_unit_keys"])
            else:
                expected = {context[i]["unit_key"] for key in scope["input_chunk_ids"] for i in by_chunk[key]}
            if selected != expected:
                raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "Saved source windows omit visible blocks")
    scoped_chunks = [key for scope in scopes for key in scope["input_chunk_ids"]]
    if set(scoped_chunks) != set(chunks):
        raise KGError(
            "PREPARATION_SCOPE_UNAVAILABLE",
            "Saved model input batches do not cover the parsed source",
        )
    expected_batches = {scope.get("key") for scope in scopes}
    if len(expected_batches) != len(scopes) or (
        {key for key in checkpoint if key.startswith("batch/")} - expected_batches
    ):
        raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "A cached model response lacks its saved input scope")

    local_refs = checkpoint["_input_scopes"]["version"] == 2
    reuse = {} if reuse is None else reuse
    combined = {"entities": [], "relations": [], "issues": []}
    catalog = {unit["unit_key"]: unit for unit in context}
    index = 0
    while index < len(scopes):
        scope = scopes[index]
        key = scope.get("key")
        valid_key = (
            isinstance(key, str) and re.fullmatch(r"batch/[0-9]+(?:\.[01])*", key)
            if structured
            else key == f"batch/{index}"
        )
        if not valid_key:
            raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "Saved extraction batch ownership is inconsistent")
        batch = ExtractionBatch(scope, chunks, model_data, scope_policy=scope_policy) if local_refs else None
        if key not in checkpoint:
            request = (
                batch.request
                if batch
                else {
                    "business_model": model_data,
                    "chunks": [chunks[item] for item in scope["input_chunk_ids"]],
                    "rule_units": scope["unit_views"],
                }
            )
            request_key = batch.request_key(language, system, schema) if batch else None
            result = reuse.get(request_key)
            reused = result is not None
            try:
                if not request_fits(language, system, request, schema):
                    raise KGError(
                        "MODEL_INPUT_TOO_LARGE", "The saved model input exceeds the current request budget"
                    )
                if not reused:
                    result = await language.json(system, request, schema)
            except KGError as exc:
                ids = scope["input_chunk_ids"]
                if (
                    not structured
                    or len(ids) < 2
                    or exc.code
                    not in {
                        "MODEL_INPUT_TOO_LARGE",
                        "MODEL_OUTPUT_TOO_LARGE",
                    }
                ):
                    raise
                # 先保存拆分结果，接管时只执行较小的窗口。
                middle = len(ids) // 2
                scopes[index : index + 1] = [
                    _scope({item: chunks[item] for item in half}, context, key=f"{key}.{side}")
                    for side, half in enumerate((ids[:middle], ids[middle:]))
                ]
                await persist()
                continue
            result = _validate_output(result, "Extraction", output_type)
            checkpoint[key] = (
                {"request_key": request_key, "result": result, "reused": reused} if batch else result
            )
            await persist()
        saved_batch = checkpoint[key]
        if batch and saved_batch.get("request_key") != batch.request_key(language, system, schema):
            raise KGError(
                "PREPARATION_SCOPE_UNAVAILABLE",
                "A saved extraction response has a different parser, prompt, schema, or model input",
            )
        data = _validate_output(
            saved_batch["result"] if batch else saved_batch, "Checkpoint extraction", output_type
        )
        if batch:
            data, all_known = batch.bind(data)
        if structured:
            valid_without_repair = await repair_window(
                language, data, scope, chunks, catalog, model, checkpoint, persist, reuse, combined["issues"]
            )
        else:
            repaired = await _repair_evidence(
                language, data, scope, chunks, context, model_data, schema, checkpoint, persist
            )
            valid_without_repair = repaired is data
            data = repaired
        if batch and all_known and valid_without_repair:
            saved_batch["reusable"] = True
            reuse[saved_batch["request_key"]] = saved_batch["result"]
        authorization = {field: list(scope[field]) for field in ("input_chunk_ids", "input_unit_keys")}
        # 局部引用加批次前缀，避免不同调用中同名 ref 相互连接。
        namespace = key + "/"
        for candidate in data["entities"]:
            visible = candidate if structured else _mark_partial_source(candidate, scope)
            combined["entities"].append(visible | {"ref": namespace + candidate["ref"], **authorization})
        for candidate in data["relations"]:
            combined["relations"].append(
                candidate
                | {
                    "source_ref": namespace + candidate["source_ref"],
                    "target_ref": namespace + candidate["target_ref"],
                    **authorization,
                }
            )
        index += 1
    if structured:
        await complete_windows(
            language, combined, scopes, chunks, catalog, model, _scope, checkpoint, persist, reuse
        )
    else:
        await _assemble_rules(language, parsed, model, combined, checkpoint, persist)
    if len(scopes) > 1:
        from .extraction_links import link_batches

        linked, issues = await link_batches(
            language,
            parsed,
            model,
            scopes,
            chunks,
            combined,
            checkpoint,
            persist,
            schema,
            lambda value, stage: _validate_output(value, stage, output_type),
            structured=structured,
            reuse=reuse,
        )
        combined["relations"].extend(linked)
        combined["issues"].extend(issues)
    return combined


async def _repair_evidence(language, data, scope, chunks, context, model_data, schema, checkpoint, persist):
    """只纠正无效引用，保留原响应中已通过校验的候选。"""
    local_chunks = {key: chunks[key] for key in scope["input_chunk_ids"]}
    failures, bad_entities, bad_relations = [], [], []

    def check(refs):
        verify_evidence(refs, local_chunks, units=context, input_unit_keys=scope["input_unit_keys"])

    def check_entity_evidence(candidate):
        definition = model_data["node_types"].get(candidate["type"])
        if definition is not None:
            normalize_candidate_literals(
                candidate,
                NodeType.model_validate(definition),
                {unit["unit_key"]: unit for unit in context},
                local_chunks,
                scope["input_unit_keys"],
            )
        for field in candidate["properties"]:
            check(candidate["evidence"].get(field, []))
        if candidate.get("mention"):
            check(candidate.get("mention_evidence", []))
        if candidate.get("identity_equivalence"):
            check(candidate["identity_equivalence"]["evidence"])
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
        if not request_fits(language, system, request, schema):
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


def _mark_partial_source(candidate, scope):
    """A model cannot certify a complete rule from an incomplete source-unit window."""
    partial = {view["unit_key"] for view in scope["unit_views"] if not view["fully_supplied"]}
    result = deepcopy(candidate)
    for declaration in result.get("atomic_groups", {}).values():
        used = set(declaration.get("rule_units", []))
        if declaration.get("section_key"):
            used.add(declaration["section_key"])
        if used & partial:
            declaration["complete"] = False
            result.setdefault("source_incomplete_unit_keys", []).extend(sorted(used & partial))
    return result


async def _assemble_rules(language, parsed, model, extracted, checkpoint, persist):
    """只在同一资料和可靠身份范围内组装局部规则声明。"""
    schema = Extraction.model_json_schema()
    units = {unit["unit_key"]: unit for unit in parsed.get("rule_units", [])}
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
            rule_keys = set(declaration.get("rule_units", []))
            if declaration.get("section_key") in units:
                rule_keys.add(declaration["section_key"])
            incomplete = rule_keys & set(candidate.get("source_incomplete_unit_keys", []))
            owners = incomplete or (rule_keys if declaration.get("segment_complete") is True else set())
            owners &= units.keys()
            if owners:
                owner_key = min(owners, key=lambda key: (units[key]["order"], key))
                grouped[(candidate["type"], canonical(identity), name, owner_key)].append(candidate)
    chunks = {chunk["chunk_id"]: chunk for chunk in parsed["chunks"]}
    for index, ((type_name, identity, group_name, owner_key), candidates) in enumerate(
        sorted(grouped.items())
    ):
        if len(candidates) < 2 and not any(
            owner_key in candidate.get("source_incomplete_unit_keys", []) for candidate in candidates
        ):
            continue
        selected = {
            key
            for candidate in candidates
            for key in candidate["atomic_groups"][group_name].get("rule_units", [])
        }
        selected.add(owner_key)
        if not selected or any(key not in units for key in selected):
            continue
        selected = sorted(selected, key=lambda key: units[key]["order"])
        orders = [units[key]["order"] for key in selected]
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
            "section_key": owner_key,
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
            "section_key": owner_key,
            "parts": candidates,
            "chunks": [chunks[key] for key in scope["input_chunk_ids"]],
            "rule_units": scope["unit_views"],
        }
        if cache_key not in checkpoint:
            if not request_fits(language, RULE_ASSEMBLY_SYSTEM, data, schema):
                request_chars = language.json_request_chars(RULE_ASSEMBLY_SYSTEM, data, schema)
                extracted.setdefault("issues", []).append(
                    {
                        "candidate": anchor,
                        "reason_code": "RULE_CONTEXT_BUDGET_EXCEEDED",
                        "details": {
                            "required_chars": request_chars,
                            "max_chars": language.profile.max_input_chars,
                        },
                    }
                )
                continue
            response = await language.json(RULE_ASSEMBLY_SYSTEM, data, schema)
            checkpoint[cache_key] = _validate_output(response, "Rule assembly")
            await persist()
        valid, rejected = [], []
        local_chunks = {key: chunks[key] for key in scope["input_chunk_ids"]}
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
            candidate = deepcopy(candidate)
            try:
                normalize_candidate_literals(
                    candidate,
                    model.node_types[type_name],
                    units,
                    local_chunks,
                    scope["input_unit_keys"],
                )
                declaration = candidate.get("atomic_groups", {}).get(group_name)
                if not declaration or declaration.get("complete") is not True:
                    continue
                cited_units = declaration.get("rule_units", [])
                if (
                    owner_key not in cited_units
                    or len(cited_units) != len(set(cited_units))
                    or not set(cited_units) <= set(scope["input_unit_keys"])
                ):
                    continue
                group = next(
                    group for group in model.node_types[type_name].atomic_groups if group.name == group_name
                )
                _atomic_statement(
                    candidate,
                    group,
                    model.node_types[type_name],
                    {"document_id": parsed["chunks"][0]["document_id"]},
                    local_chunks,
                    units,
                    model,
                    input_unit_keys=scope["input_unit_keys"],
                )
            except (KGError, KeyError, TypeError) as exc:
                rejected.append(exc.code if isinstance(exc, KGError) else type(exc).__name__)
                continue
            valid.append(declaration)
        # A section may contain separate subclauses with the same identity.
        if len(valid) == 1 and _same_assembly_source(candidates, group_name, units):
            for original in candidates:
                original["atomic_groups"][group_name] = deepcopy(valid[0])
                original.setdefault("atomic_group_scopes", {})[group_name] = {
                    key: list(scope[key]) for key in ("input_chunk_ids", "input_unit_keys")
                }
        else:
            extracted.setdefault("issues", []).append(
                {
                    "candidate": anchor,
                    "reason_code": "RULE_ASSEMBLY_UNRESOLVED",
                    "details": {
                        "candidate_count": len(candidates),
                        "valid_outputs": len(valid),
                        "rejected": rejected,
                    },
                }
            )


def _same_assembly_source(candidates, group_name, units):
    """Share an assembly only for duplicate claims with exactly the same source range."""
    if len(candidates) == 1:
        return True
    occupied, claims = [], []
    for candidate in candidates:
        declaration = candidate["atomic_groups"][group_name]
        by_field = declaration.get("literal_parts", {})
        parts = [part for values in by_field.values() for part in values]
        if not parts or any(part.get("unit_key") not in units for part in parts):
            return False
        occupied.append(canonical(by_field))
        claims.append(canonical(declaration["fields"]))
    return all(parts == occupied[0] for parts in occupied[1:]) and len(set(claims)) == 1
