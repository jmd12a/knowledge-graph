"""在同一份基础草稿上对照是否启用全篇关系审查。"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import partial
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from knowledge_graph.domain import canonical
from knowledge_graph.services.construction.extraction import extract_text

from . import quality_terra_construction as bench
from .quality_terra_document_audit import _SYSTEM, _audit

_AUDIT_PROMPT = _SYSTEM.replace(
    "The app has produced no cross-window relation candidates for you.",
    "The draft includes the output of the usual extraction and cross-window linking stages.",
)


def _freeze(checkpoint, _loop_calls):
    """共享首抽、补全和跨窗连接的输出，避免按组重抽。"""
    return {
        "_input_scopes": deepcopy(checkpoint["_input_scopes"]),
        "_shared_draft": deepcopy(checkpoint["_shared_draft"]),
    }, {}


def _inject_faults(draft, document, faults):
    """在隔离对照中修改双方共用的 CALLS 草稿，检查找漏与方向纠错。"""
    if not faults or Path(document["raw_path"]).name != faults["source"]:
        return False
    identities = {
        entity["ref"]: entity["properties"].get("app_id")
        for entity in draft["entities"]
        if entity["type"] == "Service"
    }
    reverse, omit = tuple(faults["reverse"]), tuple(faults["omit"])
    applied, relations = set(), []
    for relation in draft["relations"]:
        pair = identities.get(relation["source_ref"]), identities.get(relation["target_ref"])
        if relation["type"] == "CALLS" and pair == omit:
            applied.add("omit")
            continue
        if relation["type"] == "CALLS" and pair == reverse:
            relation = relation | {
                "source_ref": relation["target_ref"],
                "target_ref": relation["source_ref"],
            }
            applied.add("reverse")
        relations.append(relation)
    if applied != {"reverse", "omit"}:
        raise RuntimeError(f"Fault probe lacks an expected original edge: {sorted(applied)}")
    draft["relations"] = relations
    return True


async def _extract(language, parsed, model, checkpoint, persist, *, loop_calls=0, faults=None, **kwargs):
    if not loop_calls:
        draft = await extract_text(language, parsed, model, checkpoint, persist, **kwargs)
        if _inject_faults(draft, kwargs.get("review_document"), faults):
            checkpoint["_injected_faults"] = faults
        checkpoint["_shared_draft"] = deepcopy(draft)
    else:
        draft = deepcopy(checkpoint["_shared_draft"])
    checkpoint["_shared_draft_sha256"] = sha256(canonical(draft).encode()).hexdigest()
    await persist()
    if loop_calls and model.relation_types:
        await _audit(
            language,
            parsed,
            model,
            checkpoint,
            persist,
            draft,
            loop_calls,
            kwargs.get("published_graph"),
            kwargs.get("review_document"),
            system=_AUDIT_PROMPT,
        )
    return draft


async def _sequence(settings, output, report, folder, *, loop_calls, faults=None):
    report["experiment"] = {
        "cross_window_linking": True,
        "endpoint_identity_evidence": True,
        "shared_draft": True,
        "seed_catalog_shared": True,
        "treatment": "autonomous_document_audit",
    }
    if faults:
        report["experiment"]["injected_faults"] = faults
    await _run_sequence(settings, output, report, folder, loop_calls=loop_calls)


_run_sequence = bench._sequence


def main():
    probe = os.environ.get("KG_EVAL_AUDIT_FAULTS_FILE")
    faults = json.loads(Path(probe).read_text()) if probe else None
    with (
        patch.object(bench, "_frozen", _freeze),
        patch.object(bench, "extract_text", partial(_extract, faults=faults)),
        patch.object(bench, "_sequence", partial(_sequence, faults=faults)),
    ):
        bench.main()


if __name__ == "__main__":
    main()
