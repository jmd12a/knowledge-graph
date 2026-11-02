# 人工服务目录机制样例

这里的记录是明确设计的机制输入，不是企业业务语料，不用于报告企业抽取准确率。

- `model.yaml` 定义 Service/Team、按 app_id/team_code 对齐、CALLS/OWNED_BY 方向、单值 status 和唯一归属冲突规则。
- `source-a.csv` 使用 `calls_and_caller_owner`，包含调用链/循环、同名异码、A 独有别名/描述与完整名称。
- `source-b.json` 使用 `calls_and_callee_owner`，保留共同调用关系，对 pay-core 提供 degraded 和 ops-team，同时只提供该端点身份而不提供名称。
- `recovery.csv` 是小型真实写入恢复输入；`isolation.csv` 在另一个知识库使用相同 app_id 检查隔离。

CSV 的 aliases 为单个字符串声明，`merge: union` 用于保留各来源别名；映射不执行代码或猜测列含义。JSON 端点显式声明 identity_only，删除 A 后必须撤回名称、别名与描述。

集中入口：在 WSL 项目根目录执行 `.venv/bin/python -m evals.run --check`。完整运行方式见[评测运行说明](../../evals/README.md)。
