"""准备并选择固定写入范围；恢复时只重放已保存的范围。"""

from __future__ import annotations


COLLECTIONS = (
    ("entities", "entity_id", "entity_ids"),
    ("relations", "relation_id", "relation_ids"),
    ("chunk_points", "id", "chunk_ids"),
    ("object_points", "id", "object_ids"),
)


def build_write_set(plan: dict, previous: dict) -> dict:
    """完整比较业务对象和向量载荷，包含证据及来源变化。"""
    scope = {}
    for collection, id_field, scope_field in COLLECTIONS:
        old = (
            {item[id_field]: item for item in previous[collection]}
            if plan["write_mode"] == "delta" else {}
        )
        scope[scope_field] = sorted(
            item[id_field] for item in plan[collection]
            if plan["write_mode"] == "full" or item != old.get(item[id_field])
        )
    documents = set(plan["activate_documents"]) | set(plan["delete_documents"])
    if plan["write_mode"] == "full":
        documents.update(plan["document_ids"])
    scope["contribution_document_ids"] = sorted(documents)
    return scope


def select_writes(plan: dict) -> dict:
    if plan.get("write_mode", "full") != "delta":
        return plan
    scope = plan["write_set"]
    writes = {}
    for collection, id_field, scope_field in COLLECTIONS:
        selected = set(scope[scope_field])
        writes[collection] = [item for item in plan[collection] if item[id_field] in selected]
    documents = set(scope["contribution_document_ids"])
    writes["contributions"] = [
        claim for claim in plan["contributions"] if claim["document_id"] in documents
    ]
    return writes
