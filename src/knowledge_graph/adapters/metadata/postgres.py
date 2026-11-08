"""用短 PostgreSQL 事务保存配置、资料、任务和来源贡献，协调维护恢复状态。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.rows import dict_row

from ...domain import KGError, canonical, new_id, utcnow
from ...domain.jobs import SUCCESS_STATES

SCHEMA = (Path(__file__).parent / "migrations" / "001.sql").read_text("utf-8")
_QUEUE_HEAD = (
    " AND NOT EXISTS (SELECT 1 FROM jobs earlier WHERE earlier.kb_id=j.kb_id"
    " AND earlier.queue_seq<j.queue_seq AND earlier.state IN"
    " ('QUEUED','PREPARING','PREPARED','DRAINING','APPLYING','VERIFYING','NEEDS_REPAIR','CLEANING','CLEANUP_FAILED'))"
)
_FIRST_PUBLICATION = "j.state='PREPARED' AND j.last_step IN ('prepared','publish_draining')"


class Metadata:
    def __init__(self, dsn_env: str):
        self.dsn_env = dsn_env

    def _connect(self):
        dsn = os.getenv(self.dsn_env)
        if not dsn:
            raise KGError(
                "STORAGE_NOT_CONFIGURED",
                f"PostgreSQL DSN environment variable is missing: {self.dsn_env}",
                503,
            )
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5)

    def initialize(self):
        with self._connect() as conn, conn.transaction():
            conn.execute(SCHEMA)

    async def _run(self, callback: Callable):
        """在同一工作线程内创建、使用并关闭单次操作的 PostgreSQL 连接。"""

        def execute():
            # 连接不能跨线程共享；回调与关闭都留在本次线程工作内。
            conn = self._connect()
            try:
                return callback(conn)
            finally:
                conn.close()

        from ...runtime import blocking

        try:
            return await blocking(execute)
        except psycopg.OperationalError as exc:
            raise KGError(
                "UPSTREAM_UNAVAILABLE",
                "Metadata storage is unavailable, busy, or not initialized",
                503,
                retryable=True,
            ) from exc

    async def _one(self, sql: str, values=()):
        def read(conn):
            row = conn.execute(sql, values).fetchone()
            return dict(row) if row else None

        return await self._run(read)

    async def _all(self, sql: str, values=()):
        return await self._run(lambda c: [dict(r) for r in c.execute(sql, values).fetchall()])

    async def _transaction(self, callback):
        """在线程内执行短写事务，回调只操作当前连接，不跨网络等待。"""

        def run(conn):
            with conn.transaction():
                return callback(conn)

        return await self._run(run)

    async def ready(self):
        try:
            table = await self._one("SELECT to_regclass('schema_info') AS name")
            if not table or not table["name"]:
                return False
            row = await self._one("SELECT version FROM schema_info")
            return bool(row and row["version"] == 1)
        except (KGError, psycopg.DatabaseError):
            return False

    async def kb(self, kb_id: str):
        row = await self._one("SELECT * FROM knowledge_bases WHERE kb_id=%s", (kb_id,))
        if not row:
            raise KGError("NOT_FOUND", "Knowledge base does not exist", 404)
        row["model"] = json.loads(row["model_json"])
        row["embedding_profile"] = json.loads(row["embedding_profile_json"])
        return row

    async def create_kb(self, name: str, model: dict, profile: dict):
        kb_id = new_id()
        await self._transaction(
            lambda c: c.execute(
                (
                    "INSERT INTO knowledge_bases(kb_id,name,model_json,model_revision,embedding_profile_json,"
                    "created_at) VALUES(%s,%s,%s,%s,%s,%s)"
                ),
                (kb_id, name, canonical(model), new_id(), canonical(profile), utcnow()),
            )
        )
        return await self.kb(kb_id)

    async def document(self, kb_id: str, document_id: str):
        row = await self._one(
            "SELECT * FROM documents WHERE kb_id=%s AND document_id=%s", (kb_id, document_id)
        )
        if not row:
            raise KGError("NOT_FOUND", "Document does not exist", 404)
        row["source"] = json.loads(row["source_json"])
        return row

    async def documents(self, kb_id: str, active: bool | None = None, limit: int = 100, offset: int = 0):
        sql, args = "SELECT * FROM documents WHERE kb_id=%s", [kb_id]
        if active is not None:
            sql += " AND active=%s AND deleted_at IS NULL"
            args.append(active)
        return await self._all(
            sql + " ORDER BY created_at,document_id LIMIT %s OFFSET %s", (*args, limit, offset)
        )

    async def request(self, kb_id, operation, key):
        return await self._one(
            "SELECT document_id,job_id,request_json FROM jobs WHERE kb_id=%s AND operation=%s AND idempotency_key=%s",
            (kb_id, operation, key),
        )

    @staticmethod
    def _insert_job(c, kb_id, operation, document_id, input_data, job_id, key, request_data):
        now = utcnow()
        c.execute(
            (
                "INSERT INTO jobs(job_id,kb_id,operation,document_id,input_json,state,created_at,updated_at,"
                "idempotency_key,request_json)"
                " VALUES(%s,%s,%s,%s,%s,'QUEUED',%s,%s,%s,%s)"
            ),
            (
                job_id,
                kb_id,
                operation,
                document_id,
                canonical(input_data),
                now,
                now,
                key,
                canonical(request_data),
            ),
        )

    async def register_upload(self, doc: dict, input_data: dict, key: str, request_data: dict):
        """在同一事务中登记资料、导入任务与幂等请求，复用已有请求结果。"""
        job_id = new_id()

        def register(c):
            c.execute("SELECT kb_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (doc["kb_id"],))
            existing = c.execute(
                "SELECT document_id,job_id,request_json FROM jobs WHERE kb_id=%s AND operation='IMPORT' AND idempotency_key=%s",
                (doc["kb_id"], key),
            ).fetchone()
            if existing:
                return dict(existing)
            c.execute(
                (
                    "INSERT INTO documents(document_id,kb_id,original_name,media_type,raw_path,byte_length,"
                    "source_json,mapping_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ),
                (
                    doc["document_id"],
                    doc["kb_id"],
                    doc["original_name"],
                    doc["media_type"],
                    doc["raw_path"],
                    doc["byte_length"],
                    canonical(doc["source"]),
                    doc.get("mapping_id"),
                    utcnow(),
                ),
            )
            self._insert_job(
                c, doc["kb_id"], "IMPORT", doc["document_id"], input_data, job_id, key, request_data
            )
            return {
                "document_id": doc["document_id"],
                "job_id": job_id,
                "request_json": canonical(request_data),
            }

        return await self._transaction(register)

    async def enqueue(self, kb_id, operation, document_id, input_data, key, request_data):
        """校验请求幂等性和资料归属后，在同一事务中登记维护任务。"""
        job_id = new_id()

        def register(c):
            c.execute("SELECT kb_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (kb_id,))
            old = c.execute(
                "SELECT document_id,job_id,request_json FROM jobs WHERE kb_id=%s AND operation=%s AND idempotency_key=%s",
                (kb_id, operation, key),
            ).fetchone()
            if old:
                if old["request_json"] != canonical(request_data):
                    raise KGError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another request", 409)
                return old["job_id"]
            if document_id is not None:
                document = c.execute(
                    "SELECT deleted_at FROM documents WHERE kb_id=%s AND document_id=%s", (kb_id, document_id)
                ).fetchone()
                if not document:
                    raise KGError("NOT_FOUND", "Document does not exist in this knowledge base", 404)
            else:
                document = None
            self._insert_job(c, kb_id, operation, document_id, input_data, job_id, key, request_data)
            if operation == "DELETE" and document and document["deleted_at"]:
                c.execute(
                    "UPDATE jobs SET state='SUCCEEDED',last_step='already_deleted',counts_json=%s WHERE job_id=%s"
                    " AND NOT EXISTS (SELECT 1 FROM jobs WHERE kb_id=%s AND document_id=%s AND operation='DELETE'"
                    " AND state IN ('CLEANING','CLEANUP_FAILED'))",
                    (canonical({"already_deleted": True}), job_id, kb_id, document_id),
                )
            return job_id

        return await self._transaction(register)

    async def job(self, job_id: str):
        row = await self._one("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
        if not row:
            raise KGError("NOT_FOUND", "Job does not exist", 404)
        return self._decode_job(row)

    @staticmethod
    def _decode_job(row):
        for field in ("input", "error", "counts"):
            row[field] = json.loads(row[field + "_json"]) if row[field + "_json"] else None
        row.pop("request_json", None)
        row.pop("idempotency_key", None)
        return row

    async def update_job(self, job_id, **fields):
        allowed = {
            "state",
            "attempt",
            "plan_path",
            "plan_id",
            "last_step",
            "error_json",
            "counts_json",
            "next_attempt_at",
            "cleanup_pending",
        }
        if set(fields) - allowed:
            raise ValueError("Invalid job update")
        fields["updated_at"] = utcnow()
        await self._transaction(
            lambda c: c.execute(
                "UPDATE jobs SET " + ",".join(k + "=%s" for k in fields) + " WHERE job_id=%s",
                (*fields.values(), job_id),
            )
        )

    async def maintenance(self, kb_id, state, job_id):
        """检查维护任务所有权，防止释放存在不确定写入的知识库。"""
        if state not in {"READY", "DRAINING", "APPLYING", "REPAIR_REQUIRED"}:
            raise ValueError("Invalid knowledge base maintenance state")

        def change(c):
            old = c.execute(
                "SELECT maintenance_state,maintenance_job_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE",
                (kb_id,),
            ).fetchone()
            if not old:
                raise KGError("NOT_FOUND", "Knowledge base does not exist", 404)
            if state == "READY":
                if job_id is not None or old["maintenance_state"] not in {"READY", "DRAINING"}:
                    raise KGError(
                        "INVALID_JOB_STATE", "Cannot release a knowledge base with uncertain writes", 409
                    )
            elif not job_id or (old["maintenance_state"] != "READY" and old["maintenance_job_id"] != job_id):
                raise KGError("INVALID_JOB_STATE", "Another job owns knowledge base maintenance", 409)
            c.execute(
                "UPDATE knowledge_bases SET maintenance_state=%s,maintenance_job_id=%s WHERE kb_id=%s",
                (state, job_id, kb_id),
            )

        await self._transaction(change)

    @staticmethod
    def _skip_deleted(c, job):
        if job["operation"] not in {"IMPORT", "DELETE"} or job["state"] == "CLEANING":
            return False
        doc = c.execute(
            "SELECT deleted_at FROM documents WHERE kb_id=%s AND document_id=%s",
            (job["kb_id"], job["document_id"]),
        ).fetchone()
        if doc and doc["deleted_at"] and job["operation"] == "DELETE":
            c.execute(
                "UPDATE jobs SET state='SUCCEEDED',last_step='already_deleted',counts_json=%s,"
                "updated_at=%s WHERE job_id=%s",
                (canonical({"already_deleted": True}), utcnow(), job["job_id"]),
            )
            return True
        if job["operation"] == "IMPORT" and (not doc or doc["deleted_at"]):
            c.execute(
                "UPDATE jobs SET state='INVALIDATED',updated_at=%s WHERE job_id=%s",
                (utcnow(), job["job_id"]),
            )
            return True
        return False

    async def claim(self, now: float, *, publication_only=False):
        """领取可执行的队头，正式写入修复优先。"""
        eligible = (
            "j.state IN ('PREPARED','DRAINING','APPLYING','VERIFYING','NEEDS_REPAIR','CLEANING')"
            if publication_only
            else "j.state IN ('QUEUED','PREPARING','PREPARED','DRAINING','APPLYING','VERIFYING','NEEDS_REPAIR','CLEANING')"
        )
        budget = f"(j.attempt<3 OR ({_FIRST_PUBLICATION}))" if publication_only else "j.attempt<3"

        def claim_one(c):
            # 单发布 Worker 领取下一任务时，已没有正在执行的清理。
            c.execute(
                "UPDATE jobs SET state='CLEANUP_FAILED',error_json=%s,updated_at=%s"
                " WHERE state='CLEANING' AND attempt>=3",
                (
                    canonical(
                        KGError(
                            "CLEANUP_INTERRUPTED",
                            "File cleanup did not finish within its retry budget",
                            503,
                            retryable=True,
                        ).as_dict()
                    ),
                    utcnow(),
                ),
            )
            while True:
                row = c.execute(
                    f"SELECT j.* FROM jobs j JOIN knowledge_bases k USING(kb_id) WHERE {eligible}"
                    f" AND j.next_attempt_at<=%s AND {budget}"
                    " AND (k.maintenance_state='READY' OR k.maintenance_job_id=j.job_id)"
                    + _QUEUE_HEAD
                    + " ORDER BY CASE WHEN j.state IN ('APPLYING','VERIFYING','NEEDS_REPAIR') THEN 0 ELSE 1 END,"
                    " j.queue_seq LIMIT 1 FOR UPDATE OF j SKIP LOCKED",
                    (now,),
                ).fetchone()
                if row is None:
                    return None
                if self._skip_deleted(c, row):
                    continue
                if (
                    publication_only
                    and row["state"] == "PREPARED"
                    and row["last_step"] in {"prepared", "publish_draining"}
                ):
                    return row
                state = "PREPARING" if row["state"] == "QUEUED" else row["state"]
                return c.execute(
                    "UPDATE jobs SET state=%s,attempt=attempt+1,updated_at=%s WHERE job_id=%s RETURNING *",
                    (state, utcnow(), row["job_id"]),
                ).fetchone()

        row = await self._transaction(claim_one)
        return self._decode_job(row) if row else None

    async def preparation_candidates(self, after, now, limit=50):
        """分页发现可投递队头，耗尽尝试的中断任务也由接管者裁决。"""
        return await self._all(
            "SELECT j.job_id,j.queue_seq FROM jobs j JOIN knowledge_bases k USING(kb_id)"
            " WHERE j.queue_seq>%s AND j.state IN ('QUEUED','PREPARING')"
            " AND j.next_attempt_at<=%s AND k.maintenance_state='READY'"
            + _QUEUE_HEAD
            + " ORDER BY j.queue_seq LIMIT %s",
            (after, now, limit),
        )

    async def claim_preparation(self, observed, now):
        """仅为 Redis 已领取的队头登记下一次准备尝试。"""

        def claim_one(c):
            c.execute("SELECT kb_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (observed["kb_id"],))
            row = c.execute(
                "SELECT j.* FROM jobs j JOIN knowledge_bases k USING(kb_id)"
                " WHERE j.job_id=%s AND j.queue_seq=%s AND j.attempt=%s AND j.state=%s"
                " AND j.state IN ('QUEUED','PREPARING') AND j.next_attempt_at<=%s AND k.maintenance_state='READY'"
                + _QUEUE_HEAD
                + " FOR UPDATE OF j",
                (observed["job_id"], observed["queue_seq"], observed["attempt"], observed["state"], now),
            ).fetchone()
            if row is None:
                return None
            if self._skip_deleted(c, row):
                return c.execute("SELECT * FROM jobs WHERE job_id=%s", (row["job_id"],)).fetchone()
            if row["attempt"] >= 3:
                error = KGError(
                    "PREPARATION_INTERRUPTED",
                    "Preparation was interrupted after the automatic recovery limit",
                    503,
                    retryable=True,
                )
                return c.execute(
                    "UPDATE jobs SET state='PREPARE_FAILED',error_json=%s,updated_at=%s"
                    " WHERE job_id=%s RETURNING *",
                    (canonical(error.as_dict()), utcnow(), row["job_id"]),
                ).fetchone()
            return c.execute(
                "UPDATE jobs SET state='PREPARING',attempt=attempt+1,error_json=NULL,updated_at=%s"
                " WHERE job_id=%s RETURNING *",
                (utcnow(), row["job_id"]),
            ).fetchone()

        row = await self._transaction(claim_one)
        return self._decode_job(row) if row else None

    async def owns_preparation(self, job):
        return (
            await self._one(
                "SELECT 1 FROM jobs WHERE job_id=%s AND queue_seq=%s AND attempt=%s AND state='PREPARING'",
                (job["job_id"], job["queue_seq"], job["attempt"]),
            )
            is not None
        )

    async def update_preparation(self, job, **fields):
        if set(fields) - {"state", "plan_path", "plan_id", "last_step", "error_json", "next_attempt_at"}:
            raise ValueError("Invalid preparation update")
        fields["updated_at"] = utcnow()
        updated = await self._one(
            "UPDATE jobs SET "
            + ",".join(key + "=%s" for key in fields)
            + " WHERE job_id=%s AND queue_seq=%s AND attempt=%s AND state='PREPARING' RETURNING job_id",
            (*fields.values(), job["job_id"], job["queue_seq"], job["attempt"]),
        )
        return updated is not None

    async def start_publication(self, job):
        """一次事务登记排空与领取，保留初次排空可恢复的边界。"""

        def start(c):
            kb = c.execute(
                "SELECT maintenance_state,maintenance_job_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE",
                (job["kb_id"],),
            ).fetchone()
            if kb["maintenance_state"] != "READY" and kb["maintenance_job_id"] != job["job_id"]:
                raise KGError("INVALID_JOB_STATE", "Another task owns this knowledge base's maintenance", 409)
            mark = "publish_draining" if job["last_step"] in {"prepared", "publish_draining"} else "draining"
            row = c.execute(
                "UPDATE jobs SET state='DRAINING',last_step=%s,attempt=GREATEST(attempt,1),"
                "error_json=NULL,updated_at=%s WHERE job_id=%s AND state=%s AND queue_seq=%s AND retry_generation=%s"
                " RETURNING job_id",
                (mark, utcnow(), job["job_id"], job["state"], job["queue_seq"], job["retry_generation"]),
            ).fetchone()
            if not row:
                raise KGError("PUBLICATION_REPLACED", "The task changed before publication", 409)
            c.execute(
                "UPDATE knowledge_bases SET maintenance_state='DRAINING',maintenance_job_id=%s WHERE kb_id=%s",
                (job["job_id"], job["kb_id"]),
            )

        await self._transaction(start)

    async def cancel_publication(self, job):
        """在写入开始前同时恢复知识库可读和任务可重试。"""

        def cancel(c):
            gate = c.execute(
                "SELECT 1 FROM knowledge_bases WHERE kb_id=%s AND maintenance_job_id=%s"
                " AND maintenance_state='DRAINING' FOR UPDATE",
                (job["kb_id"], job["job_id"]),
            ).fetchone()
            if gate is None:
                return
            updated = c.execute(
                "UPDATE jobs SET state='PREPARED',last_step='publish_failed',updated_at=%s"
                " WHERE job_id=%s AND state='DRAINING'",
                (utcnow(), job["job_id"]),
            )
            if updated.rowcount == 1:
                c.execute(
                    "UPDATE knowledge_bases SET maintenance_state='READY',maintenance_job_id=NULL WHERE kb_id=%s",
                    (job["kb_id"],),
                )

        await self._transaction(cancel)

    async def begin_publication_writes(self, job):
        """排空后同时更新门禁、任务和手动重试的计数。"""

        def begin(c):
            kb = c.execute(
                "SELECT maintenance_state,maintenance_job_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE",
                (job["kb_id"],),
            ).fetchone()
            if kb["maintenance_state"] != "READY" and kb["maintenance_job_id"] != job["job_id"]:
                raise KGError("INVALID_JOB_STATE", "Another task owns this knowledge base's maintenance", 409)
            updated = c.execute(
                "UPDATE jobs SET state='APPLYING',last_step='begin',attempt=GREATEST(attempt,1),updated_at=%s"
                " WHERE job_id=%s AND state IN ('DRAINING','APPLYING','VERIFYING','NEEDS_REPAIR')",
                (utcnow(), job["job_id"]),
            )
            if updated.rowcount != 1:
                raise KGError("PUBLICATION_REPLACED", "The task changed before writing", 409)
            c.execute(
                "UPDATE knowledge_bases SET maintenance_state='APPLYING',maintenance_job_id=%s WHERE kb_id=%s",
                (job["job_id"], job["kb_id"]),
            )

        await self._transaction(begin)

    async def storage_mode_matches(self, distributed):
        row = await self._one(
            "SELECT EXISTS (SELECT 1 FROM documents WHERE raw_path IS NOT NULL"
            " AND (left(raw_path,4)='raw/')<>%s)"
            " OR EXISTS (SELECT 1 FROM jobs WHERE plan_path IS NOT NULL"
            " AND (left(plan_path,5)='jobs/')<>%s) AS mismatch",
            (distributed, distributed),
        )
        return not row["mismatch"]

    async def artifact_jobs(self, job_ids):
        return {
            row["job_id"]: row
            for row in await self._all(
                "SELECT job_id,queue_seq,state,plan_path FROM jobs WHERE job_id=ANY(%s)", (job_ids,)
            )
        }

    async def retry(self, job_id, generation):
        """接受一次针对当前失败代次的重试。"""

        def retry_one(c):
            reference = c.execute("SELECT kb_id FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
            if not reference:
                raise KGError("NOT_FOUND", "Job does not exist", 404)
            c.execute("SELECT kb_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (reference["kb_id"],))
            row = c.execute("SELECT * FROM jobs WHERE job_id=%s FOR UPDATE", (job_id,)).fetchone()
            if not row:
                raise KGError("NOT_FOUND", "Job does not exist", 404)
            if row["retry_generation"] != generation:
                raise KGError("STALE_RETRY", "Reload the job before retrying its current generation", 409)
            if row["operation"] == "IMPORT":
                doc = c.execute(
                    "SELECT deleted_at FROM documents WHERE document_id=%s", (row["document_id"],)
                ).fetchone()
                if doc["deleted_at"]:
                    raise KGError("INVALID_JOB_STATE", "Deleted document imports cannot be retried", 409)
            if row["state"] not in {
                "PREPARE_FAILED",
                "PREPARED",
                "NEEDS_REPAIR",
                "DRAINING",
                "CLEANUP_FAILED",
            }:
                raise KGError("INVALID_JOB_STATE", "This job cannot be retried", 409)
            requeue = row["state"] == "PREPARE_FAILED"
            changes = ""
            if requeue:
                changes = "queue_seq=DEFAULT,state='QUEUED',plan_path=NULL,plan_id=NULL,error_json=NULL,"
            elif row["state"] == "CLEANUP_FAILED":
                changes = "state='CLEANING',error_json=NULL,"
            c.execute(
                "UPDATE jobs SET "
                + changes
                + "attempt=0,next_attempt_at=0,retry_generation=retry_generation+1,updated_at=%s WHERE job_id=%s",
                (utcnow(), job_id),
            )
            return requeue

        return await self._transaction(retry_one)

    async def contributions(self, kb_id, document_ids: list[str] | None = None, *, contribution_ids=None):
        sql, args = "SELECT c.* FROM source_contributions c WHERE c.kb_id=%s", [kb_id]
        if contribution_ids is not None:
            sql += " AND c.contribution_id=ANY(%s)"
            args.append(contribution_ids)
        if document_ids is not None:
            sql += " AND c.document_id=ANY(%s)"
            args.append(document_ids)
        return self._decode_contributions(await self._all(sql, args))

    @staticmethod
    def _decode_contributions(rows):
        for row in rows:
            for key in ("value", "evidence", "claim"):
                row[key] = json.loads(row.pop(key + "_json"))
        return rows

    async def target_contributions(self, kb_id, targets, *, active_only=False):
        if not targets:
            return []
        # 逐个目标使用现有复合索引，避免把同库其他贡献传回 Worker。
        active = (
            " AND EXISTS (SELECT 1 FROM documents d WHERE d.document_id=c.document_id"
            " AND d.active=true AND d.deleted_at IS NULL)"
            if active_only
            else ""
        )
        rows = await self._all(
            "SELECT c.* FROM unnest(%s::text[],%s::text[]) wanted(kind,id) JOIN source_contributions c "
            "ON c.kb_id=%s AND c.target_kind=wanted.kind AND c.target_id=wanted.id" + active,
            (*map(list, zip(*sorted(set(targets)), strict=True)), kb_id),
        )
        return self._decode_contributions(rows)

    async def dependent_contributions(self, kb_id, entity_ids, max_rows):
        if not entity_ids:
            return []
        rows = await self._all(
            "SELECT c.* FROM source_contributions c"
            " WHERE c.kb_id=%s AND EXISTS ("
            " SELECT 1 FROM jsonb_array_elements(c.claim_json::jsonb->'identity_dependencies') dep"
            " WHERE dep->>'target_id'=ANY(%s))"
            " AND EXISTS (SELECT 1 FROM documents d WHERE d.document_id=c.document_id"
            " AND d.active=true AND d.deleted_at IS NULL)"
            " ORDER BY c.contribution_id LIMIT %s",
            (kb_id, entity_ids, max_rows + 1),
        )
        if len(rows) > max_rows:
            raise KGError("SCAN_BUDGET_EXCEEDED", "Identity dependencies exceed the maintenance budget")
        return self._decode_contributions(rows)

    async def contribution_count(self, kb_id):
        row = await self._one("SELECT count(*) AS n FROM source_contributions WHERE kb_id=%s", (kb_id,))
        return row["n"]

    async def replace_contributions(
        self, kb_id, contributions, issues, issue_documents, *, document_ids=None
    ):
        """在短事务内替换全库或指定来源的贡献，并更新候选问题记录。"""
        scope = set(document_ids) if document_ids is not None else None
        if any(
            row["kb_id"] != kb_id or (scope is not None and row["document_id"] not in scope)
            for row in contributions
        ):
            raise KGError("INVALID_ARGUMENT", "Contributions exceed the requested replacement scope")

        def replace(c):
            if document_ids is None:
                c.execute("DELETE FROM source_contributions WHERE kb_id=%s", (kb_id,))
            elif document_ids:
                c.execute(
                    "DELETE FROM source_contributions WHERE kb_id=%s AND document_id=ANY(%s)",
                    (kb_id, document_ids),
                )
            for row in contributions:
                c.execute(
                    "INSERT INTO source_contributions VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        row["contribution_id"],
                        kb_id,
                        row["document_id"],
                        row["chunk_id"],
                        row["target_kind"],
                        row["target_id"],
                        row["field_path"],
                        canonical(row["claim"]),
                        canonical(row["value"]),
                        canonical(row["evidence"]),
                        row["job_id"],
                    ),
                )
            if issue_documents:
                c.execute(
                    "DELETE FROM candidate_issues WHERE kb_id=%s AND document_id=ANY(%s)",
                    (kb_id, issue_documents),
                )
            for issue in issues:
                c.execute(
                    "INSERT INTO candidate_issues VALUES(%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (issue_id) DO NOTHING",
                    (
                        issue["issue_id"],
                        kb_id,
                        issue["job_id"],
                        issue.get("document_id"),
                        issue.get("chunk_id"),
                        canonical(issue.get("candidate", {})),
                        issue["reason_code"],
                        canonical(issue.get("details", {})),
                    ),
                )

        await self._transaction(replace)

    async def vector_members(
        self, kb_id, *, chunk_ids=None, document_ids=None, vector_ids=None, domain_ids=None, active_only=False
    ):
        sql = "SELECT m.kb_id,m.chunk_id,m.document_id,m.vector_id FROM chunk_vector_members m"
        if active_only or domain_ids:
            sql += " JOIN documents d ON d.document_id=m.document_id AND d.kb_id=m.kb_id"
        sql += " WHERE m.kb_id=%s"
        args = [kb_id]
        for field, values in (
            ("chunk_id", chunk_ids),
            ("document_id", document_ids),
            ("vector_id", vector_ids),
        ):
            if values is not None:
                sql += f" AND m.{field}=ANY(%s)"
                args.append(values)
        if active_only:
            sql += " AND d.active AND d.deleted_at IS NULL"
        if domain_ids:
            sql += " AND d.source_json::jsonb->'domain_ids' ?| %s::text[]"
            args.append(domain_ids)
        return await self._all(sql + " ORDER BY m.chunk_id", args)

    async def replace_vector_members(self, kb_id, vector_ids, members):
        """用计划记录的分组完整替换其成员，重复执行得到同一映射。"""

        def replace(conn):
            if vector_ids is None:
                conn.execute("DELETE FROM chunk_vector_members WHERE kb_id=%s", (kb_id,))
            elif vector_ids:
                conn.execute(
                    "DELETE FROM chunk_vector_members WHERE kb_id=%s AND vector_id=ANY(%s)",
                    (kb_id, vector_ids),
                )
            if members:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO chunk_vector_members(kb_id,chunk_id,document_id,vector_id) VALUES(%s,%s,%s,%s)",
                        [(kb_id, row["chunk_id"], row["document_id"], row["vector_id"]) for row in members],
                    )

        await self._transaction(replace)

    async def finish(self, job_id, plan):
        """原子提交可见状态，并登记尚未执行的删除清理。"""
        cleanup = plan["operation"] == "DELETE" and bool(plan["cleanup_paths"])

        def finalize(c):
            c.execute("SELECT kb_id FROM knowledge_bases WHERE kb_id=%s FOR UPDATE", (plan["kb_id"],))
            row = c.execute("SELECT state FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
            if row["state"] in SUCCESS_STATES:
                return False
            if row["state"] == "CLEANING":
                return True

            # 资料状态和有效配置的切换与任务发布、门禁恢复一起提交。
            for old_job_id in plan.get("cleanup_job_ids", []):
                c.execute("UPDATE jobs SET plan_path=NULL WHERE job_id=%s", (old_job_id,))
            for document_id in plan["activate_documents"]:
                c.execute(
                    "UPDATE documents SET active=true WHERE kb_id=%s AND document_id=%s AND deleted_at IS NULL",
                    (plan["kb_id"], document_id),
                )
            for document_id in plan["delete_documents"]:
                c.execute(
                    (
                        "UPDATE documents SET active=false,deleted_at=%s,raw_path=NULL,source_json='{}' WHERE kb_id=%s"
                        " AND document_id=%s"
                    ),
                    (utcnow(), plan["kb_id"], document_id),
                )
                c.execute(
                    (
                        "UPDATE jobs SET state='INVALIDATED',plan_path=NULL,updated_at=%s WHERE document_id=%s"
                        " AND operation='IMPORT' AND state IN ('QUEUED','PREPARING','PREPARED','PREPARE_FAILED')"
                    ),
                    (utcnow(), document_id),
                )
            updated = c.execute(
                (
                    "UPDATE knowledge_bases SET model_json=%s,model_revision=%s,embedding_profile_json=%s,"
                    "maintenance_state='READY',maintenance_job_id=NULL WHERE kb_id=%s AND maintenance_job_id=%s"
                ),
                (
                    canonical(plan["model"]),
                    plan["model_revision"],
                    canonical(plan["embedding_profile"]),
                    plan["kb_id"],
                    job_id,
                ),
            )
            # 紧跟所有权限定的更新核对结果，不能插入其他写操作。
            if updated.rowcount != 1:
                raise KGError("INVALID_JOB_STATE", "Maintenance ownership changed", 409)
            state = "SUCCEEDED_WITH_WARNINGS" if plan["warnings"] else "SUCCEEDED"
            step = "committed"
            cleanup_attempt = ""
            if cleanup:
                state, step = "CLEANING", "published"
                cleanup_attempt = ",attempt=1,next_attempt_at=0"
            c.execute(
                "UPDATE jobs SET state=%s,last_step=%s,error_json=NULL,counts_json=%s,updated_at=%s"
                + cleanup_attempt
                + " WHERE job_id=%s",
                (state, step, canonical(plan["counts"] | {"warnings": plan["warnings"]}), utcnow(), job_id),
            )
            return cleanup

        return await self._transaction(finalize)

    async def list_kbs(self):
        rows = await self._all("SELECT kb_id FROM knowledge_bases ORDER BY created_at,kb_id")
        return [await self.kb(row["kb_id"]) for row in rows]

    async def document_exists(self, document_id):
        return await self._one("SELECT 1 FROM documents WHERE document_id=%s", (document_id,)) is not None

    async def list_documents(self, kb_id, active=None, limit=100, offset=0):
        return await self.documents(kb_id, active, limit, offset)

    async def active_documents(self, kb_id):
        rows = await self._all(
            "SELECT * FROM documents WHERE kb_id=%s AND active=true AND deleted_at IS NULL ORDER BY document_id",
            (kb_id,),
        )
        for row in rows:
            row["source"] = json.loads(row["source_json"])
        return rows

    async def latest_extraction_job_id(self, kb_id):
        row = await self._one(
            "SELECT job_id FROM jobs WHERE kb_id=%s AND operation IN ('IMPORT','REBUILD')"
            " AND state IN ('SUCCEEDED','SUCCEEDED_WITH_WARNINGS') AND plan_path IS NOT NULL"
            " AND (counts_json::jsonb->>'extraction_batches')::int > 0"
            " ORDER BY queue_seq DESC LIMIT 1",
            (kb_id,),
        )
        return row["job_id"] if row else None

    async def jobs_for_kb(self, kb_id):
        return await self._all(
            "SELECT job_id,document_id,state FROM jobs WHERE kb_id=%s ORDER BY queue_seq", (kb_id,)
        )

    async def document_jobs(self, kb_id, document_id, limit=100, offset=0):
        rows = await self._all(
            "SELECT * FROM jobs WHERE kb_id=%s AND document_id=%s ORDER BY queue_seq LIMIT %s OFFSET %s",
            (kb_id, document_id, limit, offset),
        )
        return [self._decode_job(row) for row in rows]

    async def candidate_issues(self, job_id, limit=100, offset=0):
        rows = await self._all(
            "SELECT * FROM candidate_issues WHERE job_id=%s ORDER BY issue_id LIMIT %s OFFSET %s",
            (job_id, limit, offset),
        )
        for row in rows:
            for key in ("candidate", "details"):
                row[key] = json.loads(row.pop(key + "_json"))
        return rows

    async def expired_checkpoints(self, threshold):
        return await self._all(
            (
                "SELECT job_id FROM jobs WHERE state IN ('SUCCEEDED','SUCCEEDED_WITH_WARNINGS','INVALIDATED')"
                " AND updated_at<%s AND plan_path IS NOT NULL"
            ),
            (threshold,),
        )

    async def referenced_raw_paths(self):
        return {
            row["raw_path"]
            for row in await self._all("SELECT raw_path FROM documents WHERE raw_path IS NOT NULL")
        }

    async def reconcile_interrupted(self, *, publication_only=False):
        """仅在持有 Worker 进程锁后调用，恢复上次进程留下的中断状态。"""

        def reconcile(c):
            states = ["DRAINING", "APPLYING", "VERIFYING"]
            if not publication_only:
                states.append("PREPARING")
            locked = c.execute(
                "SELECT k.kb_id FROM knowledge_bases k WHERE EXISTS ("
                "SELECT 1 FROM jobs j WHERE j.kb_id=k.kb_id AND j.state=ANY(%s))"
                " ORDER BY k.kb_id FOR UPDATE OF k",
                (states,),
            ).fetchall()
            rows = c.execute(
                "SELECT j.*,k.maintenance_state,k.maintenance_job_id FROM jobs j"
                " JOIN knowledge_bases k USING(kb_id) WHERE j.kb_id=ANY(%s) AND j.state=ANY(%s)",
                ([row["kb_id"] for row in locked], states),
            ).fetchall()
            for row in rows:
                if row["state"] in {"APPLYING", "VERIFYING"}:
                    c.execute(
                        "UPDATE jobs SET state='NEEDS_REPAIR',updated_at=%s WHERE job_id=%s",
                        (utcnow(), row["job_id"]),
                    )
                    c.execute(
                        "UPDATE knowledge_bases SET maintenance_state='REPAIR_REQUIRED',maintenance_job_id=%s WHERE kb_id=%s",
                        (row["job_id"], row["kb_id"]),
                    )
                elif row["state"] == "PREPARING" and row["attempt"] >= 3:
                    error = canonical(
                        KGError(
                            "PREPARATION_INTERRUPTED",
                            "Preparation was interrupted after the automatic recovery limit",
                            503,
                            retryable=True,
                        ).as_dict()
                    )
                    c.execute(
                        "UPDATE jobs SET state='PREPARE_FAILED',error_json=%s,updated_at=%s WHERE job_id=%s",
                        (error, utcnow(), row["job_id"]),
                    )
                elif row["state"] == "DRAINING":
                    if row["maintenance_job_id"] == row["job_id"] and row["maintenance_state"] == "DRAINING":
                        c.execute(
                            "UPDATE knowledge_bases SET maintenance_state='READY',maintenance_job_id=NULL WHERE kb_id=%s",
                            (row["kb_id"],),
                        )
                        c.execute(
                            "UPDATE jobs SET state='PREPARED',updated_at=%s WHERE job_id=%s",
                            (utcnow(), row["job_id"]),
                        )
                    elif row["maintenance_state"] != "READY":
                        c.execute(
                            "UPDATE jobs SET state='NEEDS_REPAIR',updated_at=%s WHERE job_id=%s",
                            (utcnow(), row["job_id"]),
                        )
                        c.execute(
                            "UPDATE knowledge_bases SET maintenance_state='REPAIR_REQUIRED',maintenance_job_id=%s WHERE kb_id=%s",
                            (row["job_id"], row["kb_id"]),
                        )

        await self._transaction(reconcile)
