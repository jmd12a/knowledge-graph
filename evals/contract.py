from __future__ import annotations

import json
import re

import yaml

from .evidence import ROOT, write_json

CORPUS = ROOT / "evals" / "corpus" / "cuad"


def model():
    return yaml.safe_load((ROOT / "examples" / "contracts" / "model.yaml").read_text("utf-8"))


def definitions(entity):
    group = entity.get("atomic_groups", {}).get("full_statement", {})
    return [item["value"]["fields"]["definition"] for item in group.get("declarations", [])]


def contains_all(text, groups):
    return all(any(re.search(pattern, text, re.I) for pattern in alternatives) for alternatives in groups)


def character_range(value, maximum):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or not 0 <= value[0] < value[1] <= maximum
    ):
        raise ValueError("invalid Unicode character range")
    return value[0], value[1]


def audit_source_evidence(snapshot, by_document):
    """Independently compare stored citations with real Qdrant chunks and unchanged source text."""
    audit = {
        "source_spans": 0,
        "fact_references": 0,
        "legacy_references": 0,
        "context_references": 0,
        "fact_quotes_verified": 0,
        "contexts_verified": 0,
        "errors": [],
        "context_body_errors": [],
    }
    chunks = {point["id"]: point["payload"] for point in snapshot["chunks"]}
    context_fields = {
        "document_id",
        "chunk_id",
        "chunk_range",
        "locator",
        "source_spans",
        "role",
        "source_mapping_verified",
    }
    for chunk in chunks.values():
        for span in chunk["source_spans"]:
            if span.get("role") != "source":
                continue
            audit["source_spans"] += 1
            try:
                original = by_document[chunk["document_id"]]
                start, end = character_range(span["chunk_range"], len(chunk["text"]))
                left, right = character_range(span["origin"]["char_range"], len(original))
                if end - start != right - left or chunk["text"][start:end] != original[left:right]:
                    raise ValueError("chunk span differs from original source characters")
            except (KeyError, TypeError, ValueError) as exc:
                audit["errors"].append({"chunk_id": chunk.get("chunk_id"), "span": span, "reason": str(exc)})
    for claim in snapshot["contributions"]:
        for reference in claim["evidence"]:
            role = reference.get("role", "legacy")
            counter = {
                "fact": "fact_references",
                "legacy": "legacy_references",
                "context": "context_references",
            }.get(role)
            if counter:
                audit[counter] += 1
            if role == "context":
                unexpected = sorted(set(reference) - context_fields)
                if unexpected:
                    audit["context_body_errors"].append(
                        {
                            "contribution_id": claim["contribution_id"],
                            "chunk_id": reference.get("chunk_id"),
                            "unexpected_fields": unexpected,
                        }
                    )
            try:
                if role not in {"fact", "context", "legacy"} or (role == "legacy" and "role" in reference):
                    raise ValueError("unknown stored evidence role")
                chunk = chunks[reference["chunk_id"]]
                if (
                    reference["document_id"] != chunk["document_id"]
                    or reference["document_id"] != claim["document_id"]
                ):
                    raise ValueError("citation crosses document scope")
                if reference["locator"] != chunk["locator"]:
                    raise ValueError("citation locator differs from the indexed chunk")
                original = by_document[chunk["document_id"]]
                left, right = character_range(reference["chunk_range"], len(chunk["text"]))
                cursor, expected_spans = left, []
                for span in sorted(chunk["source_spans"], key=lambda item: item["chunk_range"]):
                    a, b = character_range(span["chunk_range"], len(chunk["text"]))
                    start, end = max(left, a), min(right, b)
                    if start >= end:
                        continue
                    if span.get("role") != "source" or start > cursor:
                        raise ValueError(
                            "complete citation range contains synthetic content or an unmapped gap"
                        )
                    origin = dict(span["origin"])
                    source_left, source_right = character_range(origin["char_range"], len(original))
                    if source_right - source_left != b - a:
                        raise ValueError("citation source mapping is not reversible")
                    source_start, source_end = source_left + start - a, source_left + end - a
                    if chunk["text"][start:end] != original[source_start:source_end]:
                        raise ValueError("citation source interval differs from the original document")
                    origin["char_range"] = [source_start, source_end]
                    expected_spans.append(
                        {
                            "chunk_range": [start, end],
                            "origin": origin,
                            "role": "source",
                            "transform": span["transform"],
                        }
                    )
                    cursor = max(cursor, end)
                if cursor != right or reference["source_spans"] != expected_spans:
                    raise ValueError("stored source spans do not cover the complete citation range")
                if role == "context":
                    if (
                        set(reference) != context_fields
                        or reference.get("source_mapping_verified") is not True
                    ):
                        raise ValueError(
                            "context must contain only coordinates, locator, spans and verified mapping state"
                        )
                    audit["contexts_verified"] += 1
                    continue
                quote = reference.get("quote")
                if not isinstance(quote, str) or not 0 < len(quote) <= 500:
                    raise ValueError(
                        "fact or legacy citation needs a nonempty quote of at most 500 characters"
                    )
                if role == "fact":
                    quote_left, quote_right = character_range(
                        reference.get("quote_range"), len(chunk["text"])
                    )
                    if not left <= quote_left < quote_right <= right:
                        raise ValueError("fact quote range falls outside the full citation range")
                    expected_quote = chunk["text"][quote_left:quote_right]
                    truncated = [quote_left, quote_right] != [left, right]
                else:
                    expected_quote = chunk["text"][left : min(right, left + 500)]
                    truncated = right - left > 500
                if (
                    quote != expected_quote
                    or not isinstance(reference.get("quote_truncated"), bool)
                    or reference["quote_truncated"] != truncated
                ):
                    raise ValueError("short quote or truncation flag differs from its indexed source range")
                audit["fact_quotes_verified"] += 1
            except (KeyError, TypeError, ValueError) as exc:
                audit["errors"].append(
                    {"contribution_id": claim["contribution_id"], "reference": reference, "reason": str(exc)}
                )
    return audit


EXPECTATIONS = [
    {
        "name": "tiered_rates",
        "file": "galacticomm-web-hosting.txt",
        "expected": "同一完整付款条款保留 0.01/次、400,000 次阈值及之后 0.005/次，不计算费用。",
        "groups": [[r"\$0\.01"], [r"400,000"], [r"\$0\.005"], [r"thereafter"], [r"per access"]],
    },
    {
        "name": "weekly_payment",
        "file": "galacticomm-web-hosting.txt",
        "expected": "付款方、收款方、前一周 Monday–Sunday 与最迟 Wednesday 在完整条款中共同保留。",
        "groups": [
            [r"Horst Entertainment"],
            [r"Galactcomm", r"Galacticomm"],
            [r"Wednesday"],
            [r"Monday"],
            [r"Sunday"],
            [r"previuos week"],
        ],
    },
    {
        "name": "bilateral_support",
        "file": "galacticomm-web-hosting.txt",
        "expected": "双方 24/7 支持义务、机房不可用、电话/传呼及 broadcast location 条件保留，this same 指代明确。",
        "groups": [
            [r"Galacticomm"],
            [r"Horst Entertainment"],
            [r"24 hours per day"],
            [r"7 days per week"],
            [r"beeper"],
            [r"telephone"],
            [r"broadcast location"],
            [r"this same 24 hour service"],
        ],
    },
    {
        "name": "termination_purchase",
        "file": "galacticomm-web-hosting.txt",
        "expected": "Galacticomm 选择终止这一条件、Horst 购买软件许可的权利与 $15,000.00 金额不能拆开。",
        "groups": [
            [r"Galacticomm, Inc\. chooses to terminate"],
            [r"Horst Entertainment Inc\."],
            [r"right to purchase"],
            [r"license copy of the software"],
            [r"\$15,000\.00"],
        ],
    },
    {
        "name": "joint_filing_scope",
        "file": "rmr-joint-filing.txt",
        "expected": "联合申报主体、Schedule 13G/A、Schedule 13D 修订、RMR A类普通股与依据规则的范围可回查。",
        "groups": [
            [r"Schedule 13G/A"],
            [r"Schedule 13D"],
            [r"Class A Common Stock"],
            [r"The RMR Group Inc\."],
            [r"filed on behalf of each of us"],
            [r"Rule 13d-1\(k\)"],
        ],
    },
]


def property_values(entity, field):
    return [item["value"] for item in entity.get("properties", {}).get(field, {}).get("values", [])]


def source_paragraph(source, anchor):
    matches = [
        paragraph.strip() for paragraph in re.split(r"\r?\n[ \t]*\r?\n+", source) if anchor in paragraph
    ]
    if len(matches) != 1:
        raise ValueError("Fixed source paragraph is missing or ambiguous: " + anchor)
    return matches[0]


def audit_filing_date_roles(snapshot, document_id, source):
    context = re.search(r"Schedule 13G/A.*?dated as of December 31, 2019", source, re.S)
    if context is None:
        raise ValueError("The fixed source no longer contains its filing-date qualifier")
    agreements = [
        item
        for item in snapshot["entities"]
        if item["entity_type"] == "Agreement" and document_id in item["document_ids"]
    ]
    supported_clauses = [
        item["entity_id"]
        for item in snapshot["entities"]
        if item["entity_type"] == "Clause"
        and document_id in item["document_ids"]
        and any(context.group() in text for text in definitions(item))
    ]
    return {
        "name": "filing as-of qualifier and agreement signing date retain their source roles",
        "passed": len(agreements) == 1
        and bool(supported_clauses)
        and property_values(agreements[0], "signing_date") == ["2020-01-22"]
        and not property_values(agreements[0], "as_of_date"),
        "expected": {
            "Agreement.signing_date": "2020-01-22",
            "Agreement.as_of_date": "not stated",
            "Clause.source_qualifier": context.group(),
            "date_subject": "Schedule 13G/A, not the Joint Filing Agreement",
        },
        "actual": {
            "agreements": [
                {
                    "entity_id": item["entity_id"],
                    "as_of": property_values(item, "as_of_date"),
                    "signing": property_values(item, "signing_date"),
                }
                for item in agreements
            ],
            "source_clause_ids": supported_clauses,
        },
    }


def audit_business_graph(snapshot, imported, by_document, issues):
    """Audit adopted graph coverage and fixed source obligations, without estimating whole-source recall."""
    configuration = model()
    title_field = configuration["node_types"]["Agreement"]["name_field"]
    party_field = configuration["node_types"]["Party"]["name_field"]
    checks, documents = [], {}

    def check(name, passed, expected, actual):
        checks.append({"name": name, "passed": passed, "expected": expected, "actual": actual})

    specifications = [
        {
            "file": "galacticomm-web-hosting.txt",
            "label": "hosting",
            "title_pattern": r"\bWeb Hosting Agreement\b",
            "parties": {
                "provider": ["Galacticomm", "Galacticomm, Inc."],
                "customer": ["Horst Entertainment Inc.", "Horst Entertainment"],
            },
        },
        {
            "file": "rmr-joint-filing.txt",
            "label": "joint filing",
            "title_pattern": r"\bJoint Filing Agreement\b",
            "parties": {"trust": ["ABP TRUST"], "individual": ["ADAM D. PORTNOY", "Adam D. Portnoy"]},
        },
    ]
    for specification in specifications:
        document_id = imported[specification["file"]]["document_id"]
        original = by_document[document_id]
        # Case-insensitive title discovery returns the exact spellings that actually occur in the source.
        source_titles = sorted(
            {match.group() for match in re.finditer(specification["title_pattern"], original, re.I)}
        )
        entities = [item for item in snapshot["entities"] if document_id in item["document_ids"]]
        relations = [item for item in snapshot["relations"] if document_id in item["document_ids"]]
        agreements = [
            item
            for item in entities
            if item["entity_type"] == "Agreement"
            and item["identity"].get("document_id") == document_id
            and item["identity"]["fields"].get(title_field) in source_titles
            and property_values(item, title_field) == [item["identity"]["fields"][title_field]]
        ]
        agreement_ids = {item["entity_id"] for item in agreements}
        parties = [
            item
            for item in entities
            if item["entity_type"] == "Party"
            and item["identity"].get("document_id") == document_id
            and isinstance(item["identity"]["fields"].get(party_field), str)
            and item["identity"]["fields"][party_field] in original
            and property_values(item, party_field) == [item["identity"]["fields"][party_field]]
        ]
        party_ids = {item["entity_id"] for item in parties}
        clauses = [item for item in entities if item["entity_type"] == "Clause"]
        party_to = [
            item
            for item in relations
            if item["relation_type"] == "PARTY_TO"
            and item["source_id"] in party_ids
            and item["target_id"] in agreement_ids
        ]
        linked_party_ids = {item["source_id"] for item in party_to}
        group_matches = {
            group: [
                {"entity_id": item["entity_id"], "legal_name": item["identity"]["fields"][party_field]}
                for item in parties
                if item["entity_id"] in linked_party_ids and item["identity"]["fields"][party_field] in names
            ]
            for group, names in specification["parties"].items()
        }
        containment = {
            item["entity_id"]: [
                edge["relation_id"]
                for edge in relations
                if edge["relation_type"] == "CONTAINS"
                and edge["source_id"] in agreement_ids
                and edge["target_id"] == item["entity_id"]
            ]
            for item in clauses
        }
        missing_containment = sorted(entity_id for entity_id, edge_ids in containment.items() if not edge_ids)
        check(
            specification["label"] + " source Agreement is adopted",
            len(agreements) == 1,
            {"matching_agreements": 1, "source_titles": source_titles, "identity_field": title_field},
            [
                {
                    "entity_id": item["entity_id"],
                    "identity": item["identity"],
                    "properties": item["properties"],
                }
                for item in agreements
            ],
        )
        check(
            specification["label"] + " source parties are linked by PARTY_TO",
            len(agreement_ids) == 1 and len(linked_party_ids) >= 2 and all(group_matches.values()),
            {
                "minimum_distinct_parties": 2,
                "explicit_source_mentions": specification["parties"],
                "direction": "Party -> corresponding Agreement",
                "identity_equivalence": "requires human review",
            },
            {
                "party_count": len(parties),
                "linked_party_ids": sorted(linked_party_ids),
                "source_group_matches": group_matches,
                "relations": party_to,
            },
        )
        check(
            specification["label"] + " adopted clauses all have CONTAINS",
            len(agreement_ids) == 1 and bool(clauses) and not missing_containment,
            "nonzero adopted Clause set; each has Agreement -> Clause CONTAINS from its source agreement",
            {
                "adopted_clause_count": len(clauses),
                "containment": containment,
                "missing_clause_ids": missing_containment,
                "scope": "adopted graph structural coverage",
            },
        )
        documents[specification["file"]] = {
            "document_id": document_id,
            "source_titles": source_titles,
            "agreement_ids": sorted(agreement_ids),
            "parties": parties,
            "clauses": clauses,
            "relations": relations,
            "containment": containment,
            "missing_containment": missing_containment,
        }

    hosting = documents["galacticomm-web-hosting.txt"]
    original = by_document[hosting["document_id"]]
    payment = source_paragraph(original, "Horst Entertainment agrees to pay")
    support = source_paragraph(original, "Galacticomm must provide")
    purchase = source_paragraph(original, "In the event Galacticomm, Inc. chooses to terminate")

    def clauses_containing(paragraph):
        return [item for item in hosting["clauses"] if any(paragraph in text for text in definitions(item))]

    obligations = []
    for name, legal_name, paragraph in (
        ("payer", "Horst Entertainment", payment),
        ("provider technical support", "Galacticomm", support),
        ("customer technical support", "Horst Entertainment", support),
    ):
        actors = [
            item for item in hosting["parties"] if item["identity"]["fields"][party_field] == legal_name
        ]
        targets = clauses_containing(paragraph)
        links = [
            edge
            for edge in hosting["relations"]
            if edge["relation_type"] == "OBLIGATED_BY"
            and edge["source_id"] in {item["entity_id"] for item in actors}
            and edge["target_id"] in {item["entity_id"] for item in targets}
        ]
        actual = {
            "source_legal_name": legal_name,
            "source_paragraph": paragraph,
            "actor_ids": [item["entity_id"] for item in actors],
            "complete_source_clause_ids": [item["entity_id"] for item in targets],
            "relations": links,
        }
        obligations.append({"name": name, **actual})
        check(
            "hosting " + name + " is linked by OBLIGATED_BY",
            bool(actors) and bool(targets) and bool(links),
            {
                "source_subject_exact_name": legal_name,
                "direction": "obligated Party -> complete source Clause",
                "source_paragraph": paragraph,
            },
            actual,
        )
    payment_ids = {item["entity_id"] for item in clauses_containing(payment)}
    purchase_ids = {item["entity_id"] for item in clauses_containing(purchase)}
    names = {item["entity_id"]: item["identity"]["fields"][party_field] for item in hosting["parties"]}
    misused_obligations = [
        edge
        for edge in hosting["relations"]
        if edge["relation_type"] == "OBLIGATED_BY"
        and (
            (edge["target_id"] in payment_ids and names.get(edge["source_id"]) != "Horst Entertainment")
            or edge["target_id"] in purchase_ids
        )
    ]
    check(
        "hosting OBLIGATED_BY does not stand for payee or purchase-option holder",
        bool(payment_ids) and bool(purchase_ids) and not misused_obligations,
        "payment obligation belongs to Horst Entertainment; the termination software purchase option is a right, not an obligation",
        {
            "payment_clause_ids": sorted(payment_ids),
            "purchase_option_clause_ids": sorted(purchase_ids),
            "misused_relations": misused_obligations,
        },
    )
    hosting_agreements = [
        item for item in snapshot["entities"] if item["entity_id"] in hosting["agreement_ids"]
    ]
    check(
        "hosting date fields do not invent a basis or century",
        bool(hosting_agreements)
        and all(
            not property_values(item, "as_of_date") and not property_values(item, "signing_date")
            for item in hosting_agreements
        ),
        "the source has no as-of date and only the two-digit signing year 9/9/97; preserve it in source, without guessing an ISO-date century",
        [
            {
                "entity_id": item["entity_id"],
                "signing_date": property_values(item, "signing_date"),
                "as_of_date": property_values(item, "as_of_date"),
            }
            for item in hosting_agreements
        ],
    )

    difficult_clauses = []
    for name, anchor, reason in (
        (
            "additional fees cross-clause scope",
            "There shall be no additional fees",
            "The exception points to software/bandwidth charges stated elsewhere in the agreement; completeness needs supported targets.",
        ),
        (
            "prior agreements cutoff reference",
            "This agreement shall void and nullify",
            "The phrase to this date needs a reliable source-supported cutoff; a signature date alone must not silently resolve it.",
        ),
    ):
        paragraph = source_paragraph(original, anchor)
        adopted = clauses_containing(paragraph)
        matching_issues = [
            issue
            for issue in issues
            if issue["document_id"] == hosting["document_id"]
            and issue.get("candidate", {}).get("type") == "Clause"
            and anchor in json.dumps(issue["candidate"], ensure_ascii=False)
        ]
        disposition = {
            "name": name,
            "source_paragraph": paragraph,
            "completeness_concern": reason,
            "adopted_clause_ids": [item["entity_id"] for item in adopted],
            "candidate_issues": matching_issues,
            "disposition": "adopted_requires_reference_review" if adopted else "not_adopted",
            "human_review": {"status": "pending", "conclusion": None},
        }
        difficult_clauses.append(disposition)
        if not adopted:
            check(
                "unadopted " + name + " has an explicit issue",
                bool(matching_issues) and all(issue.get("reason_code") for issue in matching_issues),
                "an omitted difficult source Clause has its own rejection reason; no target Clause count is imposed",
                disposition,
            )
    return {
        "scope": "structural coverage of adopted objects plus three fixed source obligations; not whole-source semantic recall",
        "documents": documents,
        "obligations": obligations,
        "difficult_clauses": difficult_clauses,
        "checks": checks,
        "whole_source_semantic_recall": "requires independent human review",
    }


async def run(runtime):
    e, api = runtime.e, runtime.api
    kb = await runtime.kb("public-contract", model())
    runtime.start_worker()
    imported = {}
    issues = []
    source_manifest = json.loads((CORPUS / "source-manifest.json").read_text("utf-8"))
    for source in source_manifest["files"]:
        path = CORPUS / source["path"]
        document = await api.upload(
            "contract." + source["path"],
            kb,
            path,
            source={
                "dataset": source_manifest["dataset"],
                "license": source_manifest["license"],
                "source_url": source["source_url"],
                "encoding": "utf-8",
            },
        )
        job = await api.wait_job(document["job_id"])
        imported[source["path"]] = {**document, "job": job}
        response = await api.client.get(f"/kbs/{kb}/documents/{document['document_id']}/content")
        evidence = e.record(
            "responses",
            {
                "protocol": "HTTP",
                "operation": "original content byte comparison",
                "document_id": document["document_id"],
                "file": source["path"],
                "status_code": response.status_code,
                "expected_bytes": len(path.read_bytes()),
                "actual_bytes": len(response.content),
                "bytes_equal": response.content == path.read_bytes(),
            },
        )
        e.check(
            "original bytes preserved " + source["path"],
            response.status_code == 200
            and response.content == path.read_bytes()
            and len(response.content) == source["byte_length"],
            source["byte_length"],
            {"length": len(response.content), "bytes_equal": response.content == path.read_bytes()},
            evidence=evidence,
        )
        page = await api.request("GET", f"/jobs/{document['job_id']}/issues", params={"limit": 1000})
        issues.extend(page)
    snapshot, snapshot_record = await runtime.snapshot(kb, "full public contracts extracted")
    by_document = {
        value["document_id"]: (CORPUS / filename).read_text("utf-8") for filename, value in imported.items()
    }
    citation_audit = audit_source_evidence(snapshot, by_document)
    audit_record = e.record("snapshots", {"label": "persistent contract citation audit", **citation_audit})
    e.check(
        "contract source coordinates and exact quotes",
        citation_audit["source_spans"] > 0
        and citation_audit["fact_quotes_verified"] > 0
        and not citation_audit["errors"],
        "nonzero fact quotes match their exact quote ranges; complete fact/context mappings match unchanged sources",
        citation_audit,
        evidence=audit_record,
    )
    e.check(
        "context citations store coordinates without copied body",
        citation_audit["context_references"] > 0
        and citation_audit["contexts_verified"] == citation_audit["context_references"]
        and not citation_audit["context_body_errors"],
        "nonzero context references with complete verified mappings and no quote/quote_range/quote_truncated/text fields",
        {
            key: citation_audit[key]
            for key in ("context_references", "contexts_verified", "context_body_errors")
        },
        evidence=audit_record,
    )
    clauses = [item for item in snapshot["entities"] if item["entity_type"] == "Clause"]
    e.check(
        "real extraction adopts complete rule entities",
        bool(clauses),
        "at least one source-supported Clause",
        {
            "clause_count": len(clauses),
            "counts": {filename: value["job"].get("counts") for filename, value in imported.items()},
        },
        evidence=snapshot_record,
    )
    graph_audit = audit_business_graph(snapshot, imported, by_document, issues)
    graph_record = e.record("snapshots", {"label": "contract business graph coverage audit", **graph_audit})
    for item in graph_audit["checks"]:
        e.check(item["name"], item["passed"], item["expected"], item["actual"], evidence=graph_record)
    e.pending(
        "whole-source contract semantic recall review",
        "independently inspect all source parties, clauses and relations; adopted-clause connectivity is not full-source recall",
        {"graph_scope": graph_audit["scope"], "difficult_clauses": graph_audit["difficult_clauses"]},
        evidence="contract-review.json",
    )
    detail_records = {}
    for entity in snapshot["entities"]:
        detail = await api.request(
            "GET", f"/kbs/{kb}/knowledge/entity/{entity['entity_id']}", params={"evidence_limit": 1000}
        )
        detail_records[entity["entity_id"]] = {"detail": detail, "evidence": api.last_record}
        if entity["entity_type"] == "Clause":
            complete = [item["value"] for item in entity["atomic_groups"]["full_statement"]["declarations"]]
            exact = all(
                any(text.strip() in by_document[document_id] for document_id in entity["document_ids"])
                for text in definitions(entity)
            )
            e.check(
                "complete source clause " + entity["entity_id"],
                bool(complete)
                and exact
                and all(item["complete"] and item["references_resolved"] for item in complete),
                "verbatim contiguous full definition with complete source support",
                {"definitions": definitions(entity), "atomic_values": complete},
                evidence=api.last_record,
            )

    review = []
    for expectation in EXPECTATIONS:
        document_id = imported[expectation["file"]]["document_id"]
        matches = [
            entity
            for entity in clauses
            if entity["document_ids"] == [document_id]
            and any(contains_all(text, expectation["groups"]) for text in definitions(entity))
        ]
        actual = [
            {
                "entity_id": entity["entity_id"],
                "full_definitions": definitions(entity),
                "detail_record": detail_records[entity["entity_id"]]["evidence"],
            }
            for entity in matches
        ]
        e.check(
            "fixed source terms retained together " + expectation["name"],
            bool(matches),
            expectation["expected"],
            actual,
            evidence=snapshot_record,
        )
        entry = {
            "name": expectation["name"],
            "expected": expectation["expected"],
            "file": expectation["file"],
            "document_id": document_id,
            "machine_term_check_passed": bool(matches),
            "actual_matches": actual,
            "all_document_clause_ids": [
                entity["entity_id"] for entity in clauses if document_id in entity["document_ids"]
            ],
            "candidate_issues": [item for item in issues if item["document_id"] == document_id],
            "human_review": {"status": "pending", "conclusion": None},
        }
        review.append(entry)
        e.pending(
            "human semantic review " + expectation["name"],
            expectation["expected"],
            actual,
            evidence="contract-review.json",
        )
    filing_doc = imported["rmr-joint-filing.txt"]["document_id"]
    date_roles = audit_filing_date_roles(snapshot, filing_doc, by_document[filing_doc])
    e.check(
        date_roles["name"],
        date_roles["passed"],
        date_roles["expected"],
        date_roles["actual"],
        evidence=snapshot_record,
    )
    parties = [entity for entity in snapshot["entities"] if entity["entity_type"] == "Party"]
    e.pending(
        "party spelling and payer/payee identity review",
        "付款方/收款方及名称拼写差异有明确身份依据；不猜测 Galacticomm/Galactcomm/Galaticomm 身份合并。",
        {"parties": parties, "relations": snapshot["relations"], "candidate_issues": issues},
        evidence=snapshot_record,
    )
    e.pending(
        "missing-identity and incomplete-rule rejection review",
        "实际候选中缺可靠身份或完整性的内容明确未采用；不能以无候选推断此行为已被真实语料验证。",
        {"issues": issues, "issue_count": len(issues)},
        evidence=snapshot_record,
    )
    write_json(
        e.directory / "contract-review.json",
        {
            "kb_id": kb,
            "sources": source_manifest,
            "expectations": review,
            "parties": parties,
            "date_check": date_roles,
            "candidate_issues": issues,
            "business_graph_audit": graph_record,
            "difficult_clauses": graph_audit["difficult_clauses"],
            "whole_source_semantic_recall": "pending independent human review",
            "snapshot": snapshot_record,
        },
    )

    hosting_id = imported["galacticomm-web-hosting.txt"]["document_id"]
    query = "Return the complete per-access payment rates, access thresholds, prior-week period, and payment deadline."
    filters = {"document_ids": [hosting_id]}
    document_result = await api.paid_request(
        "contract.document_baseline",
        f"/kbs/{kb}/knowledge/search",
        {"query": query, "kinds": ["chunk"], "filters": filters, "semantic": True, "limit": 10},
    )
    graph_result = await api.paid_request(
        "contract.object_baseline",
        f"/kbs/{kb}/knowledge/search",
        {"query": query, "kinds": ["entity"], "filters": filters, "semantic": True, "limit": 10},
    )
    combined = await api.paid_request(
        "contract.combined",
        f"/kbs/{kb}/retrieve",
        {"query": query, "filters": filters, "evidence_limit": 1000},
    )
    e.check(
        "contract semantic routes execute without silent downgrade",
        all(
            not any(
                warning["code"] in {"SEMANTIC_UNAVAILABLE", "PLAN_UNAVAILABLE"}
                for warning in result["warnings"]
            )
            for result in (document_result, graph_result, combined)
        ),
        "real document/object/combined routes without downgrade",
        {"document": document_result, "object": graph_result, "combined": combined},
        evidence=e.record(
            "snapshots",
            {
                "label": "three retrieval routes",
                "document": document_result,
                "object": graph_result,
                "combined": combined,
            },
        ),
    )
    expected_rate = EXPECTATIONS[0]
    ranks = []
    for index, item in enumerate(graph_result["knowledge"], 1):
        entity = next((value for value in clauses if value["entity_id"] == item.get("entity_id")), None)
        if entity and any(contains_all(text, expected_rate["groups"]) for text in definitions(entity)):
            ranks.append(index)
    write_json(
        e.directory / "contract-metrics.json",
        {
            "scope": "two fixed public source documents; lexical/reference checks require human semantic review",
            "entities": len(snapshot["entities"]),
            "relations": len(snapshot["relations"]),
            "clauses": len(clauses),
            "contributions": len(snapshot["contributions"]),
            "adopted_source_quote_checks": citation_audit["fact_quotes_verified"],
            "fact_references": citation_audit["fact_references"],
            "legacy_references": citation_audit["legacy_references"],
            "context_references": citation_audit["context_references"],
            "verified_context_references": citation_audit["contexts_verified"],
            "context_body_copy_errors": len(citation_audit["context_body_errors"]),
            "business_graph_check_count": len(graph_audit["checks"]),
            "business_graph_check_failures": [
                item["name"] for item in graph_audit["checks"] if not item["passed"]
            ],
            "adopted_clause_containment": {
                filename: {
                    "adopted_clauses": len(item["clauses"]),
                    "missing_CONTAINS": len(item["missing_containment"]),
                }
                for filename, item in graph_audit["documents"].items()
            },
            "whole_source_semantic_recall": "not_estimated",
            "source_coordinate_errors": len(citation_audit["errors"]),
            "fixed_term_groups_retained": sum(entry["machine_term_check_passed"] for entry in review),
            "fixed_term_groups_total": len(review),
            "payment_rule_candidate_ranks_by_fixed_terms": ranks,
            "payment_rule_reciprocal_rank_by_fixed_terms": 1 / min(ranks) if ranks else 0,
            "document_candidates": len(document_result["knowledge"]),
            "object_candidates": len(graph_result["knowledge"]),
            "combined_status": combined["status"],
            "semantic_accuracy": "not_estimated",
            "human_review": "pending",
            "monetary_cost": "not_measured",
        },
    )
