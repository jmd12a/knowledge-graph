from __future__ import annotations

import unicodedata
from collections import defaultdict

from ...domain import KGError, canonical, new_id
from ...domain.source import effective_contributions


def _values(contributions, merge):
    """按规范 JSON 值归并来源；union 拆分列表，其他策略保留原值。"""
    grouped = {}
    for contribution in contributions:
        values = (
            contribution["value"]
            if merge == "union" and isinstance(contribution["value"], list)
            else [contribution["value"]]
        )
        for value in values:
            item = grouped.setdefault(canonical(value), {"value": value, "contribution_ids": []})
            item["contribution_ids"].append(contribution["contribution_id"])
    result = []
    for key in sorted(grouped):
        item = grouped[key]
        item["contribution_ids"] = sorted(set(item["contribution_ids"]))
        result.append(item)
    return result


def _atomic_values(contributions):
    """以整组字段归并完整声明，同时保留每份来源的证据和引用映射。"""
    grouped = {}
    for contribution in contributions:
        statement = contribution["value"]
        # Equivalent business declarations stay equivalent when source offsets or chunk IDs differ.
        value = {
            "fields": statement["fields"],
            "complete": statement.get("complete", False),
            "references_resolved": statement.get("references_resolved", False),
        }
        key = canonical(value)
        item = grouped.setdefault(
            key,
            {
                "value": value,
                "contribution_ids": [],
                "source_declarations": [],
            },
        )
        item["contribution_ids"].append(contribution["contribution_id"])
        item["source_declarations"].append(
            {
                "contribution_id": contribution["contribution_id"],
                "document_id": contribution["document_id"],
                "section_key": statement.get("section_key"),
                "field_evidence": statement.get("field_evidence", {}),
                "resolved_references": statement.get("resolved_references", []),
                "completeness_check": statement.get("completeness_check", {}),
                **(
                    {"literal_provenance": statement["literal_provenance"]}
                    if statement.get("literal_provenance")
                    else {}
                ),
            }
        )
    result = []
    for key in sorted(grouped):
        item = grouped[key]
        item["contribution_ids"] = sorted(set(item["contribution_ids"]))
        item["source_declarations"].sort(key=lambda declaration: declaration["contribution_id"])
        result.append(item)
    return result


def relation_conflict_groups(changes, model):
    groups = set()
    for claim in changes:
        if claim["target_kind"] == "relation" and claim["field_path"] == "$relation":
            relation = claim["value"]
            relation_type = relation["relation_type"]
            if model.relation_types[relation_type].conflict_rule.get("max_distinct_targets"):
                groups.add((relation["source_id"], relation_type))
    return [list(group) for group in sorted(groups)]


def aggregate(
    kb_id, contributions, model, previous_entities=None, previous_relations=None, *, endpoint_ids=()
):
    """合并传入的目标贡献；局部维护可引用已经存在的外部端点。"""
    old_entities = {item["entity_id"]: item for item in previous_entities or []}
    old_relations = {item["relation_id"]: item for item in previous_relations or []}
    contributions, _ = effective_contributions(contributions)
    by_target = defaultdict(list)
    for contribution in contributions:
        if contribution["kb_id"] != kb_id:
            raise KGError("INVALID_CONTRIBUTION", "A source contribution belongs to another knowledge base")
        by_target[(contribution["target_kind"], contribution["target_id"])].append(contribution)
    entities, relations = [], []
    for (kind, object_id), claims in sorted(by_target.items()):
        if kind not in {"entity", "relation"}:
            raise KGError("INVALID_CONTRIBUTION", "A source contribution has an invalid object kind")
        exists = [
            claim
            for claim in claims
            if claim["field_path"] == ("$exists" if kind == "entity" else "$relation")
        ]
        if not exists:
            raise KGError(
                "INVALID_CONTRIBUTION",
                "Adopted properties lack a source-supported object",
                target_id=object_id,
            )
        declarations = {canonical(claim["value"]["identity"]) for claim in exists}
        if len(declarations) != 1:
            raise KGError(
                "IDENTITY_AMBIGUITY",
                "One object ID has inconsistent business identities",
                target_id=object_id,
            )
        declaration = exists[0]["value"]
        definition = (
            model.node_types[declaration["entity_type"]]
            if kind == "entity"
            else model.relation_types[declaration["relation_type"]]
        )

        properties, conflicts, atomic_groups = {}, [], {}
        for name, prop in definition.properties.items():
            field_claims = [claim for claim in claims if claim["field_path"] == "/properties/" + name]
            values = _values(field_claims, prop.merge)
            if not values:
                properties[name] = {"state": "missing", "values": []}
                continue
            conflicted = prop.merge == "single" and len(values) > 1
            properties[name] = {
                "state": "conflicted" if conflicted else "confirmed",
                "merge": prop.merge,
                "values": values,
            }
            if conflicted:
                conflicts.append(
                    {
                        "field_path": "/properties/" + name,
                        "reason": "MULTIPLE_SINGLE_VALUES",
                        "contribution_ids": sorted(
                            {item for value in values for item in value["contribution_ids"]}
                        ),
                    }
                )

        # 原子规则按完整声明比较，不能把不同来源的字段拼成一条规则。
        if kind == "entity":
            for group in definition.atomic_groups:
                group_claims = [
                    claim
                    for claim in claims
                    if claim["field_path"] == "/atomic_groups/" + group.name
                    or claim["field_path"].startswith("/atomic_groups/" + group.name + "/")
                ]
                variants = _atomic_values(group_claims)
                atomic_groups[group.name] = {
                    "state": "missing"
                    if not variants
                    else "confirmed"
                    if len(variants) == 1
                    else "conflicted",
                    "declarations": variants,
                }
                for name in group.fields:
                    properties.pop(name, None)
                if len(variants) > 1:
                    conflicts.append(
                        {
                            "field_path": "/atomic_groups/" + group.name,
                            "reason": "ALTERNATIVE_COMPLETE_STATEMENTS",
                            "contribution_ids": sorted(
                                {item for variant in variants for item in variant["contribution_ids"]}
                            ),
                        }
                    )

        common = {
            "kb_id": kb_id,
            "kind": kind,
            "origin": "graph",
            "properties": properties,
            "conflicts": conflicts,
            "domain_ids": sorted(
                {domain for claim in exists for domain in claim["value"].get("domain_ids", [])}
            ),
            "contribution_ids": sorted({claim["contribution_id"] for claim in claims}),
            "document_ids": sorted({claim["document_id"] for claim in claims}),
        }
        if kind == "entity":
            identity = declaration["identity"]
            names = properties.get(definition.name_field, {}).get("values", [])
            raw_names = sorted(
                {str(value["value"]) for value in names if value["value"] not in (None, "")},
                key=lambda value: (unicodedata.normalize("NFC", value), value),
            )
            name = next(
                (preferred for preferred in definition.name_priority if preferred in raw_names),
                raw_names[0]
                if raw_names
                else " / ".join(str(value) for value in identity["fields"].values()),
            )
            aliases = set(raw_names) - {name}
            for value in properties.get("aliases", {}).get("values", []):
                if isinstance(value["value"], str):
                    aliases.add(value["value"])
                elif isinstance(value["value"], list):
                    aliases.update(str(item) for item in value["value"])
            aliases.discard(name)
            atomic_fields = {
                field
                for group in definition.atomic_groups
                if atomic_groups[group.name]["declarations"]
                for field in group.fields
            }
            missing = [
                field
                for field, prop in definition.properties.items()
                if prop.required
                and field not in atomic_fields
                and not properties.get(field, {}).get("values")
            ]
            item = common | {
                "entity_id": object_id,
                "entity_type": declaration["entity_type"],
                "identity": identity,
                "canonical_name": name,
                "aliases": sorted(aliases),
                "atomic_groups": atomic_groups,
                "completeness": "identity_only" if missing else "complete",
                "missing_fields": missing,
            }
            fragments = []
            for field, prop in definition.properties.items():
                if prop.merge == "supported_text":
                    fragments.extend(
                        {"field": field, **value} for value in properties.get(field, {}).get("values", [])
                    )
            item["description_fragments"] = fragments
            item["description"] = "\n".join(str(fragment["value"]) for fragment in fragments)
            entities.append(item)
        else:
            item = common | {
                "relation_id": object_id,
                "relation_type": declaration["relation_type"],
                "source_id": declaration["source_id"],
                "target_id": declaration["target_id"],
                "identity": declaration["identity"],
            }
            relations.append(item)
        item["search_text"] = _search_text(item, definition.description)

    # 局部聚合前已收齐同组关系，保留关系仍需验证两个端点。
    entity_ids = {entity["entity_id"] for entity in entities} | set(endpoint_ids)
    for relation in relations:
        if relation["source_id"] not in entity_ids or relation["target_id"] not in entity_ids:
            raise KGError("INVALID_CONTRIBUTION", "A retained relation lacks a source-supported endpoint")
    conflict_groups = defaultdict(list)
    for relation in relations:
        definition = model.relation_types[relation["relation_type"]]
        if definition.conflict_rule.get("max_distinct_targets"):
            conflict_groups[(relation["source_id"], relation["relation_type"])].append(relation)
    for (source_id, relation_type), group in conflict_groups.items():
        maximum = model.relation_types[relation_type].conflict_rule["max_distinct_targets"]
        if len({relation["target_id"] for relation in group}) > maximum:
            for relation in group:
                relation["conflicts"].append(
                    {
                        "field_path": "$relation",
                        "reason": "MAX_DISTINCT_TARGETS",
                        "group": {"source_id": source_id, "relation_type": relation_type},
                        "relation_ids": sorted(item["relation_id"] for item in group),
                    }
                )

    # Revisions compare complete final objects, after relation conflict groups have been recomputed.
    for item in entities + relations:
        if item["kind"] == "relation":
            item["search_text"] = _search_text(item, model.relation_types[item["relation_type"]].description)
        object_id = item["entity_id"] if item["kind"] == "entity" else item["relation_id"]
        previous = old_entities.get(object_id) if item["kind"] == "entity" else old_relations.get(object_id)
        previous_content = {key: value for key, value in (previous or {}).items() if key != "object_revision"}
        item["object_revision"] = (
            previous["object_revision"] if previous and previous_content == item else new_id()
        )
    return entities, relations


def _search_text(item, type_description):
    fields = [
        item.get("canonical_name", ""),
        " ".join(item.get("aliases", [])),
        item.get("entity_type", item.get("relation_type", "")),
        type_description,
    ]
    for name, prop in item["properties"].items():
        if prop.get("values"):
            fields.append(
                name
                + " "
                + ("CONFLICT: " if prop["state"] == "conflicted" else "")
                + canonical([value["value"] for value in prop["values"]])
            )
    for name, group in item.get("atomic_groups", {}).items():
        if group["declarations"]:
            fields.append(
                name
                + " "
                + ("CONFLICT: " if group["state"] == "conflicted" else "")
                + canonical([declaration["value"]["fields"] for declaration in group["declarations"]])
            )
    if item.get("conflicts"):
        fields.append(
            "CONFLICT: " + ", ".join(sorted({conflict["reason"] for conflict in item["conflicts"]}))
        )
    return "\n".join(field for field in fields if field)
