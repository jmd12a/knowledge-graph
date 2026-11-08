"""按字段修复证据，并在确有续段时按来源窗口补全。"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass

from pydantic import Field, StrictBool, StrictInt, ValidationError

from ...domain import KGError, StrictModel
from .extraction_batches import ExtractionBatch, request_fits
from .extraction_windows import open_source_edges
from .literal import SourceSelection, ground_literal_field, normalize_candidate_literals
from .source_references import evidence_positions, unit_visible_pieces, verify_evidence

_REPAIR_SYSTEM = """Correct the evidence for the listed fields only. Return patches by task_id.
Use selections [{unit_key,quote}] from the supplied rule_units. For a literal field,
{unit_key} selects an entire visible block. Do not change a business value or add fields.
Return an empty patches list for items with no supported correction."""
_COMPLETE_SYSTEM = """Complete only the listed literal fields. Read the supplied source windows and return
patches by task_id with ordered selections [{unit_key,quote}] or whole blocks [{unit_key}].
Keep the original supported text, including conditions and exceptions. The revised text must
extend that same clause with newly supplied source. Do not merge neighboring clauses. Use only the task's allowed_units.
Set complete=true only if the clause wording is complete. Omit unresolved items."""


class GroundingPatch(StrictModel):
    task_id: StrictInt = Field(ge=0)
    selections: list[SourceSelection] = Field(min_length=1)
    complete: StrictBool = False


class GroundingResult(StrictModel):
    patches: list[GroundingPatch]


@dataclass
class EvidenceField:
    target: dict
    label: str
    evidence_key: str = "evidence"
    field: str | None = None
    value: object = None
    literal_values: str | None = None
    rule_units: list[str] | None = None

    def verify(self, scope, chunks, catalog):
        if self.literal_values is not None:
            ground_literal_field(
                self.target,
                self.field,
                self.literal_values,
                catalog,
                chunks,
                scope["input_unit_keys"],
                fallback_units=self.rule_units,
                allow_open_edges=self.literal_values == "fields",
            )
        else:
            evidence = self.target.get(self.evidence_key, {} if self.field is not None else [])
            verify_evidence(
                evidence.get(self.field, []) if self.field is not None else evidence,
                chunks,
                units=catalog,
                input_unit_keys=scope["input_unit_keys"],
            )

    def replace(self, selections, scope, chunks, catalog):
        original = self.target
        revised = original.copy()
        if self.literal_values is not None:
            for name in (self.literal_values, "evidence", "literal_parts", "literal_provenance"):
                revised[name] = original.get(name, {}).copy()
            revised["literal_parts"][self.field] = selections
            revised["literal_provenance"].pop(self.field, None)
        elif self.field is None:
            revised[self.evidence_key] = selections
        else:
            revised[self.evidence_key] = original.get(self.evidence_key, {}) | {self.field: selections}
        self.target = revised
        try:
            self.verify(scope, chunks, catalog)
        finally:
            self.target = original
        original.update(revised)


def _entity_fields(entity, definition):
    literal = {key for key, prop in definition.properties.items() if prop.source_mode == "literal"}
    for field in sorted(entity["properties"].keys() | entity.get("literal_parts", {}).keys()):
        yield EvidenceField(
            entity,
            f"entity {entity['ref']}.{field}",
            field=field,
            value=entity["properties"].get(field),
            literal_values="properties" if field in literal else None,
        )
    if entity.get("mention"):
        yield EvidenceField(
            entity, f"entity {entity['ref']}.mention", "mention_evidence", value=entity["mention"]
        )
    if equivalent := entity.get("identity_equivalence"):
        yield EvidenceField(
            equivalent, f"entity {entity['ref']}.identity_equivalence", value=equivalent["identity_fields"]
        )
    groups = {group.name: group for group in definition.atomic_groups}
    for name, declaration in entity.get("atomic_groups", {}).items():
        if name not in groups:
            continue
        for field in groups[name].fields:
            if field not in literal and field not in declaration["fields"]:
                continue
            yield EvidenceField(
                declaration,
                f"entity {entity['ref']}.{name}.{field}",
                field=field,
                value=declaration["fields"].get(field),
                literal_values="fields" if field in literal else None,
                rule_units=declaration["rule_units"] if field in literal else None,
            )


async def _patches(language, system, scope, chunks, tasks, checkpoint, persist, reuse, issues):
    batch = ExtractionBatch(scope, chunks, {}, scope_policy="structure")
    request = {"rule_units": batch.request["rule_units"], "tasks": tasks}
    schema = GroundingResult.model_json_schema()
    request_key = batch.request_key(language, system, schema, request=request)
    cache_key = f"grounding/{request_key}"
    saved = checkpoint.get(cache_key)
    if saved is None:
        if not request_fits(language, system, request, schema):
            issues.append({"reason_code": "GROUNDING_INPUT_TOO_LARGE", "details": {"tasks": len(tasks)}})
            return {}
        result = reuse.get(request_key)
        try:
            if result is None:
                result = await language.json(system, request, schema)
            result = GroundingResult.model_validate(result).model_dump()
        except KGError as exc:
            if exc.code == "MODEL_QUOTA_EXHAUSTED" or exc.retryable:
                raise
            issues.append({"reason_code": exc.code, "details": {"tasks": len(tasks), "stage": "grounding"}})
            return {}
        except ValidationError:
            issues.append({"reason_code": "INVALID_MODEL_RESPONSE", "details": {"stage": "grounding"}})
            return {}
        saved = checkpoint[cache_key] = {"request_key": request_key, "result": result}
        await persist()
    if saved.get("request_key") != request_key:
        raise KGError("PREPARATION_SCOPE_UNAVAILABLE", "Saved evidence patch has a different request")
    try:
        patches = GroundingResult.model_validate(saved["result"]).patches
    except (ValidationError, KeyError) as exc:
        raise KGError(
            "PREPARATION_SCOPE_UNAVAILABLE", "Saved evidence patch has an invalid structure"
        ) from exc
    counts = Counter(patch.task_id for patch in patches)
    return {
        patch.task_id: (
            patch.complete,
            [
                part.model_dump(exclude_none=True) | {"unit_key": batch.unit_keys[part.unit_key]}
                for part in patch.selections
            ],
        )
        for patch in patches
        if counts[patch.task_id] == 1
        and patch.task_id < len(tasks)
        and all(part.unit_key in batch.unit_keys for part in patch.selections)
    }


async def repair_window(language, data, scope, chunks, catalog, model, checkpoint, persist, reuse, issues):
    local = {key: chunks[key] for key in scope["input_chunk_ids"]}
    fields = [
        field
        for entity in data["entities"]
        if (definition := model.node_types.get(entity["type"]))
        for field in _entity_fields(entity, definition)
    ]
    for relation in data["relations"]:
        label = f"relation {relation['type']} {relation['source_ref']} {relation['target_ref']}"
        fields.append(EvidenceField(relation, label))
        fields.extend(
            EvidenceField(relation, f"{label}.{field}", "property_evidence", field, value)
            for field, value in relation["properties"].items()
        )
    tasks, invalid = [], []
    local_keys = {key: f"unit/{i}" for i, key in enumerate(scope["input_unit_keys"])}
    for field in fields:
        try:
            field.verify(scope, local, catalog)
        except KGError as exc:
            tasks.append(
                {
                    "task_id": len(tasks),
                    "field": field.label,
                    "value": field.value,
                    "error": exc.code,
                    "literal": field.literal_values is not None,
                    "declared_units": [
                        local_keys[key] for key in field.rule_units or [] if key in local_keys
                    ],
                }
            )
            invalid.append(field)
    if tasks:
        patches = await _patches(
            language, _REPAIR_SYSTEM, scope, chunks, tasks, checkpoint, persist, reuse, issues
        )
        for task_id, (_, selections) in patches.items():
            field = invalid[task_id]
            if field.literal_values is None and any(not part.get("quote") for part in selections):
                continue
            if field.rule_units and any(part["unit_key"] not in field.rule_units for part in selections):
                continue
            try:
                field.replace(selections, scope, local, catalog)
            except KGError as exc:
                issues.append(
                    {"reason_code": exc.code, "details": {"field": field.label, "stage": "grounding"}}
                )

    for entity in data["entities"]:
        definition = model.node_types.get(entity["type"])
        if definition is None:
            continue
        try:
            normalize_candidate_literals(entity, definition, catalog, local, scope["input_unit_keys"])
        except KGError:
            # 对齐阶段按原来的字段和坐标记录未能采纳的原因。
            continue
    return not tasks


def _positions(evidence):
    return {
        (item["document_id"], item["origin"], item["coordinate"]): item["ranges"]
        for item in evidence_positions(evidence)
    }


def _contains(outer, inner):
    return all(
        any(left <= start < end <= right for left, right in outer.get(anchor, []))
        for anchor, ranges in inner.items()
        for start, end in ranges
    )


def _document_source(chunks):
    return _positions(
        [
            {
                "document_id": chunk["document_id"],
                "source_spans": [span for span in chunk["source_spans"] if span["role"] == "source"],
            }
            for chunk in chunks.values()
        ]
    )


def _completion_groups(data, scopes, chunks, catalog, model):
    groups = defaultdict(list)
    views_by_batch = {
        scope["key"]: {view["unit_key"]: view for view in scope["unit_views"]} for scope in scopes
    }
    pieces_by_unit = {}
    chunk_order = {key: i for i, key in enumerate(chunks)}
    for candidate in data["entities"]:
        definition = model.node_types.get(candidate["type"])
        if definition is None:
            continue
        known_groups = {group.name for group in definition.atomic_groups}
        batch = "/".join(candidate["ref"].split("/", 2)[:2])
        views = views_by_batch[batch]
        for name, declaration in candidate.get("atomic_groups", {}).items():
            if name not in known_groups:
                continue
            fields = {
                field: edges
                for field, parts in declaration["literal_parts"].items()
                if (edges := open_source_edges(parts, views, catalog))
            }
            if not fields:
                continue
            declaration["complete"] = False
            allowed = set(declaration["rule_units"])
            if not allowed or allowed - views.keys():
                continue
            original_chunks = {key: chunks[key] for key in candidate["input_chunk_ids"]}
            needed = set()
            for field, edges in fields.items():
                needed.update(ref["chunk_id"] for ref in declaration["evidence"][field])
                for key, offset in edges:
                    if key not in pieces_by_unit:
                        pieces_by_unit[key] = unit_visible_pieces(catalog[key], chunks)
                    needed.update(
                        piece["chunk_id"]
                        for piece in pieces_by_unit[key]
                        if piece["unit_range"][0] <= offset < piece["unit_range"][1]
                    )
            if needed <= original_chunks.keys():
                continue
            window = tuple(sorted(needed, key=chunk_order.__getitem__))
            groups[window].append((candidate, name, declaration, sorted(fields), definition))
    return groups


def _complete_field(revised, declaration, field, patch, original_chunks, scope, chunks, catalog, views):
    if not patch or not patch[0]:
        return False
    parts = patch[1]
    if any(part["unit_key"] not in declaration["rule_units"] for part in parts):
        return False
    revised["literal_parts"][field] = parts
    revised.get("literal_provenance", {}).pop(field, None)
    old = _positions(verify_evidence(declaration["evidence"][field], original_chunks))
    new = ground_literal_field(revised, field, "fields", catalog, chunks, scope["input_unit_keys"])
    proposed = _positions(verify_evidence(new["evidence"], chunks))
    return (
        bool(old)
        and _contains(proposed, old)
        and not _contains(_document_source(original_chunks), proposed)
        and not open_source_edges(revised["literal_parts"][field], views, catalog)
    )


def _preserve_unpatched_evidence(declaration, definition, name, patched, chunks, catalog, units):
    def fixed(refs):
        return [
            {
                "chunk_id": item["chunk_id"],
                "start": item["chunk_range"][0],
                "end": item["chunk_range"][1],
                "quote": chunks[item["chunk_id"]]["text"][slice(*item["chunk_range"])],
            }
            for item in verify_evidence(refs, chunks, units=catalog, input_unit_keys=units)
        ]

    fields = next(group.fields for group in definition.atomic_groups if group.name == name)
    for field in fields:
        if field in patched:
            continue
        if definition.properties[field].source_mode == "literal":
            ground_literal_field(
                declaration,
                field,
                "fields",
                catalog,
                chunks,
                units,
                fallback_units=declaration["rule_units"],
            )
        else:
            declaration["evidence"][field] = fixed(declaration["evidence"].get(field, []))
    for i, reference in enumerate(declaration["resolved_references"]):
        try:
            declaration["resolved_references"][i] = {
                side: fixed(reference[side]) for side in ("reference", "target")
            }
        except KGError:
            declaration["resolved_references"][i] = {"reference": [], "target": []}


async def complete_windows(
    language, data, scopes, chunks, catalog, model, make_scope, checkpoint, persist, reuse
):
    for window, pending in _completion_groups(data, scopes, chunks, catalog, model).items():
        local = {key: chunks[key] for key in window}
        scope = make_scope(local, list(catalog.values()))
        views = {view["unit_key"]: view for view in scope["unit_views"]}
        aliases = {key: f"unit/{i}" for i, key in enumerate(scope["input_unit_keys"])}
        tasks = []
        for candidate, name, declaration, fields, _ in pending:
            for field in fields:
                tasks.append(
                    {
                        "task_id": len(tasks),
                        "entity_type": candidate["type"],
                        "atomic_group": name,
                        "field": field,
                        "original_parts": [
                            part | {"unit_key": aliases[part["unit_key"]]}
                            for part in declaration["literal_parts"][field]
                            if part["unit_key"] in aliases
                        ],
                        "allowed_units": [
                            aliases[key] for key in declaration["rule_units"] if key in aliases
                        ],
                    }
                )
        patches = await _patches(
            language, _COMPLETE_SYSTEM, scope, chunks, tasks, checkpoint, persist, reuse, data["issues"]
        )
        index = 0
        for candidate, name, declaration, fields, definition in pending:
            revised = deepcopy(declaration)
            original_chunks = {key: chunks[key] for key in candidate["input_chunk_ids"]}
            accepted = True
            for field in fields:
                patch = patches.get(index)
                index += 1
                try:
                    accepted &= _complete_field(
                        revised, declaration, field, patch, original_chunks, scope, local, catalog, views
                    )
                except KGError:
                    accepted = False
            if accepted:
                authorized = {
                    "input_chunk_ids": list(dict.fromkeys(candidate["input_chunk_ids"] + list(local))),
                    "input_unit_keys": list(
                        dict.fromkeys(candidate["input_unit_keys"] + scope["input_unit_keys"])
                    ),
                }
                revised["complete"] = True
                proposed = deepcopy(candidate)
                proposed["atomic_groups"] = {name: revised}
                try:
                    _preserve_unpatched_evidence(
                        revised,
                        definition,
                        name,
                        fields,
                        original_chunks,
                        catalog,
                        candidate["input_unit_keys"],
                    )
                    normalize_candidate_literals(
                        proposed,
                        definition,
                        catalog,
                        {key: chunks[key] for key in authorized["input_chunk_ids"]},
                        authorized["input_unit_keys"],
                    )
                except KGError:
                    accepted = False
                else:
                    candidate["atomic_groups"][name] = proposed["atomic_groups"][name]
                    candidate.setdefault("atomic_group_scopes", {})[name] = authorized
            if not accepted:
                data["issues"].append(
                    {
                        "reason_code": "RULE_ASSEMBLY_UNRESOLVED",
                        "candidate": {"ref": candidate["ref"], "atomic_group": name},
                        "details": {"unit_keys": scope["input_unit_keys"]},
                    }
                )
