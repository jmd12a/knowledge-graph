"""管理数据根目录内的原文件、上传暂存及可恢复写入计划。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from ...domain import KGError, canonical, new_id, valid_id
from ...domain.jobs import prepared_fields, validate_prepared_mutation
from ...runtime import blocking


class _ContentReader:
    def __init__(self, stream):
        self.stream = stream

    async def read(self, size):
        try:
            return await blocking(self.stream.read, size)
        except OSError as exc:
            raise KGError(
                "SOURCE_UNAVAILABLE", "Document content could not be read", 503, retryable=True
            ) from exc

    async def close(self):
        await blocking(self.stream.close)


class Files:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def path(self, *parts):
        target = self.root.joinpath(*parts).resolve()
        if target != self.root and self.root not in target.parents:
            raise KGError("INVALID_ARGUMENT", "Path outside data root")
        return target

    async def open_content(self, path, expected_bytes):
        """核对来源文件长度，并在取消时关闭线程已经打开的文件句柄。"""
        opened = []

        def open_checked():
            target = self.path(path)
            if self.path("raw") not in target.parents:
                raise KGError("INCONSISTENT_STORAGE", "Registered document path is invalid", 503)
            try:
                stream = target.open("rb")
                # 先登记线程创建的句柄，便于外层取消路径统一关闭。
                opened.append(stream)
                if os.fstat(stream.fileno()).st_size != expected_bytes:
                    raise KGError(
                        "INCONSISTENT_STORAGE", "Registered document length does not match its content", 503
                    )
                return stream
            except OSError as exc:
                raise KGError(
                    "SOURCE_UNAVAILABLE", "Registered document content is unavailable", 503, retryable=True
                ) from exc

        try:
            stream = await blocking(open_checked)
        except BaseException:
            if opened:
                await blocking(opened[0].close)
            raise
        return _ContentReader(stream)

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for name in ("raw", "jobs", "locks", "uploads"):
            self.path(name).mkdir(parents=True, exist_ok=True, mode=0o700)

    def raw_path(self, kb_id, document_id, extension):
        valid_id(kb_id)
        valid_id(document_id)
        if extension not in {".txt", ".md", ".markdown", ".csv", ".json", ".pdf", ".docx"}:
            raise KGError("UNSUPPORTED_FORMAT", "Invalid original file extension")
        return self.path("raw", kb_id, document_id, "original" + extension)

    def job_dir(self, job_id):
        return self.path("jobs", valid_id(job_id))

    def attempt_dir(self, job):
        return self.job_dir(job["job_id"])

    def begin_preparation(self, job):
        pass

    def read_preparation(self, job):
        return self.read_json(self.attempt_dir(job) / "preparation.json")

    def extraction_path(self, job):
        return self.job_dir(job["job_id"]) / "extraction-results.json"

    def parse_document(self, parser, document, model):
        return parser.parse(document, model)

    def raw_directory(self, kb_id, document_id):
        return self.path("raw", valid_id(kb_id), valid_id(document_id))

    def _cleanup_target(self, path):
        return Path(path).resolve()

    def atomic(self, path: Path, value: dict):
        """先同步临时文件，再原子替换目标并同步目录，持久保存 JSON。"""
        path = Path(path).resolve()
        if self.root not in path.parents:
            raise KGError("INVALID_ARGUMENT", "Atomic write path is outside the data root")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(path.name + "." + new_id() + ".tmp")
        data = canonical(value).encode("utf-8")
        try:
            with temp.open("xb") as stream:
                os.chmod(temp, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # 内容先落盘，再替换目录项；目录同步完成后才算保存成功。
            os.replace(temp, path)
            self._sync_directories(path.parent)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _object_ids(plan):
        return {
            "entities": [x["entity_id"] for x in plan["entities"]],
            "relations": [x["relation_id"] for x in plan["relations"]],
            "chunks": [x["id"] for x in plan["chunk_points"]],
            "objects": [x["id"] for x in plan["object_points"]],
            "contributions": [x["contribution_id"] for x in plan["contributions"]],
        }

    def save_plan(self, job_id, plan, *, path=None):
        """校验写入计划并保存恢复清单，返回与任务绑定的固定路径。"""
        validate_prepared_mutation(plan)
        if plan["job_id"] != job_id:
            raise KGError("PLAN_INVALID", "Prepared mutation belongs to another job", 503)

        payload_bytes = canonical(plan).encode("utf-8")
        wrapper = {
            "schema_version": 2,
            "plan_id": plan["plan_id"],
            "payload_byte_length": len(payload_bytes),
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "object_ids": self._object_ids(plan),
            "expected_business_fields": sorted(prepared_fields(plan)),
            "payload": plan,
        }
        path = path or self.job_dir(job_id) / "prepared.json"
        self.atomic(path, wrapper)
        return str(path)

    def _read_plan_wrapper(self, job):
        expected = self.job_dir(job["job_id"]) / "prepared.json"
        if not job.get("plan_path") or Path(job["plan_path"]).resolve() != expected:
            raise ValueError("Unexpected prepared plan path")
        return self.read_json(expected)

    def read_plan(self, job):
        """核对任务绑定、清单结构和清理范围后，返回可重放的写入计划。"""
        try:
            wrapper = self._read_plan_wrapper(job)
            if not isinstance(wrapper, dict):
                raise ValueError("Prepared wrapper must be an object")
            version = wrapper.get("schema_version")
            if type(version) is not int or version not in {1, 2}:
                raise ValueError("Unsupported prepared wrapper version")
            expected_fields = {
                "schema_version",
                "plan_id",
                "payload_byte_length",
                "object_ids",
                "expected_business_fields",
                "payload",
            }
            # Version 1 plans remain readable so queued work can resume after an upgrade.
            if version == 2:
                expected_fields.add("payload_sha256")
            if set(wrapper) != expected_fields:
                raise ValueError("Unexpected prepared wrapper structure")
            plan = wrapper["payload"]
            payload_bytes = canonical(plan).encode("utf-8")
            if wrapper["payload_byte_length"] != len(payload_bytes):
                raise ValueError("Payload length mismatch")
            if version == 2 and wrapper["payload_sha256"] != hashlib.sha256(payload_bytes).hexdigest():
                raise ValueError("Payload integrity check failed")
            if wrapper["plan_id"] != plan["plan_id"] or wrapper["plan_id"] != job.get("plan_id"):
                raise ValueError("Plan ID differs from its persisted job binding")
            if wrapper["expected_business_fields"] != sorted(prepared_fields(plan)) or wrapper[
                "object_ids"
            ] != self._object_ids(plan):
                raise ValueError("Manifest fields or object sets do not match the fixed schema")

            validate_prepared_mutation(plan, job)
            allowed_cleanup = {
                self.raw_directory(plan["kb_id"], document_id) for document_id in plan["delete_documents"]
            }
            allowed_cleanup |= {self.job_dir(job_id) for job_id in plan["cleanup_job_ids"]}
            if any(self._cleanup_target(path) not in allowed_cleanup for path in plan["cleanup_paths"]):
                raise ValueError("Cleanup path does not belong to the explicit deletion plan")
            return plan
        except (OSError, ValueError, KeyError, TypeError, KGError) as exc:
            if isinstance(exc, KGError) and exc.code == "OBJECT_STORE_UNAVAILABLE":
                raise
            raise KGError(
                "PLAN_INVALID", "Prepared plan is missing or structurally invalid", 503, retryable=False
            ) from exc

    @staticmethod
    def same_bytes(first: Path, second: Path):
        """分块逐字节比较两个原文件，提前返回首个差异。"""
        with first.open("rb") as a, second.open("rb") as b:
            while True:
                left, right = a.read(1024 * 1024), b.read(1024 * 1024)
                if left != right:
                    return False
                if not left:
                    return True

    def remove(self, path: str):
        target = Path(path).resolve()
        if self.root not in target.parents or target in {
            self.path(name) for name in ("raw", "jobs", "uploads", "locks")
        }:
            raise KGError("INVALID_ARGUMENT", "Cleanup target is outside the explicit data subtree")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    def _sync_directories(self, directory):
        """从变更目录向数据根目录逐级同步，持久化目录项变动。"""
        current = Path(directory).resolve()
        while current == self.root or self.root in current.parents:
            descriptor = os.open(current, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if current == self.root:
                break
            current = current.parent

    def exists(self, path):
        target = Path(path).resolve()
        if self.root not in target.parents:
            raise KGError("INVALID_ARGUMENT", "File path is outside the data root")
        return target.exists()

    def read_json(self, path):
        target = Path(path).resolve()
        if self.root not in target.parents:
            raise KGError("INVALID_ARGUMENT", "File path is outside the data root")
        try:
            value = json.loads(target.read_text("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object")
            return value
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as exc:
            raise KGError("PLAN_INVALID", "Checkpoint JSON is invalid", 503) from exc

    def job_checkpoints(self, job_id):
        return [
            value
            for name in ("preparation.json", "prepared.json")
            if (value := self.read_json(self.job_dir(job_id) / name)) is not None
        ]

    async def stage_upload(self, upload, max_bytes):
        """分块接收上传并执行大小限制，失败或取消时移除暂存文件。"""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise KGError("INVALID_ARGUMENT", "Upload size budget must be positive")
        temporary = self.path("uploads", new_id() + ".tmp")
        size = 0
        stream = temporary.open("xb")
        try:
            os.chmod(temporary, 0o600)
            while block := await upload.read(1024 * 1024):
                if not isinstance(block, bytes):
                    raise KGError("INVALID_ARGUMENT", "Upload reader must return bytes")
                size += len(block)
                if size > max_bytes:
                    raise KGError(
                        "UPLOAD_TOO_LARGE", "File exceeds configured upload size", 413, max_bytes=max_bytes
                    )
                await blocking(stream.write, block)
            if size == 0:
                raise KGError("EMPTY_DOCUMENT", "Empty upload is not supported")

            # 关闭前保证内容持久化，随后才可发布到正式原文目录。
            await blocking(stream.flush)
            await blocking(os.fsync, stream.fileno())
        except BaseException:
            # 包括取消在内的所有失败都要先关句柄，再移除未发布文件。
            await blocking(stream.close)
            await blocking(temporary.unlink, missing_ok=True)
            raise
        else:
            await blocking(stream.close)
        return {"path": str(temporary), "byte_length": size}

    def publish_upload(self, temp_path, kb_id, document_id, extension):
        """将暂存文件移动到正式原文路径，并同步来源和目标目录。"""
        temporary = Path(temp_path).resolve()
        if temporary.parent != self.path("uploads"):
            raise KGError("INVALID_ARGUMENT", "Upload stage is outside the staging directory")
        target = self.raw_path(kb_id, document_id, extension)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(temporary, target)
        self._sync_directories(target.parent)
        self._sync_directories(temporary.parent)
        return str(target)

    def orphan_files(self, referenced_paths, cutoff):
        """列出超过保留期限且未被元数据引用的原文和上传暂存文件。"""
        candidates = list(self.path("uploads").glob("*.tmp"))
        candidates += [
            path for path in self.path("raw").glob("*/*/original.*") if str(path) not in referenced_paths
        ]
        result = []
        for path in candidates:
            try:
                if path.stat().st_mtime < cutoff:
                    result.append(str(path))
            except FileNotFoundError:
                continue
        return result
