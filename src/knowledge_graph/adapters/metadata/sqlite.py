"""用短 SQLite 事务保存配置、资料、任务和来源贡献，协调维护恢复状态。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from ...domain import KGError, canonical, new_id, utcnow
from ...domain.jobs import SUCCESS_STATES

SCHEMA = (Path(__file__).parent / "migrations" / "001.sql").read_text("utf-8")


class Metadata:
    def __init__(self, data_root: str):
        self.path = Path(data_root) / "metadata.sqlite3"

    def _connect(self, *, create=False):
        if not create and not self.path.exists():
            raise KGError(
                "STORAGE_NOT_INITIALIZED", "Run kg init-storage before starting the application", 503
            )
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect(create=True) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.executescript(SCHEMA)

    async def _run(self, callback: Callable):
        """在同一工作线程内创建、使用并关闭单次操作的 SQLite 连接。"""

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
        except sqlite3.OperationalError as exc:
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
            # 事务回调只做已准备好的本地数据库操作。
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = callback(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

        return await self._run(run)

    async def ready(self):
        if not self.path.exists():
            return False
        try:
            row = await self._one("SELECT version FROM schema_info")
            columns = await self._all("PRAGMA table_info(jobs)")
            return bool(row and row["version"] == 1 and "plan_id" in {column["name"] for column in columns})
        except sqlite3.DatabaseError:
            return False

    async def kb(self, kb_id: str):
        row = await self._one("SELECT * FROM knowledge_bases WHERE kb_id=?", (kb_id,))
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
                    "created_at) VALUES(?,?,?,?,?,?)"
                ),
                (kb_id, name, canonical(model), new_id(), canonical(profile), utcnow()),
            )
        )
        return await self.kb(kb_id)

    async def document(self, kb_id: str, document_id: str):
        row = await self._one("SELECT * FROM documents WHERE kb_id=? AND document_id=?", (kb_id, document_id))
        if not row:
            raise KGError("NOT_FOUND", "Document does not exist", 404)
        row["source"] = json.loads(row["source_json"])
        return row

    async def documents(self, kb_id: str, active: bool | None = None, limit: int = 100, offset: int = 0):
        sql, args = "SELECT * FROM documents WHERE kb_id=?", [kb_id]
        if active is not None:
            sql += " AND active=? AND deleted_at IS NULL"
            args.append(int(active))
        return await self._all(sql + " ORDER BY created_at LIMIT ? OFFSET ?", (*args, limit, offset))

    async def request(self, kb_id, operation, key):
        return await self._one(
            "SELECT * FROM requests WHERE kb_id=? AND operation=? AND idempotency_key=?",
            (kb_id, operation, key),
        )

    @staticmethod
    def _insert_job(c, kb_id, operation, document_id, input_data, job_id):
        now = utcnow()
        c.execute(
            (
                "INSERT INTO jobs(job_id,kb_id,operation,document_id,input_json,state,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'QUEUED',?,?)"
            ),
            (job_id, kb_id, operation, document_id, canonical(input_data), now, now),
        )

    async def register_upload(self, doc: dict, input_data: dict, key: str, request_data: dict):
        """在同一事务中登记资料、导入任务与幂等请求，复用已有请求结果。"""
        job_id = new_id()

        def register(c):
            existing = c.execute(
                "SELECT * FROM requests WHERE kb_id=? AND operation='IMPORT' AND idempotency_key=?",
                (doc["kb_id"], key),
            ).fetchone()
            if existing:
                return dict(existing)
            c.execute(
                (
                    "INSERT INTO documents(document_id,kb_id,original_name,media_type,raw_path,byte_length,"
                    "source_json,mapping_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)"
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
            self._insert_job(c, doc["kb_id"], "IMPORT", doc["document_id"], input_data, job_id)
            c.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                (doc["kb_id"], "IMPORT", key, canonical(request_data), doc["document_id"], job_id),
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
            old = c.execute(
                "SELECT * FROM requests WHERE kb_id=? AND operation=? AND idempotency_key=?",
                (kb_id, operation, key),
            ).fetchone()
            if old:
                if old["request_json"] != canonical(request_data):
                    raise KGError("IDEMPOTENCY_CONFLICT", "Idempotency key belongs to another request", 409)
                return old["job_id"]
            if document_id is not None:
                document = c.execute(
                    "SELECT deleted_at FROM documents WHERE kb_id=? AND document_id=?", (kb_id, document_id)
                ).fetchone()
                if not document:
                    raise KGError("NOT_FOUND", "Document does not exist in this knowledge base", 404)
            else:
                document = None
            self._insert_job(c, kb_id, operation, document_id, input_data, job_id)
            if operation == "DELETE" and document and document["deleted_at"]:
                c.execute(
                    "UPDATE jobs SET state='SUCCEEDED',last_step='already_deleted',counts_json=? WHERE job_id=?",
                    (canonical({"already_deleted": True}), job_id),
                )
            c.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                (kb_id, operation, key, canonical(request_data), document_id, job_id),
            )
            return job_id

        return await self._transaction(register)

    async def job(self, job_id: str):
        row = await self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not row:
            raise KGError("NOT_FOUND", "Job does not exist", 404)
        for field in ("input", "error", "counts"):
            row[field] = json.loads(row[field + "_json"]) if row[field + "_json"] else None
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
                "UPDATE jobs SET " + ",".join(k + "=?" for k in fields) + " WHERE job_id=?",
                (*fields.values(), job_id),
            )
        )

    async def maintenance(self, kb_id, state, job_id):
        """检查维护任务所有权，防止释放存在不确定写入的知识库。"""
        if state not in {"READY", "DRAINING", "APPLYING", "REPAIR_REQUIRED"}:
            raise ValueError("Invalid knowledge base maintenance state")

        def change(c):
            old = c.execute(
                "SELECT maintenance_state,maintenance_job_id FROM knowledge_bases WHERE kb_id=?", (kb_id,)
            ).fetchone()
            if not old:
                raise KGError("NOT_FOUND", "Knowledge base does not exist", 404)
            if state == "READY":
                if job_id is not None or old[0] not in {"READY", "DRAINING"}:
                    raise KGError(
                        "INVALID_JOB_STATE", "Cannot release a knowledge base with uncertain writes", 409
                    )
            elif not job_id or (old[0] != "READY" and old[1] != job_id):
                raise KGError("INVALID_JOB_STATE", "Another job owns knowledge base maintenance", 409)
            c.execute(
                "UPDATE knowledge_bases SET maintenance_state=?,maintenance_job_id=? WHERE kb_id=?",
                (state, job_id, kb_id),
            )

        await self._transaction(change)

    async def claim(self, now: float):
        """优先领取待修复任务，并保持同一知识库内的维护队列顺序。"""

        def claim_one(c):
            rows = c.execute(
                (
                    "SELECT j.*,k.maintenance_state,k.maintenance_job_id FROM jobs j"
                    " JOIN knowledge_bases k USING(kb_id)"
                    " WHERE j.state IN ('QUEUED','PREPARING','PREPARED','DRAINING','APPLYING','VERIFYING',"
                    "'NEEDS_REPAIR') ORDER BY CASE WHEN j.state IN ('APPLYING','VERIFYING','NEEDS_REPAIR') THEN 0 "
                    "ELSE 1 END,j.queue_seq"
                )
            ).fetchall()

            for row in rows:
                row = dict(row)
                if row["next_attempt_at"] > now or row["attempt"] >= 3:
                    continue
                if row["maintenance_state"] != "READY" and row["maintenance_job_id"] != row["job_id"]:
                    continue

                # 修复优先不越过本库更早的未完成任务。
                earlier = c.execute(
                    (
                        "SELECT 1 FROM jobs WHERE kb_id=? AND queue_seq<?"
                        " AND state IN ('QUEUED','PREPARING','PREPARED','DRAINING','APPLYING','VERIFYING','NEEDS_REPAIR')"
                        " LIMIT 1"
                    ),
                    (row["kb_id"], row["queue_seq"]),
                ).fetchone()
                if earlier:
                    continue
                if row["operation"] == "DELETE":
                    document = c.execute(
                        "SELECT deleted_at FROM documents WHERE kb_id=? AND document_id=?",
                        (row["kb_id"], row["document_id"]),
                    ).fetchone()
                    if document and document["deleted_at"]:
                        c.execute(
                            (
                                "UPDATE jobs SET state='SUCCEEDED',last_step='already_deleted',counts_json=?,updated_at=?"
                                " WHERE job_id=?"
                            ),
                            (canonical({"already_deleted": True}), utcnow(), row["job_id"]),
                        )
                        continue
                if row["operation"] == "IMPORT":
                    doc = c.execute(
                        "SELECT deleted_at FROM documents WHERE document_id=?", (row["document_id"],)
                    ).fetchone()
                    if not doc or doc[0]:
                        c.execute(
                            "UPDATE jobs SET state='INVALIDATED',updated_at=? WHERE job_id=?",
                            (utcnow(), row["job_id"]),
                        )
                        continue

                state = "PREPARING" if row["state"] == "QUEUED" else row["state"]
                c.execute(
                    "UPDATE jobs SET state=?,attempt=attempt+1,updated_at=? WHERE job_id=? AND state=?",
                    (state, utcnow(), row["job_id"], row["state"]),
                )
                return row["job_id"]
            return None

        job_id = await self._transaction(claim_one)
        return await self.job(job_id) if job_id else None

    async def retry(self, job_id, key):
        """按状态重置同一任务；准备失败重新排队，写入修复保留既有计划。"""

        def retry_one(c):
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KGError("NOT_FOUND", "Job does not exist", 404)
            if row["operation"] == "IMPORT":
                doc = c.execute(
                    "SELECT deleted_at FROM documents WHERE document_id=?", (row["document_id"],)
                ).fetchone()
                if doc[0]:
                    raise KGError("INVALID_JOB_STATE", "Deleted document imports cannot be retried", 409)
            old = c.execute(
                "SELECT request_json FROM requests WHERE kb_id=? AND operation='RETRY' AND idempotency_key=?",
                (row["kb_id"], key),
            ).fetchone()
            request_data = canonical({"job_id": job_id})
            if old:
                if old[0] != request_data:
                    raise KGError("IDEMPOTENCY_CONFLICT", "Retry key conflict", 409)
                return False
            if row["state"] not in {"PREPARE_FAILED", "PREPARED", "NEEDS_REPAIR", "DRAINING"}:
                raise KGError("INVALID_JOB_STATE", "This job cannot be retried", 409)
            if row["state"] == "PREPARE_FAILED":
                # 准备失败重新排到队尾，下次准备读取新的知识库基线。
                max_seq = c.execute("SELECT COALESCE(MAX(queue_seq),0)+1 FROM jobs").fetchone()[0]
                c.execute(
                    (
                        "UPDATE jobs SET queue_seq=?,state='QUEUED',attempt=0,plan_path=NULL,plan_id=NULL,"
                        "error_json=NULL,next_attempt_at=0,updated_at=? WHERE job_id=?"
                    ),
                    (max_seq, utcnow(), job_id),
                )
            else:
                c.execute(
                    "UPDATE jobs SET attempt=0,next_attempt_at=0,updated_at=? WHERE job_id=?",
                    (utcnow(), job_id),
                )
            c.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                (row["kb_id"], "RETRY", key, request_data, row["document_id"], job_id),
            )
            return row["state"] == "PREPARE_FAILED"

        return await self._transaction(retry_one)

    async def contributions(self, kb_id, document_ids: list[str] | None = None, *, contribution_ids=None):
        sql, args = "SELECT c.* FROM source_contributions c WHERE c.kb_id=?", [kb_id]
        if contribution_ids is not None:
            if not contribution_ids:
                return []
            sql = (
                "SELECT c.* FROM json_each(?) wanted CROSS JOIN source_contributions c "
                "ON c.contribution_id=wanted.value WHERE c.kb_id=?"
            )
            args = [canonical(list(dict.fromkeys(contribution_ids))), kb_id]
        if document_ids is not None:
            if not document_ids:
                return []
            sql += " AND c.document_id IN (SELECT value FROM json_each(?))"
            args.append(canonical(document_ids))
        rows = await self._all(sql, args)
        for row in rows:
            for key in ("value", "evidence", "claim"):
                row[key] = json.loads(row.pop(key + "_json"))
        return rows

    async def replace_contributions(self, kb_id, contributions, issues, issue_documents):
        """在单个事务内替换当前来源贡献及指定资料的候选问题记录。"""

        def replace(c):
            c.execute("DELETE FROM source_contributions WHERE kb_id=?", (kb_id,))
            for row in contributions:
                c.execute(
                    "INSERT INTO source_contributions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                    "DELETE FROM candidate_issues WHERE kb_id=? AND document_id IN ("
                    + ",".join("?" for _ in issue_documents)
                    + ")",
                    (kb_id, *issue_documents),
                )
            for issue in issues:
                c.execute(
                    "INSERT OR REPLACE INTO candidate_issues VALUES(?,?,?,?,?,?,?,?)",
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

    async def finish(self, job_id, plan):
        """原子提交资料状态、有效模型、任务成功状态与查询门禁恢复。"""

        def finalize(c):
            row = c.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row[0] in SUCCESS_STATES:
                return

            # 资料状态和有效配置的切换与任务成功、门禁恢复一起提交。
            for old_job_id in plan.get("cleanup_job_ids", []):
                c.execute("UPDATE jobs SET plan_path=NULL WHERE job_id=?", (old_job_id,))
            for document_id in plan["activate_documents"]:
                c.execute(
                    "UPDATE documents SET active=1 WHERE kb_id=? AND document_id=? AND deleted_at IS NULL",
                    (plan["kb_id"], document_id),
                )
            for document_id in plan["delete_documents"]:
                c.execute(
                    (
                        "UPDATE documents SET active=0,deleted_at=?,raw_path=NULL,source_json='{}' WHERE kb_id=?"
                        " AND document_id=?"
                    ),
                    (utcnow(), plan["kb_id"], document_id),
                )
                c.execute(
                    (
                        "UPDATE jobs SET state='INVALIDATED',plan_path=NULL,updated_at=? WHERE document_id=?"
                        " AND operation='IMPORT' AND state IN ('QUEUED','PREPARING','PREPARED','PREPARE_FAILED')"
                    ),
                    (utcnow(), document_id),
                )
            c.execute(
                (
                    "UPDATE knowledge_bases SET model_json=?,model_revision=?,embedding_profile_json=?,"
                    "maintenance_state='READY',maintenance_job_id=NULL WHERE kb_id=? AND maintenance_job_id=?"
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
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                raise KGError("INVALID_JOB_STATE", "Maintenance ownership changed", 409)
            final = "SUCCEEDED_WITH_WARNINGS" if plan["warnings"] else "SUCCEEDED"
            c.execute(
                (
                    "UPDATE jobs SET state=?,last_step='committed',error_json=NULL,counts_json=?,updated_at=?"
                    " WHERE job_id=?"
                ),
                (final, canonical(plan["counts"] | {"warnings": plan["warnings"]}), utcnow(), job_id),
            )

        await self._transaction(finalize)

    async def list_kbs(self):
        rows = await self._all("SELECT kb_id FROM knowledge_bases ORDER BY created_at,kb_id")
        return [await self.kb(row["kb_id"]) for row in rows]

    async def document_exists(self, document_id):
        return await self._one("SELECT 1 FROM documents WHERE document_id=?", (document_id,)) is not None

    async def list_documents(self, kb_id, active=None, limit=100, offset=0):
        return await self.documents(kb_id, active, limit, offset)

    async def active_documents(self, kb_id):
        rows = await self._all(
            "SELECT * FROM documents WHERE kb_id=? AND active=1 AND deleted_at IS NULL ORDER BY document_id",
            (kb_id,),
        )
        for row in rows:
            row["source"] = json.loads(row["source_json"])
        return rows

    async def jobs_for_kb(self, kb_id):
        rows = await self._all("SELECT job_id FROM jobs WHERE kb_id=? ORDER BY queue_seq", (kb_id,))
        return [await self.job(row["job_id"]) for row in rows]

    async def document_jobs(self, kb_id, document_id):
        rows = await self._all(
            "SELECT job_id FROM jobs WHERE kb_id=? AND document_id=? ORDER BY queue_seq", (kb_id, document_id)
        )
        return [await self.job(row["job_id"]) for row in rows]

    async def candidate_issues(self, job_id, limit=100, offset=0):
        rows = await self._all(
            "SELECT * FROM candidate_issues WHERE job_id=? ORDER BY issue_id LIMIT ? OFFSET ?",
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
                " AND updated_at<? AND plan_path IS NOT NULL"
            ),
            (threshold,),
        )

    async def referenced_raw_paths(self):
        return {
            row["raw_path"]
            for row in await self._all("SELECT raw_path FROM documents WHERE raw_path IS NOT NULL")
        }

    async def reconcile_interrupted(self):
        """仅在持有 Worker 进程锁后调用，恢复上次进程留下的中断状态。"""

        def reconcile(c):
            rows = c.execute(
                (
                    "SELECT j.*,k.maintenance_state,k.maintenance_job_id FROM jobs j"
                    " JOIN knowledge_bases k USING(kb_id)"
                    " WHERE j.state IN ('PREPARING','DRAINING','APPLYING','VERIFYING')"
                )
            ).fetchall()
            for row in rows:
                if row["state"] in {"APPLYING", "VERIFYING"}:
                    c.execute(
                        "UPDATE jobs SET state='NEEDS_REPAIR',updated_at=? WHERE job_id=?",
                        (utcnow(), row["job_id"]),
                    )
                    c.execute(
                        "UPDATE knowledge_bases SET maintenance_state='REPAIR_REQUIRED',maintenance_job_id=? WHERE kb_id=?",
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
                        "UPDATE jobs SET state='PREPARE_FAILED',error_json=?,updated_at=? WHERE job_id=?",
                        (error, utcnow(), row["job_id"]),
                    )
                elif row["state"] == "DRAINING":
                    if row["maintenance_job_id"] == row["job_id"] and row["maintenance_state"] == "DRAINING":
                        c.execute(
                            "UPDATE knowledge_bases SET maintenance_state='READY',maintenance_job_id=NULL WHERE kb_id=?",
                            (row["kb_id"],),
                        )
                        c.execute(
                            "UPDATE jobs SET state='PREPARED',updated_at=? WHERE job_id=?",
                            (utcnow(), row["job_id"]),
                        )
                    elif row["maintenance_state"] != "READY":
                        c.execute(
                            "UPDATE jobs SET state='NEEDS_REPAIR',updated_at=? WHERE job_id=?",
                            (utcnow(), row["job_id"]),
                        )
                        c.execute(
                            "UPDATE knowledge_bases SET maintenance_state='REPAIR_REQUIRED',maintenance_job_id=? WHERE kb_id=?",
                            (row["job_id"], row["kb_id"]),
                        )

        await self._transaction(reconcile)
