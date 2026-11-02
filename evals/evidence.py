from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("mechanisms", "contract", "recovery")


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


class CaseFailure(RuntimeError):
    pass


class PaidValidationStopped(RuntimeError):
    pass


class Evidence:
    def __init__(self, run_dir=None, *, port=8765):
        records = (ROOT / "evals" / "records").resolve()
        self.directory = Path(run_dir).resolve() if run_dir else records / str(uuid4())
        if self.directory.parent != records or UUID(self.directory.name).version != 4:
            raise ValueError("run-dir must be evals/records/<UUIDv4>")
        path = self.directory / "manifest.json"
        if path.exists():
            self.state = read_json(path)
            if self.state["run_id"] != self.directory.name:
                raise ValueError("The run directory and saved run ID differ")
        else:
            self.state = {
                "schema_version": 1,
                "run_id": self.directory.name,
                "created_at": now(),
                "data_root": str(ROOT / "data" / "evals" / self.directory.name),
                "port": port,
                "actions": {},
                "checks": {},
                "stages": {},
                "kbs": {},
                "classification": {
                    "mechanisms": "Artificial service catalog mechanism cases; not enterprise corpus accuracy",
                    "contract": "Two complete public CUAD contracts; not the full CUAD benchmark",
                    "recovery": "Real storage/process recovery with artificial service catalog records",
                },
            }
            self.save()
        expected_root = str(ROOT / "data" / "evals" / self.state["run_id"])
        if self.state["data_root"] != expected_root:
            raise ValueError("Saved data root is not this run's isolated Linux directory")
        self.stage = "setup"

    def save(self):
        self.state["updated_at"] = now()
        write_json(self.directory / "manifest.json", self.state)

    def record(self, group, value):
        path = self.directory / group / (str(uuid4()) + ".json")
        write_json(path, {"recorded_at": now(), "stage": self.stage, **value})
        return str(path.relative_to(self.directory))

    def stopped(self):
        path = self.directory / "PAID_VALIDATION_STOPPED.json"
        if path.exists():
            raise PaidValidationStopped(read_json(path)["reason"])

    def bind_sources(self, *, extra_files=()):
        """Keep source bytes so reused responses can never be presented as new-code validation."""
        additional = []
        for supplied in extra_files:
            path = Path(supplied).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file():
                raise CaseFailure(
                    "Additional source binding must name an existing file inside this repository"
                )
            additional.append(path)
        files = sorted(
            {
                *ROOT.joinpath("src").rglob("*.py"),
                *ROOT.joinpath("src").rglob("*.sql"),
                *ROOT.joinpath("evals").glob("*.py"),
                *ROOT.joinpath("examples").rglob("*.yaml"),
                *ROOT.joinpath("examples").rglob("*.csv"),
                *ROOT.joinpath("examples").rglob("*.json"),
                *ROOT.joinpath("evals", "corpus").rglob("*.txt"),
                ROOT / "evals" / "corpus" / "cuad" / "source-manifest.json",
                *additional,
            }
        )
        names = [str(path.relative_to(ROOT)) for path in files]
        previous = self.state.get("source_files")
        if previous is not None:
            if previous != names or any(
                (self.directory / "program-sources" / name).read_bytes() != (ROOT / name).read_bytes()
                for name in names
            ):
                raise CaseFailure(
                    "Program, model or source bytes changed; use a new run for new verification"
                )
            return
        for path, name in zip(files, names, strict=True):
            target = self.directory / "program-sources" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        self.state["source_files"] = names
        self.state["source_binding_created_at"] = now()
        self.save()

    def check(self, name, passed, expected, actual, *, evidence=None, required=False):
        value = {
            "name": name,
            "stage": self.stage,
            "status": "pass" if passed else "fail",
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "at": now(),
        }
        value["record"] = self.record("checks", value)
        self.state["checks"][value["record"]] = value
        self.save()
        if required and not passed:
            raise CaseFailure(name)
        return passed

    def pending(self, name, expected, actual=None, *, evidence=None):
        value = {
            "name": name,
            "stage": self.stage,
            "status": "unverified",
            "expected": expected,
            "actual": actual,
            "evidence": evidence,
            "at": now(),
        }
        value["record"] = self.record("checks", value)
        self.state["checks"][value["record"]] = value
        self.save()

    def key(self, action):
        item = self.state["actions"].setdefault(action, {})
        if "key" not in item:
            item["key"] = str(uuid4())
            self.save()
        return item["key"]

    def saved(self, action):
        return self.state["actions"].get(action, {}).get("result")

    def remember(self, action, result):
        self.state["actions"].setdefault(action, {})["result"] = result
        self.save()
        return result

    def report(self):
        checks = list(self.state["checks"].values())
        counts = dict(Counter(item["status"] for item in checks))
        calls = [read_json(path) for path in sorted((self.directory / "model_calls").glob("*.json"))]
        call_counts = dict(Counter(item.get("outcome", "started") for item in calls))
        summary = {
            "run_id": self.state["run_id"],
            "generated_at": now(),
            "data_root": self.state["data_root"],
            "configuration": self.state.get("configuration"),
            "kbs": self.state["kbs"],
            "exit_code": self.state.get("exit_code"),
            "failure": self.state.get("failure"),
            "shutdown_error": self.state.get("shutdown_error"),
            "stages": {stage: self.state["stages"].get(stage, {"status": "not_run"}) for stage in STAGES},
            "check_counts": counts,
            "model_call_counts": call_counts,
            "model_calls": [
                {
                    key: item.get(key)
                    for key in (
                        "call_id",
                        "role",
                        "model",
                        "endpoint",
                        "outcome",
                        "elapsed_ms",
                        "usage",
                        "input_items",
                        "error",
                    )
                }
                for item in calls
            ],
            "checks": checks,
            "action_provenance": {
                key: {
                    field: item.get(field)
                    for field in ("request", "response_record", "executed_at", "reuse_records")
                }
                for key, item in self.state["actions"].items()
                if "response_record" in item
            },
            "unverified": [item for item in checks if item["status"] == "unverified"],
            "limits": [
                "No enterprise corpus accuracy claim",
                "No full CUAD benchmark score",
                "Contract semantic quality requires recorded human review",
                "Actual monetary cost is not measured",
            ],
        }
        write_json(self.directory / "report.json", summary)
        rows = [
            f"# 集中端到端评测记录 {self.state['run_id']}",
            "",
            f"生成时间：{summary['generated_at']}",
            "",
            "所有状态只代表实际执行证据。未执行、失败、人工待核验均不计为通过。",
            "",
            "| 阶段 | 状态 |",
            "|---|---|",
        ]
        for stage, value in summary["stages"].items():
            rows.append(f"| {stage} | {value['status']} |")
        if summary["exit_code"] is not None:
            rows += ["", f"程序退出码：{summary['exit_code']}。"]
        if summary["shutdown_error"]:
            rows += ["", "另有关闭阶段错误，完整类型和消息见 report.json 的 shutdown_error。"]
        rows += [
            "",
            f"机械核验：通过 {counts.get('pass', 0)}；失败 {counts.get('fail', 0)}；"
            f"未验证 {counts.get('unverified', 0)}。",
            "",
            f"真实模型端点调用记录：{len(calls)}。调用明细和原始响应保存在 model_calls/。",
            "",
            "| 阶段 / 核验 | 结果 | 证据 |",
            "|---|---|---|",
        ]
        for item in checks:
            label = (item["stage"] + " / " + item["name"]).replace("|", "\\|")
            link = item.get("evidence") or item["record"]
            rows.append(f"| {label} | {item['status']} | [记录]({link}) |")
        rows += [
            "",
            "逐项预期、实际、失败阶段见 report.json；原始 HTTP/MCP 响应见 responses/；",
            "完整图、贡献与索引诊断见 snapshots/；进程身份与中断证据见 processes/。",
            "",
            "合同人工核验表见 contract-review.json（如已生成），不得把 token 命中当成语义准确率。",
            "",
            "复用响应保留原始执行时间和证据路径，并在 reuses/ 明示 reused=true；不算新执行验证。",
            "程序、模型或资料字节变化必须新建 run；program-sources/ 保存本次执行的原始程序及输入。",
            "",
            "默认保留所有证据和独立数据库数据。本程序不删除整库 Neo4j 或其他知识库的 Qdrant 集合。",
            "",
        ]
        (self.directory / "report.md").write_text("\n".join(rows), encoding="utf-8")
        return summary


def without_request_ids(value):
    if isinstance(value, dict):
        return {key: without_request_ids(item) for key, item in value.items() if key != "request_id"}
    if isinstance(value, list):
        return [without_request_ids(item) for item in value]
    return value
