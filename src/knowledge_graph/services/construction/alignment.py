from __future__ import annotations

import re
from collections import Counter, defaultdict

from ...domain import KGError, canonical, check_value, identity_value, new_id, validate_identity_key
from .literal import normalize_candidate_literals
from .source_references import (
    evidence_positions,
    origin_key,
    unit_context_evidence,
    verify_evidence,
)


def _source_ranges(evidence):
    ranges = defaultdict(list)
    for item in evidence:
        for span in item["source_spans"]:
            origin = span["origin"]
            coordinate = "value_range" if "value_range" in origin else "char_range"
            ranges[(origin_key(origin), coordinate)].append(origin[coordinate])
    return ranges


def _covers(ranges, left, right):
    """判断可相接的来源区间能否无缺口覆盖目标区间。"""
    cursor = left
    for start, end in sorted(ranges):
        if start > cursor:
            break
        if end > cursor:
            cursor = end
        if cursor >= right:
            return True
    return False


def _input_scope(owner, chunks):
    """读取候选保存的模型输入范围，并检查引用的切片和来源单元。"""
    ids = owner.get("input_chunk_ids")
    if ids is None:
        local = chunks
    else:
        if not isinstance(ids, list) or any(not isinstance(key, str) or key not in chunks for key in ids):
            raise KGError(
                "EVIDENCE_NOT_FOUND", "The saved input scope references an unavailable source chunk"
            )
        local = {key: chunks[key] for key in ids}
    keys = owner.get("input_unit_keys")
    if keys is not None and (not isinstance(keys, list) or any(not isinstance(key, str) for key in keys)):
        raise KGError("EVIDENCE_INVALID", "The saved source-unit input scope is invalid")
    return local, keys


def _atomic_statement(
    candidate, group, definition, document, chunks, catalog, model, *, input_unit_keys=None
):
    """只校验字段、引用原文及输入授权；保留模型完整性判断，不据此否决文本。"""
    declaration = candidate.get("atomic_groups", {}).get(group.name)
    if (
        not isinstance(declaration, dict)
        or not isinstance(declaration.get("section_key"), str)
        or not declaration["section_key"].strip()
    ):
        raise KGError("RULE_INCOMPLETE", "Atomic rule declaration lacks a source key", group=group.name)
    fields = declaration.get("fields", {})
    if not isinstance(fields, dict) or set(fields) != set(group.fields):
        raise KGError(
            "RULE_INCOMPLETE",
            "Atomic declaration lacks its complete configured field group",
            group=group.name,
        )
    supports, per_field, verified_references = [], {}, []
    for field in group.fields:
        check_value(fields[field], definition.properties[field])
        if fields[field] is None or fields[field] == "" or fields[field] == []:
            raise KGError(
                "RULE_INCOMPLETE",
                "An absent rule field cannot be treated as an explicit statement",
                field=field,
            )
        field_evidence = verify_evidence(
            declaration.get("evidence", {}).get(field, []),
            chunks,
            units=catalog,
            input_unit_keys=input_unit_keys,
        )
        if {item["document_id"] for item in field_evidence} != {document["document_id"]}:
            raise KGError("RULE_INCOMPLETE", "Atomic declaration crosses source documents")
        per_field[field] = field_evidence
        supports.extend(field_evidence)

    mapping_id = candidate.get("mapping_id")
    if mapping_id:
        mapping = model.mappings.get(mapping_id)
        if not mapping or declaration.get("mapping_id") != mapping_id:
            raise KGError("RULE_INCOMPLETE", "Atomic mapping authorization is missing")
        record_index = declaration.get("record_index")
        for evidence in supports:
            for span in evidence["source_spans"]:
                origin = span["origin"]
                actual_record = (
                    origin.get("row") if mapping["format"] == "csv" else origin.get("record_index")
                )
                if actual_record != record_index:
                    raise KGError("RULE_INCOMPLETE", "Mapped rule fields must come from one record")
        validation = {"method": "explicit_mapping", "mapping_id": mapping_id, "record_index": record_index}
    else:
        selected = declaration.get("rule_units", [])
        if not isinstance(selected, list) or not selected or len(selected) != len(set(selected)):
            raise KGError("RULE_INCOMPLETE", "A rule must identify its complete source clause")
        if any(not isinstance(key, str) or key not in catalog for key in selected):
            raise KGError("RULE_INCOMPLETE", "The rule cites an unavailable source clause")
        if input_unit_keys is not None and set(selected) - set(input_unit_keys):
            raise KGError("RULE_INCOMPLETE", "The rule selected a source unit not supplied in its model call")
        ordered = sorted((catalog[key] for key in selected), key=lambda unit: unit["order"])
        # 上下文证据由程序确定；字段证据和完整性标记仍分别校验。
        section_support = [
            item for unit in ordered for item in unit_context_evidence(unit, chunks, visible_only=True)
        ]
        if {item["document_id"] for item in section_support} != {document["document_id"]}:
            raise KGError("RULE_INCOMPLETE", "Rule context crosses source documents")
        allowed = defaultdict(list)
        for unit in ordered:
            for region in unit["regions"]:
                key = (region["origin"], region["coordinate"])
                allowed[key].append(region["range"])
        for key, ranges in _source_ranges(supports).items():
            if any(not _covers(allowed[key], *interval) for interval in ranges):
                raise KGError(
                    "RULE_INCOMPLETE", "A rule field is supported outside its declared source clause"
                )

        # 补充引用无效时只拒绝该引用，不丢弃已有独立证据的条款。
        rejected_references = 0
        for reference in declaration.get("resolved_references", []):
            try:
                source = verify_evidence(
                    reference.get("reference", []), chunks, units=catalog, input_unit_keys=input_unit_keys
                )
                target = verify_evidence(
                    reference.get("target", []), chunks, units=catalog, input_unit_keys=input_unit_keys
                )
                if {item["document_id"] for item in source + target} != {document["document_id"]}:
                    raise KGError("EVIDENCE_INVALID", "A resolved reference crosses source documents")
                if any(
                    not _covers(allowed[key], *interval)
                    for key, ranges in _source_ranges(source).items()
                    for interval in ranges
                ):
                    raise KGError("EVIDENCE_INVALID", "The reference is outside the source rule")
                if evidence_positions(source) == evidence_positions(target):
                    raise KGError("EVIDENCE_INVALID", "Reference and target are identical")
            except (KGError, AttributeError, TypeError):
                rejected_references += 1
                continue
            verified_references.append({"reference": source, "target": target})
            supports.extend(source + target)
        supports.extend(section_support)
        validation = {
            "method": "basic_source_evidence",
            "rule_units": [unit["unit_key"] for unit in ordered],
            "section_key": declaration["section_key"],
            "resolved_reference_count": len(verified_references),
            "required_reference_count": 0,
            "rejected_optional_reference_count": rejected_references,
            "semantic_completeness_verified": False,
        }
    statement = {
        "fields": fields,
        "complete": declaration.get("complete") is True,
        "references_resolved": declaration.get("references_resolved") is True
        and not validation.get("rejected_optional_reference_count", 0),
        "section_key": declaration["section_key"],
        "field_evidence": per_field,
        "resolved_references": verified_references,
        "completeness_check": validation,
        **(
            {"literal_provenance": declaration["literal_provenance"]}
            if declaration.get("literal_provenance")
            else {}
        ),
    }
    return statement, supports


def _relation_identity(candidate, definition, source, target):
    values = candidate.get("properties", {})
    if any(field not in values for field in definition.identity_properties):
        raise KGError("MISSING_IDENTITY", "Relation is missing an identity property")
    return {
        "source_id": source["id"],
        "target_id": target["id"],
        "relation_type": candidate["type"],
        "fields": {field: values[field] for field in definition.identity_properties},
    }


_ALIAS_EQUIVALENCE = re.compile(
    r"\b(?:also known as|formerly known as|a\.?k\.?a\.?)\b|即|又称|简称|亦称", re.I
)


async def _resolve_mention(doc, candidate, definition, graph, chunks, catalog, input_unit_keys):
    """Use only configured, source-verified independent facts to reuse an existing ID."""
    if graph is None or not definition.identity_match.model_dump(exclude_defaults=True):
        raise KGError("MISSING_IDENTITY", "Incomplete identity has no configured alignment path")
    mention = candidate.get("mention")
    if not isinstance(mention, str) or not mention.strip():
        raise KGError("MISSING_IDENTITY", "Incomplete identity requires a source mention")
    mention_support = verify_evidence(
        candidate.get("mention_evidence", []),
        chunks,
        units=catalog,
        input_unit_keys=input_unit_keys,
    )
    if {item["document_id"] for item in mention_support} != {doc["document_id"]}:
        raise KGError("EVIDENCE_INVALID", "Entity mention must come from this document")
    if not any(mention in item.get("quote", "") for item in mention_support):
        raise KGError("IDENTITY_EVIDENCE_MISSING", "Mention evidence does not contain the stated name")
    matches = {}
    document_ids = [doc["document_id"]] if definition.identity.scope == "document" else None
    for field in definition.identity_match.stable_fields:
        if field not in candidate["properties"]:
            continue
        support = verify_evidence(
            candidate.get("evidence", {}).get(field, []),
            chunks,
            units=catalog,
            input_unit_keys=input_unit_keys,
        )
        if {item["document_id"] for item in support} != {doc["document_id"]}:
            raise KGError("EVIDENCE_INVALID", "Identity discriminator belongs to another document")
        value = candidate["properties"][field]
        if not any(str(value) in item.get("quote", "") for item in support):
            raise KGError("IDENTITY_EVIDENCE_MISSING", "Stable-field evidence omits its value")
        found = await graph.by_property_value(doc["kb_id"], candidate["type"], field, value, document_ids)
        if len(found) > 1:
            raise KGError("IDENTITY_AMBIGUITY", "A stable field matches multiple entities")
        matches.update((item["entity_id"], item) for item in found)
    equivalence = candidate.get("identity_equivalence")
    if equivalence:
        if not definition.identity_match.explicit_alias:
            raise KGError("IDENTITY_EVIDENCE_MISSING", "Explicit alias matching is not configured")
        values = equivalence["identity_fields"]
        if set(values) != set(definition.identity.fields):
            raise KGError("IDENTITY_EVIDENCE_MISSING", "Alias statement lacks complete identity values")
        support = verify_evidence(
            equivalence["evidence"], chunks, units=catalog, input_unit_keys=input_unit_keys
        )
        if {item["document_id"] for item in support} != {doc["document_id"]}:
            raise KGError("EVIDENCE_INVALID", "Alias statement belongs to another document")
        if not any(
            mention in item.get("quote", "")
            and all(str(value) in item.get("quote", "") for value in values.values())
            and _ALIAS_EQUIVALENCE.search(item.get("quote", ""))
            for item in support
        ):
            raise KGError(
                "IDENTITY_EVIDENCE_MISSING", "Source does not explicitly equate the mention and identity"
            )
        identity = identity_value(definition, values, doc["document_id"])
        found = await graph.by_identity(doc["kb_id"], "entity", [(candidate["type"], canonical(identity))])
        if len(found) > 1:
            raise KGError("IDENTITY_AMBIGUITY", "Alias identity matches multiple entities")
        matches.update((item["entity_id"], item) for item in found)
    if len(matches) != 1:
        raise KGError(
            "IDENTITY_AMBIGUITY" if matches else "IDENTITY_EVIDENCE_MISSING",
            "Independent source facts do not identify one existing entity",
        )
    target = next(iter(matches.values()))
    if target["entity_type"] != candidate["type"] or (
        definition.identity.scope == "document" and doc["document_id"] not in target.get("document_ids", [])
    ):
        raise KGError("IDENTITY_AMBIGUITY", "Candidate exceeds its identity type or document scope")
    return target, mention_support


async def align(
    doc,
    extracted,
    chunks,
    model,
    state,
    job_id,
    existing_entities,
    existing_relations,
    rule_context=None,
    *,
    graph=None,
):
    """按业务身份对齐候选并生成来源贡献，将无法采用的候选单独记录。

    实体贡献先暂存；仅供关系端点使用的不完整实体须有成功采用的关系才保留。
    """
    if graph is not None:
        keys = set()
        for candidate in extracted["entities"]:
            definition = model.node_types.get(candidate["type"])
            if definition is not None:
                try:
                    identity = identity_value(definition, candidate["properties"], doc["document_id"])
                    keys.add((candidate["type"], canonical(identity)))
                except KGError:
                    # 正常对齐阶段会记录无法构造可靠身份的候选。
                    continue
        existing_entities = await graph.by_identity(doc["kb_id"], "entity", sorted(keys))

    entity_identities, relation_identities = defaultdict(set), defaultdict(set)
    for entity in existing_entities:
        entity_identities[(entity["entity_type"], canonical(entity["identity"]))].add(entity["entity_id"])
    identities = state.setdefault("entity_ids", {})
    relation_ids = state.setdefault("relation_ids", {})
    contribution_ids = state.setdefault("contribution_ids", {})
    output, issues, refs, entity_rows = [], [], {}, {}
    known_domains = {domain["id"] for domain in model.domains}
    relation_refs = {
        relation[key] for relation in extracted["relations"] for key in ("source_ref", "target_ref")
    }
    ref_counts = Counter(candidate["ref"] for candidate in extracted["entities"])
    catalog = {unit["unit_key"]: unit for unit in rule_context or []}

    def issue(candidate, exc):
        evidence = candidate.get("evidence")
        first = next(iter(evidence.values()), []) if isinstance(evidence, dict) else evidence or []
        issues.append(
            {
                "issue_id": new_id(),
                "kb_id": doc["kb_id"],
                "job_id": job_id,
                "document_id": doc["document_id"],
                "chunk_id": first[0].get("chunk_id") if first else None,
                "candidate": candidate,
                "reason_code": exc.code,
                "details": {"message": exc.message} | exc.details,
            }
        )

    def contribution(kind, object_id, field, value, evidence, claim=None, dependencies=()):
        comparable_value = {"fields": value["fields"]} if field.startswith("/atomic_groups/") else value
        key = canonical(
            {
                "document_id": doc["document_id"],
                "kind": kind,
                "target_id": object_id,
                "field": field,
                "value": comparable_value,
                "source_positions": evidence_positions(evidence),
                "identity_dependencies": list(dependencies),
            }
        )
        if key not in contribution_ids:
            contribution_ids[key] = new_id()
        return {
            "contribution_id": contribution_ids[key],
            "kb_id": doc["kb_id"],
            "document_id": doc["document_id"],
            "chunk_id": evidence[0]["chunk_id"],
            "target_kind": kind,
            "target_id": object_id,
            "field_path": field,
            "claim": (claim if claim is not None else {"value": value})
            | ({"identity_dependencies": list(dependencies)} if dependencies else {}),
            "value": value,
            "evidence": evidence,
            "job_id": job_id,
        }

    for candidate in extracted["entities"]:
        try:
            if ref_counts[candidate["ref"]] != 1:
                raise KGError("IDENTITY_AMBIGUITY", "Every duplicate local reference is unresolved")
            definition = model.node_types.get(candidate["type"])
            if not definition:
                raise KGError("UNKNOWN_TYPE", "Unknown entity type")
            local_chunks, local_units = _input_scope(candidate, chunks)
            group_scopes = {
                name: _input_scope(scope, chunks)
                for name, scope in candidate.get("atomic_group_scopes", {}).items()
            }
            normalize_candidate_literals(
                candidate,
                definition,
                catalog,
                local_chunks,
                local_units if local_units is not None else list(catalog),
                group_scopes=group_scopes,
            )
            values = candidate["properties"]
            if set(values) - definition.properties.keys():
                raise KGError("UNKNOWN_FIELD", "Unknown entity property")
            for field, value in values.items():
                check_value(value, definition.properties[field])
            try:
                identity = identity_value(definition, values, doc["document_id"])
                alias_target, mention_evidence = None, []
            except KGError as exc:
                if exc.code != "MISSING_IDENTITY":
                    raise
                alias_target, mention_evidence = await _resolve_mention(
                    doc, candidate, definition, graph, local_chunks, catalog, local_units
                )
                identity = alias_target["identity"]
            atomic_fields = {field for group in definition.atomic_groups for field in group.fields}
            missing = [
                field
                for field, prop in definition.properties.items()
                if prop.required and field not in atomic_fields and field not in values
            ]
            endpoint_only = bool(missing) and alias_target is None
            if (
                missing
                and alias_target is None
                and not (candidate.get("identity_only") and candidate["ref"] in relation_refs)
            ):
                raise KGError(
                    "MISSING_REQUIRED_FIELD", "Entity is missing required properties", fields=missing
                )
            domains = sorted(
                set(
                    candidate.get("domain_ids", [])
                    + doc["source"].get("domain_ids", [])
                    + definition.domain_ids
                )
            )
            if set(domains) - known_domains:
                raise KGError("UNKNOWN_DOMAIN", "Candidate contains unconfigured domains")
            evidence_by_field = {}
            for field in values:
                if field not in atomic_fields:
                    evidence_by_field[field] = verify_evidence(
                        candidate.get("evidence", {}).get(field, []),
                        local_chunks,
                        units=catalog,
                        input_unit_keys=local_units,
                    )
            identity_evidence = []
            if alias_target is None:
                for field in definition.identity.fields:
                    if field not in evidence_by_field:
                        evidence_by_field[field] = verify_evidence(
                            candidate.get("evidence", {}).get(field, []),
                            local_chunks,
                            units=catalog,
                            input_unit_keys=local_units,
                        )
                    identity_evidence.extend(evidence_by_field[field])
            else:
                identity_evidence = mention_evidence
            if {item["document_id"] for item in identity_evidence} != {doc["document_id"]}:
                raise KGError("EVIDENCE_INVALID", "Entity support belongs to another source document")
            key = canonical([candidate["type"], identity])
            existing = entity_identities[(candidate["type"], canonical(identity))]
            if len(existing) > 1:
                raise KGError("IDENTITY_AMBIGUITY", "Multiple existing objects share this business identity")
            object_id = (
                alias_target["entity_id"]
                if alias_target is not None
                else identities.get(key) or next(iter(existing), None) or new_id()
            )
            if identities.get(key) not in (None, object_id):
                raise KGError("IDENTITY_AMBIGUITY", "Local identity maps to conflicting graph objects")
            identities[key] = object_id
            dependencies = (
                [{"target_id": object_id, "identity_key": canonical(identity)}]
                if alias_target is not None
                else []
            )
            rows = []
            if alias_target is None:
                rows.append(
                    contribution(
                        "entity",
                        object_id,
                        "$exists",
                        {"entity_type": candidate["type"], "identity": identity, "domain_ids": domains},
                        identity_evidence,
                    )
                )
                for field in definition.identity.fields:
                    rows.append(
                        contribution(
                            "entity",
                            object_id,
                            "/identity/" + field,
                            identity["fields"][field],
                            evidence_by_field[field],
                            {"raw_value": values[field]},
                        )
                    )
            else:
                rows.append(
                    contribution(
                        "entity",
                        object_id,
                        "$mention",
                        {"mention": candidate["mention"]},
                        mention_evidence,
                        dependencies=dependencies,
                    )
                )
            for field, value in values.items():
                if field not in atomic_fields and (
                    alias_target is None or field not in definition.identity.fields
                ):
                    rows.append(
                        contribution(
                            "entity",
                            object_id,
                            "/properties/" + field,
                            identity["fields"].get(field, value),
                            evidence_by_field[field],
                            {"raw_value": value}
                            | (
                                {"literal_provenance": candidate["literal_provenance"][field]}
                                if field in candidate.get("literal_provenance", {})
                                else {}
                            ),
                            dependencies,
                        )
                    )
            for group in definition.atomic_groups:
                # 规则字段必须成组成立；端点豁免不能让不完整规则成为事实。
                scope = candidate.get("atomic_group_scopes", {}).get(group.name)
                group_chunks, group_units = (
                    _input_scope(scope, chunks) if scope is not None else (local_chunks, local_units)
                )
                statement, supports = _atomic_statement(
                    candidate,
                    group,
                    definition,
                    doc,
                    group_chunks,
                    catalog,
                    model,
                    input_unit_keys=group_units,
                )
                rows.append(
                    contribution(
                        "entity",
                        object_id,
                        "/atomic_groups/" + group.name,
                        statement,
                        supports,
                        {"fields": statement["fields"]},
                        dependencies,
                    )
                )
            refs[candidate["ref"]] = {
                "id": object_id,
                "type": candidate["type"],
                "identity": identity,
                "evidence": identity_evidence,
                "identity_dependencies": dependencies,
            }
            entity_rows[candidate["ref"]] = (rows, endpoint_only)
        except KGError as exc:
            issue(candidate, exc)

    # 关系身份依赖已校验的端点 ID，实体对齐结束后再批量匹配。
    if graph is not None:
        keys = set()
        for candidate in extracted["relations"]:
            definition = model.relation_types.get(candidate["type"])
            source, target = refs.get(candidate["source_ref"]), refs.get(candidate["target_ref"])
            if definition and source and target:
                try:
                    keys.add(validate_identity_key(_relation_identity(candidate, definition, source, target)))
                except KGError:
                    continue
        existing_relations = await graph.by_identity(doc["kb_id"], "relation", sorted(keys))
    for relation in existing_relations:
        relation_identities[canonical(relation["identity"])].add(relation["relation_id"])

    # 关系只使用已通过身份与证据检查的本地实体引用。
    used_refs = set()
    for candidate in extracted["relations"]:
        try:
            definition = model.relation_types.get(candidate["type"])
            source, target = refs.get(candidate["source_ref"]), refs.get(candidate["target_ref"])
            if not definition:
                raise KGError("UNKNOWN_TYPE", "Unknown relation type")
            if (
                not source
                or not target
                or source["type"] not in definition.source_types
                or target["type"] not in definition.target_types
            ):
                raise KGError("INVALID_ENDPOINT", "Relation endpoints are unresolved or have invalid types")
            values = candidate.get("properties", {})
            if set(values) - definition.properties.keys():
                raise KGError("UNKNOWN_FIELD", "Unknown relation property")
            for field, prop in definition.properties.items():
                if prop.required and field not in values:
                    raise KGError(
                        "MISSING_REQUIRED_FIELD", "Relation is missing required properties", field=field
                    )
                if field in values:
                    check_value(values[field], prop)
            identity = _relation_identity(candidate, definition, source, target)
            key = validate_identity_key(identity)
            existing = relation_identities[key]
            if len(existing) > 1:
                raise KGError(
                    "IDENTITY_AMBIGUITY", "Multiple existing relations share this business identity"
                )
            object_id = relation_ids.get(key) or next(iter(existing), None) or new_id()
            relation_ids[key] = object_id
            local_chunks, local_units = _input_scope(candidate, chunks)
            evidence = verify_evidence(
                candidate.get("evidence", []), local_chunks, units=catalog, input_unit_keys=local_units
            )
            if {item["document_id"] for item in evidence} != {doc["document_id"]}:
                raise KGError("EVIDENCE_INVALID", "Relation support belongs to another source document")
            endpoint_identities = {"source": source["identity"], "target": target["identity"]}
            mapping_id = candidate.get("mapping_id")
            if mapping_id and mapping_id != doc.get("mapping_id"):
                raise KGError("INVALID_MAPPING", "The relation was not authorized by this document's mapping")
            value = identity | {
                "identity": identity,
                "domain_ids": definition.domain_ids,
                "endpoint_identities": endpoint_identities,
                "mapping_id": mapping_id,
            }
            rows = [
                contribution(
                    "relation",
                    object_id,
                    "$relation",
                    value,
                    evidence,
                    {
                        "mapping_id": mapping_id,
                        "endpoint_identities": endpoint_identities,
                        "endpoint_evidence": {"source": source["evidence"], "target": target["evidence"]},
                    },
                    list(
                        {
                            item["target_id"]: item
                            for endpoint in (source, target)
                            for item in endpoint["identity_dependencies"]
                        }.values()
                    ),
                )
            ]
            for field, value_ in values.items():
                support = verify_evidence(
                    candidate.get("property_evidence", {}).get(field, []),
                    local_chunks,
                    units=catalog,
                    input_unit_keys=local_units,
                )
                rows.append(
                    contribution(
                        "relation",
                        object_id,
                        "/properties/" + field,
                        value_,
                        support,
                        dependencies=list(
                            {
                                item["target_id"]: item
                                for endpoint in (source, target)
                                for item in endpoint["identity_dependencies"]
                            }.values()
                        ),
                    )
                )
            output.extend(rows)
            used_refs.update((candidate["source_ref"], candidate["target_ref"]))
        except KGError as exc:
            issue(candidate, exc)

    # 端点放宽不产生独立事实：没有采用关系支撑的缺字段实体退出本轮结果。
    for ref, (rows, endpoint_only) in entity_rows.items():
        if not endpoint_only or ref in used_refs:
            output.extend(rows)
        else:
            issue({"ref": ref}, KGError("INVALID_ENDPOINT", "An incomplete endpoint has no adopted relation"))
    return list({row["contribution_id"]: row for row in output}.values()), issues
