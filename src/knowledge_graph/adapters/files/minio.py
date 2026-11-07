"""把来源和恢复材料保存在 MinIO，本机目录只承载锁和临时文件。"""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryFile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ...domain import KGError, canonical, valid_id
from ...runtime import blocking
from .local import Files, _ContentReader


log = logging.getLogger(__name__)


_MATERIALS = ("preparation.json", "extraction-results.json", "prepared.json")
_CLOSED = {"kg-closed": "true"}
_MISSING = {"404", "NoSuchKey", "NotFound"}


class MinioFiles(Files):
    def __init__(self, root, settings):
        super().__init__(root)
        access_key = os.getenv(settings.minio_access_key_env)
        secret_key = os.getenv(settings.minio_secret_key_env)
        if not access_key or not secret_key:
            raise KGError("STORAGE_NOT_CONFIGURED", "MinIO credentials are missing", 503)
        self.bucket = settings.minio_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.minio_endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(
                connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 1},
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required", response_checksum_validation="when_required",
            ),
        )

    def close(self):
        self.client.close()

    def _request(self, method, *, missing_ok=False, **kwargs):
        try:
            return getattr(self.client, method)(Bucket=self.bucket, **kwargs)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if missing_ok and code in _MISSING:
                return None
            if code in {"409", "412", "PreconditionFailed", "ConditionalRequestConflict"}:
                raise KGError("OBJECT_WRITE_CONFLICT", "The object changed before its conditional write", 503,
                              retryable=True) from exc
            raise KGError("OBJECT_STORE_UNAVAILABLE", "MinIO did not complete the operation", 503,
                          retryable=True) from exc
        except (BotoCoreError, OSError) as exc:
            raise KGError("OBJECT_STORE_UNAVAILABLE", "MinIO did not complete the operation", 503,
                          retryable=True) from exc

    def initialize(self):
        super().initialize()
        self.ready()
        versioning = self._request("get_bucket_versioning").get("Status")
        if versioning:
            raise KGError("STORAGE_NOT_CONFIGURED", "MinIO requires a bucket without object version history", 503)

    def ready(self):
        self._request("head_bucket")
        return True

    @staticmethod
    def _key(path):
        value = str(path)
        parts = value.split("/")
        if parts[0] not in {"raw", "jobs"} or any(part in {"", ".", ".."} for part in parts) or "\\" in value:
            raise KGError("INVALID_ARGUMENT", "Invalid object key")
        return value

    @staticmethod
    def _closed(head):
        return bool(head and head.get("Metadata", {}).get("kg-closed") == "true")

    def _head(self, path):
        return self._request("head_object", Key=self._key(path), missing_ok=True)

    def _objects(self, path):
        prefix = self._key(path) + "/"
        paginator = self.client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                yield from page.get("Contents", [])
        except (BotoCoreError, ClientError, OSError) as exc:
            raise KGError("OBJECT_STORE_UNAVAILABLE", "MinIO could not list objects", 503, retryable=True) from exc

    def raw_directory(self, kb_id, document_id):
        return Path("raw", valid_id(kb_id), valid_id(document_id))

    def raw_path(self, kb_id, document_id, extension):
        name = super().raw_path(kb_id, document_id, extension).name
        return self.raw_directory(kb_id, document_id) / name

    def job_dir(self, job_id):
        return Path("jobs", valid_id(job_id))

    def attempt_dir(self, job):
        return self.job_dir(job["job_id"]) / str(job["queue_seq"]) / str(job["attempt"])

    def begin_preparation(self, job):
        self.atomic(self.attempt_dir(job) / "scope.json", {"kb_id": job["kb_id"]})

    def read_preparation(self, job):
        for attempt in range(job["attempt"] - 1, 0, -1):
            path = self.attempt_dir(job | {"attempt": attempt}) / "preparation.json"
            try:
                value = self.read_json(path)
            except KGError as exc:
                if exc.code != "PLAN_INVALID":
                    raise
                log.warning("Ignoring invalid draft for job %s attempt %s", job["job_id"], attempt)
                continue
            if value is not None:
                return value
        return None

    def _read_plan_wrapper(self, job):
        path = Path(self._key(job.get("plan_path", "")))
        expected = self.job_dir(job["job_id"]) / str(job["queue_seq"])
        attempt = path.parent.name
        if path.parent.parent != expected or not attempt.isdecimal() or int(attempt) < 1 or path.name != "prepared.json":
            raise ValueError("Unexpected prepared plan key")
        return self.read_json(path)

    def extraction_path(self, job):
        return Path(self._key(job["plan_path"])).with_name("extraction-results.json")

    def _cleanup_target(self, path):
        return Path(self._key(path))

    def atomic(self, path, value):
        key = self._key(path)
        previous = self._head(path) if Path(path).name == "preparation.json" else None
        if self._closed(previous):
            raise KGError("PREPARATION_REPLACED", "This preparation is closed", 409)
        condition = {"IfMatch": previous["ETag"]} if previous else {"IfNoneMatch": "*"}
        self._request(
            "put_object", Key=key, Body=canonical(value).encode("utf-8"), ContentType="application/json", **condition,
        )

    def read_json(self, path):
        result = self._request("get_object", Key=self._key(path), missing_ok=True)
        if result is None:
            return None
        try:
            with closing(result["Body"]) as body:
                if self._closed(result):
                    return None
                value = json.load(body)
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                return value
        except (ValueError, UnicodeError) as exc:
            raise KGError("PLAN_INVALID", "Checkpoint JSON is invalid", 503) from exc
        except (BotoCoreError, OSError) as exc:
            raise KGError("OBJECT_STORE_UNAVAILABLE", "MinIO could not read the checkpoint", 503,
                          retryable=True) from exc

    def exists(self, path):
        target = Path(self._key(path))
        if target.parts[0] == "jobs" and len(target.parts) == 2:
            return next(self._objects(target), None) is not None
        info = self._head(target)
        return info is not None and not self._closed(info)

    def job_checkpoints(self, job_id):
        paths = [Path(item["Key"]) for item in self._objects(self.job_dir(job_id))]
        planned = {path.parent for path in paths if path.name == "prepared.json"}
        result = [
            value for path in paths
            if path.name in {"preparation.json", "prepared.json"} and (value := self.read_json(path)) is not None
        ]
        if any(path.name == "scope.json" and path.parent not in planned for path in paths):
            # 旧尝试可能刚登记范围，还没来得及写入可核对来源的草稿。
            result.append({"unsealed_attempt": True})
        return result

    def publish_upload(self, temp_path, kb_id, document_id, extension):
        temp = self.path(temp_path)
        if temp.parent != self.path("uploads"):
            raise KGError("INVALID_ARGUMENT", "Upload stage is outside the staging directory")
        key = self.raw_path(kb_id, document_id, extension)
        with temp.open("rb") as source:
            self._request("put_object", Key=str(key), Body=source, IfNoneMatch="*")
        return str(key)

    def _open_raw(self, path):
        key = self._key(path)
        if not key.startswith("raw/"):
            raise KGError("INCONSISTENT_STORAGE", "Registered document object is invalid", 503)
        result = self._request("get_object", Key=key, missing_ok=True)
        if result is None:
            raise KGError("SOURCE_UNAVAILABLE", "Registered document content is unavailable", 503, retryable=True)
        return result

    async def open_content(self, path, expected_bytes):
        opened = []

        def open_checked():
            result = self._open_raw(path)
            opened.append(result["Body"])
            if result["ContentLength"] != expected_bytes:
                raise KGError("INCONSISTENT_STORAGE", "Registered document length does not match its content", 503)

        try:
            await blocking(open_checked)
        except BaseException:
            if opened:
                await blocking(opened[0].close)
            raise
        return _ContentReader(opened[0])

    def same_bytes(self, first, second):
        try:
            with ExitStack() as stack:
                readers = []
                for path in (first, second):
                    if path.is_absolute():
                        temp = self.path(str(path))
                        if temp.parent != self.path("uploads"):
                            raise KGError("INVALID_ARGUMENT", "Upload stage is outside the staging directory")
                        readers.append(stack.enter_context(temp.open("rb")))
                    else:
                        readers.append(stack.enter_context(closing(self._open_raw(path)["Body"])))
                while True:
                    left, right = (reader.read(1024 * 1024) for reader in readers)
                    if left != right:
                        return False
                    if not left:
                        return True
        except BotoCoreError as exc:
            raise KGError("SOURCE_UNAVAILABLE", "MinIO could not compare document content", 503,
                          retryable=True) from exc

    def parse_document(self, parser, document, model):
        # 匿名文件在进程退出时释放，远端 Worker 中断也不会留下原文副本。
        with TemporaryFile(dir=self.path("uploads")) as temporary:
            result = self._open_raw(document["raw_path"])
            try:
                with closing(result["Body"]) as source:
                    shutil.copyfileobj(source, temporary, length=1024 * 1024)
            except (BotoCoreError, OSError) as exc:
                raise KGError("SOURCE_UNAVAILABLE", "MinIO could not read document content", 503,
                              retryable=True) from exc
            if temporary.tell() != document["byte_length"]:
                raise KGError("INCONSISTENT_STORAGE", "Registered document length does not match its content", 503)
            temporary.flush()
            return parser.parse(document | {"raw_path": f"/proc/self/fd/{temporary.fileno()}"}, model)

    @staticmethod
    def _attempt(path):
        parts = Path(path).parts
        if (
            len(parts) != 4 or parts[0] != "jobs"
            or not all(value.isdecimal() and int(value) > 0 for value in parts[2:])
        ):
            raise KGError("INVALID_ARGUMENT", "Invalid preparation prefix")
        valid_id(parts[1])
        return Path(*parts)

    def retire_attempt(self, path):
        """先封住不确定的对象键，防止迟到的条件 PUT 恢复已删正文。"""
        path = self._attempt(path)
        prepared = self._head(path / "prepared.json")
        if self._closed(prepared):
            return
        if prepared:
            # 正式计划最后写入，前序上传均已确认。保留最后一个对象直至其他对象已清理。
            for name in ("preparation.json", "extraction-results.json", "scope.json", "prepared.json"):
                self._request("delete_object", Key=str(path / name))
            return
        for name in _MATERIALS:
            target = path / name
            if not self._closed(self._head(target)):
                self._request("put_object", Key=str(target), Body=b"", Metadata=_CLOSED)

    def remove(self, path):
        target = Path(path)
        if target.is_absolute():
            if self.path(path).parent != self.path("uploads"):
                raise KGError("INVALID_ARGUMENT", "Cleanup target is outside the upload staging directory")
            return super().remove(path)
        parts = Path(self._key(path)).parts
        if parts[0] == "jobs" and len(parts) == 2:
            valid_id(parts[1])
            attempts = {Path(item["Key"]).parent for item in self._objects(target)}
            for attempt in sorted(attempts):
                self.retire_attempt(attempt)
        elif parts[0] == "raw" and len(parts) in {3, 4}:
            valid_id(parts[1])
            valid_id(parts[2])
            keys = [item["Key"] for item in self._objects(target)] if len(parts) == 3 else [str(target)]
            for key in keys:
                self._request("delete_object", Key=key)
        else:
            raise KGError("INVALID_ARGUMENT", "Cleanup target is outside the explicit object subtree")

    def attempts_page(self, cursor):
        options = {"ContinuationToken": cursor} if cursor else {}
        page = self._request("list_objects_v2", Prefix="jobs/", MaxKeys=100, **options)
        attempts = {}
        for item in page.get("Contents", []):
            path = self._attempt(Path(item["Key"]).parent)
            modified = item["LastModified"].timestamp()
            attempts[path] = max(modified, attempts.get(path, 0))
        return page.get("NextContinuationToken"), attempts

    def orphan_files(self, referenced_paths, cutoff):
        return [
            item["Key"] for item in self._objects("raw")
            if item["Key"] not in referenced_paths and item["LastModified"].timestamp() < cutoff
        ]
