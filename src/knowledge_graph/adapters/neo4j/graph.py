"""以固定 Cypher 模板读写当前图知识，管理约束、检索字段和批量写入。"""

from __future__ import annotations

import json
import os
import re

from neo4j import AsyncGraphDatabase, Query
from neo4j.exceptions import DriverError, Neo4jError

from ...domain import BusinessModel, KGError, canonical, valid_id


class Graph:
    def __init__(self, settings):
        self.settings = settings
        self.driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, os.getenv(settings.neo4j_password_env, ""))
        )

    async def close(self):
        await self.driver.close()

    async def ready(self):
        await self.driver.verify_connectivity()
        constraints = await self._run("SHOW CONSTRAINTS YIELD name RETURN name")
        expected = {"kg_entity_id", "kg_entity_identity", "kg_relation_id", "kg_relation_identity"}
        indexes = await self._run("SHOW INDEXES YIELD name,state WHERE name='kg_entity_text' RETURN state")
        return expected <= {row["name"] for row in constraints} and bool(
            indexes and indexes[0]["state"] == "ONLINE"
        )

    async def _run(self, query, **parameters):
        """为一次图操作创建并关闭会话，消费结果后统一转换驱动错误。"""
        try:
            async with self.driver.session(database=self.settings.neo4j_database) as session:
                result = await session.run(Query(query, timeout=self.settings.graph_timeout), **parameters)
                return [record.data() async for record in result]
        except (Neo4jError, DriverError) as exc:
            timeout = (
                "timeout" in str(getattr(exc, "code", "")).lower()
                or "timedout" in str(getattr(exc, "code", "")).lower()
            )
            raise KGError(
                "TIMEOUT" if timeout else "UPSTREAM_UNAVAILABLE",
                "Graph storage request could not complete",
                504 if timeout else 503,
                retryable=True,
            ) from exc

    async def initialize(self):
        """创建应用所需约束和全文索引，并等待索引可用。"""
        statements = [
            "CREATE CONSTRAINT kg_entity_id IF NOT EXISTS FOR (n:KGEntity) REQUIRE (n.kb_id,n.entity_id) IS UNIQUE",
            (
                "CREATE CONSTRAINT kg_entity_identity IF NOT EXISTS FOR (n:KGEntity) REQUIRE (n.kb_id,"
                "n.entity_type,n.identity_key) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT kg_relation_id IF NOT EXISTS FOR ()-[r:KG_REL]-() REQUIRE (r.kb_id,"
                "r.relation_id) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT kg_relation_identity IF NOT EXISTS FOR ()-[r:KG_REL]-() REQUIRE (r.kb_id,"
                "r.relation_key) IS UNIQUE"
            ),
            (
                "CREATE FULLTEXT INDEX kg_entity_text IF NOT EXISTS FOR (n:KGEntity)"
                " ON EACH [n.canonical_name,n.aliases,n.search_text]"
            ),
        ]
        for statement in statements:
            await self._run(statement)
        await self._run("CALL db.awaitIndexes(60)")

    async def all(self, kb_id):
        valid_id(kb_id)
        entities = await self._run("MATCH (n:KGEntity {kb_id:$kb}) RETURN n.data_json AS data", kb=kb_id)
        relations = await self._run(
            (
                "MATCH (a:KGEntity {kb_id:$kb})-[r:KG_REL {kb_id:$kb}]->(b:KGEntity {kb_id:$kb})"
                " RETURN r.data_json AS data"
            ),
            kb=kb_id,
        )
        return [json.loads(x["data"]) for x in entities], [json.loads(x["data"]) for x in relations]

    async def get(self, kb_id, kind, object_id):
        if kind == "entity":
            rows = await self._run(
                "MATCH (n:KGEntity {kb_id:$kb,entity_id:$id}) RETURN n.data_json AS data",
                kb=kb_id,
                id=object_id,
            )
        else:
            rows = await self._run(
                (
                    "MATCH (a:KGEntity {kb_id:$kb})-[r:KG_REL {kb_id:$kb,relation_id:$id}]->(b:KGEntity {kb_id:$kb})"
                    " RETURN r.data_json AS data"
                ),
                kb=kb_id,
                id=object_id,
            )
        return json.loads(rows[0]["data"]) if rows else None

    async def find_identity(self, kb_id, entity_type, identity):
        rows = await self._run(
            "MATCH (n:KGEntity {kb_id:$kb,entity_type:$type,identity_key:$identity}) RETURN n.entity_id AS id",
            kb=kb_id,
            type=entity_type,
            identity=canonical(identity),
        )
        return rows[0]["id"] if rows else None

    @staticmethod
    def _filters(entity_types, domain_ids, document_ids):
        return {"types": entity_types or [], "domains": domain_ids or [], "documents": document_ids or []}

    async def by_id(self, kb_id, kind, ids):
        if not ids:
            return []
        query = (
            "UNWIND $keys AS id MATCH (n:KGEntity {kb_id:$kb,entity_id:id}) RETURN n.data_json AS data"
            if kind == "entity"
            else "UNWIND $keys AS id MATCH ()-[r:KG_REL {kb_id:$kb,relation_id:id}]->() RETURN r.data_json AS data"
        )
        rows = await self._run(query, kb=kb_id, keys=sorted(set(ids)))
        return [json.loads(row["data"]) for row in rows]

    async def by_identity(self, kb_id, kind, keys):
        if not keys:
            return []
        query = (
            "UNWIND $keys AS key MATCH (n:KGEntity {kb_id:$kb,entity_type:key[0],identity_key:key[1]})"
            " RETURN n.data_json AS data"
            if kind == "entity"
            else "UNWIND $keys AS key MATCH ()-[r:KG_REL {kb_id:$kb,relation_key:key}]->() RETURN r.data_json AS data"
        )
        rows = await self._run(query, kb=kb_id, keys=list(keys))
        return [json.loads(row["data"]) for row in rows]

    async def relation_groups(self, kb_id, groups):
        if not groups:
            return []
        rows = await self._run(
            "UNWIND $groups AS item MATCH (a:KGEntity {kb_id:$kb,entity_id:item[0]})"
            "-[r:KG_REL {kb_id:$kb,relation_type:item[1]}]->(:KGEntity {kb_id:$kb})"
            " RETURN r.data_json AS data",
            kb=kb_id,
            groups=groups,
        )
        return [json.loads(row["data"]) for row in rows]

    async def counts(self, kb_id):
        rows = await self._run(
            "MATCH (n:KGEntity {kb_id:$kb}) WITH count(n) AS total RETURN 'entities' AS kind,total "
            "UNION ALL MATCH ()-[r:KG_REL {kb_id:$kb}]->() "
            "WITH count(r) AS total RETURN 'relations' AS kind,total",
            kb=kb_id,
        )
        return {row["kind"]: row["total"] for row in rows}

    async def by_property_value(self, kb_id, entity_type, field, value, document_ids=None):
        """Read exact confirmed scalar matches with a second row for ambiguity detection."""
        valid_id(kb_id)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field):
            raise KGError("INVALID_ARGUMENT", "Invalid identity discriminator field")
        rows = await self._run(
            "MATCH (n:KGEntity {kb_id:$kb,entity_type:$type})"
            + f" WHERE n.p_{field}=$value"
            + " AND (size($documents)=0 OR any(d IN n.document_ids WHERE d IN $documents))"
            + " RETURN n.data_json AS data ORDER BY n.entity_id LIMIT 2",
            kb=kb_id,
            type=entity_type,
            value=value,
            documents=document_ids or [],
        )
        return [json.loads(row["data"]) for row in rows]

    async def candidates(
        self, kb_id, text, scan_limit, entity_types=None, domain_ids=None, document_ids=None
    ):
        """在相同过滤与预算下分别读取精确候选和关键词候选。"""
        valid_id(kb_id)
        if (
            isinstance(scan_limit, bool)
            or not isinstance(scan_limit, int)
            or not 1 <= scan_limit <= self.settings.candidate_scan_limit
        ):
            raise KGError("INVALID_ARGUMENT", "Invalid candidate scan budget")
        restriction = (
            " AND (size($types)=0 OR n.entity_type IN $types) AND (size($domains)=0 OR any(d IN n.domain_ids"
            " WHERE d IN $domains)) AND (size($documents)=0 OR any(d IN n.document_ids"
            " WHERE d IN $documents)) "
        )
        args = self._filters(entity_types, domain_ids, document_ids)
        exact = await self._run(
            (
                "MATCH (n:KGEntity {kb_id:$kb})"
                " WHERE (n.canonical_name=$text OR $text IN n.aliases OR $text IN n.identity_values)"
            )
            + restriction
            + "RETURN n.data_json AS data ORDER BY n.canonical_name,n.entity_id LIMIT $limit",
            kb=kb_id,
            text=text,
            limit=scan_limit + 1,
            **args,
        )
        keyword = await self._run(
            "MATCH (n:KGEntity {kb_id:$kb}) WHERE toLower(n.search_text) CONTAINS toLower($text)"
            + restriction
            + "RETURN n.data_json AS data ORDER BY n.canonical_name,n.entity_id LIMIT $limit",
            kb=kb_id,
            text=text,
            limit=scan_limit + 1,
            **args,
        )
        return [json.loads(x["data"]) for x in exact], [json.loads(x["data"]) for x in keyword]

    async def fulltext(self, kb_id, text, scan_limit, entity_types=None, domain_ids=None, document_ids=None):
        """按词构造全文查询，过滤知识库及范围后再应用候选预算。"""
        valid_id(kb_id)
        if (
            isinstance(scan_limit, bool)
            or not isinstance(scan_limit, int)
            or not 1 <= scan_limit <= self.settings.candidate_scan_limit
        ):
            raise KGError("INVALID_ARGUMENT", "Invalid full-text candidate scan budget")
        import re

        tokens = re.findall(r"\w+", text, flags=re.UNICODE)
        if not tokens:
            return []
        query_text = " OR ".join('"' + token + '"' for token in tokens)
        # 范围过滤必须先于预算截断，避免其他知识库占用候选名额。
        rows = await self._run(
            (
                "CALL db.index.fulltext.queryNodes('kg_entity_text',$text) YIELD node,score WHERE node.kb_id=$kb"
                " AND (size($types)=0 OR node.entity_type IN $types)"
                " AND (size($domains)=0 OR any(d IN node.domain_ids WHERE d IN $domains))"
                " AND (size($documents)=0 OR any(d IN node.document_ids WHERE d IN $documents))"
                " RETURN node.data_json AS data,score ORDER BY score DESC,node.entity_id LIMIT $limit"
            ),
            kb=kb_id,
            text=query_text,
            limit=scan_limit + 1,
            **self._filters(entity_types, domain_ids, document_ids),
        )
        return [json.loads(x["data"]) for x in rows]

    async def adjacent(self, kb_id, node_id, direction, relation_types, limit):
        valid_id(kb_id)
        valid_id(node_id)
        if (
            direction not in {"outgoing", "incoming", "both"}
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1001
        ):
            raise KGError("INVALID_ARGUMENT", "Invalid graph direction or edge scan budget")
        pattern = {
            "outgoing": "(a)-[r:KG_REL]->(b)",
            "incoming": "(a)<-[r:KG_REL]-(b)",
            "both": "(a)-[r:KG_REL]-(b)",
        }[direction]
        rows = await self._run(
            "MATCH "
            + pattern
            + (
                " WHERE a:KGEntity AND b:KGEntity AND a.kb_id=$kb AND b.kb_id=$kb AND r.kb_id=$kb"
                " AND a.entity_id=$id AND (size($types)=0 OR r.relation_type IN $types)"
                " RETURN b.data_json AS node,r.data_json AS edge ORDER BY r.relation_id LIMIT $limit"
            ),
            kb=kb_id,
            id=node_id,
            types=relation_types,
            limit=limit,
        )
        return [(json.loads(row["node"]), json.loads(row["edge"])) for row in rows]

    @staticmethod
    def _properties(item, model):
        """将完整对象映射为图属性，并只展开模型声明为可查询的业务字段。"""
        kind = "entity" if "entity_id" in item else "relation"
        props = {
            "kb_id": item["kb_id"],
            "data_json": canonical(item),
            "object_revision": item["object_revision"],
            "contribution_ids": item["contribution_ids"],
            "domain_ids": item.get("domain_ids", []),
            "search_text": item.get("search_text", ""),
            "document_ids": item.get("document_ids", []),
        }
        if kind == "entity":
            props |= {
                "entity_id": item["entity_id"],
                "entity_type": item["entity_type"],
                "identity_key": canonical(item["identity"]),
                "identity_json": canonical(item["identity"]),
                "identity_values": [str(v) for v in item["identity"]["fields"].values()],
                "canonical_name": item["canonical_name"],
                "aliases": item["aliases"],
                "completeness": item["completeness"],
                "missing_fields": item["missing_fields"],
                "properties_json": canonical(item["properties"]),
                "conflicts_json": canonical(item["conflicts"]),
            }
            definitions = model.node_types[item["entity_type"]].properties
        else:
            props |= {
                "relation_id": item["relation_id"],
                "relation_type": item["relation_type"],
                "relation_key": canonical(item["identity"]),
                "properties_json": canonical(item["properties"]),
                "conflicts_json": canonical(item["conflicts"]),
            }
            definitions = model.relation_types[item["relation_type"]].properties

        for name, definition in definitions.items():
            if definition.queryable:
                value = item["properties"].get(name, {})
                values = [x["value"] for x in value.get("values", [])]
                if values:
                    props["pv_" + name] = values
                if value.get("state") == "confirmed" and len(values) == 1:
                    props["p_" + name] = values[0]
        return props

    async def apply(
        self, kb_id, model_data, entities, relations, delete_entities, delete_relations, rebuild=False
    ):
        """按实体、关系及删除依赖顺序分批应用计划，支持按原 ID 重放。"""
        model = BusinessModel.model_validate(model_data)
        if rebuild:
            # 重建时先删除关系，再删除实体，避免残留边阻止节点删除。
            await self._run(
                "MATCH (:KGEntity {kb_id:$kb})-[r:KG_REL {kb_id:$kb}]->(:KGEntity {kb_id:$kb}) DELETE r",
                kb=kb_id,
            )
            await self._run("MATCH (n:KGEntity {kb_id:$kb}) DELETE n", kb=kb_id)

        # 先建立全部实体，再写入依赖端点的关系。
        for batch in self._batches(entities):
            await self._run(
                "UNWIND $rows AS row MERGE (n:KGEntity {kb_id:$kb,entity_id:row.id}) SET n=row.props",
                kb=kb_id,
                rows=[{"id": x["entity_id"], "props": self._properties(x, model)} for x in batch],
            )

        for batch in self._batches(relations):
            rows = [
                {
                    "id": x["relation_id"],
                    "source": x["source_id"],
                    "target": x["target_id"],
                    "props": self._properties(x, model),
                }
                for x in batch
            ]
            await self._run(
                (
                    "UNWIND $rows AS row MATCH (a:KGEntity {kb_id:$kb,entity_id:row.source}),(b:KGEntity {kb_id:$kb,"
                    "entity_id:row.target}) MERGE (a)-[r:KG_REL {kb_id:$kb,relation_id:row.id}]->(b) SET r=row.props"
                ),
                kb=kb_id,
                rows=rows,
            )

        # 删除先撤关系；仍被任意关系引用的实体由查询条件保护。
        if delete_relations:
            await self._run(
                (
                    "MATCH (:KGEntity {kb_id:$kb})-[r:KG_REL {kb_id:$kb}]->(:KGEntity {kb_id:$kb})"
                    " WHERE r.relation_id IN $ids DELETE r"
                ),
                kb=kb_id,
                ids=delete_relations,
            )
        if delete_entities:
            await self._run(
                "MATCH (n:KGEntity {kb_id:$kb}) WHERE n.entity_id IN $ids AND NOT (n)--() DELETE n",
                kb=kb_id,
                ids=delete_entities,
            )

    @staticmethod
    def _batches(items, size=100):
        return (items[i : i + size] for i in range(0, len(items), size))
