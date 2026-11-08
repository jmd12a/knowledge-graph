# 仓库工作说明

## 代码位置

- `src/knowledge_graph/domain/`：领域对象、校验与错误类型；`ports/`：外部能力接口。
- `src/knowledge_graph/services/`：应用流程，按 `construction/`、`retrieval/`、`documents/`、`model_config/` 分目录。
- `src/knowledge_graph/adapters/`：数据库、向量库、文件、解析器和模型服务实现。
- `src/knowledge_graph/interfaces/`：HTTP、MCP 与 CLI 入口；`runtime/`：进程锁和查询门禁。
- `src/knowledge_graph/bootstrap.py`：组装服务与适配器；`settings.py`：运行配置；`prompts/`：模型提示词。
- `evals/`：评测代码、冻结语料和发布证据；`examples/`：业务模型与输入样例。
- `configs/`：配置模板；`deploy/`：部署模板；`scripts/`：本地检查脚本；`docs/`：设计、运行与验证说明。

依赖方向为 `interfaces → services → domain/ports`。适配器实现端口并由 `bootstrap.py` 装配。改动跨层契约时，检查调用方、端口、适配器及对应评测入口。

## Python 风格

- 使用 Python 3.12。文件采用 UTF-8、LF 换行、4 空格缩进和末尾换行；JSON、YAML 使用 2 空格缩进。Markdown 保留有意添加的行尾空格。以 `.editorconfig` 为准。
- Python 代码由 Ruff 格式化，行宽 110，字符串使用双引号。导入按标准库、第三方库、本项目模块分组并在组内排序；Ruff 的 `I` 规则负责检查顺序。
- 模块、函数、变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。已有外部协议字段和存储字段名称是兼容性契约，不因风格整理而改名。
- 新增跨模块接口时写明参数和返回值类型；避免为局部临时变量添加无信息量的标注。优先用明确的变量名、中间结果和提前返回降低嵌套，不为了缩短文件而拆散同一职责。
- 注释只解释代码无法直接表达的原因、外部约束和恢复语义，不逐行复述操作。修改行为时同步检查相关注释，删除过时描述；新增代码注释和项目文档使用中文。
- 可预期的应用错误使用现有 `KGError` 和错误码；在接口层转换协议错误。异常不得被空 `except` 吞掉。日志记录必要的错误类别和标识，不输出密钥、原文或模型推理内容。
- 日常只格式化本次修改的文件。全仓风格调整与行为修改分开，避免混在同一差异中。

## 优化工作方式

- **先建立基线。** 做效果、性能或成本优化时，先明确目标、典型失败和验收标准。用改动前的版本跑代表性样本。评测入口见 [评测说明](evals/README.md)。
- **读开源项目的实际实现。** 遇到关键设计取舍或多轮优化效果不佳时，选解决同类问题的项目，沿入口读核心代码和相关测试。记录参考版本、适用条件，以及要在本项目验证的做法。
- **拆开验证，再测通用性。** 用已知失败案例定位问题。对照时固定数据、模型、预算等条件，逐项验证改动。结果有波动时重复运行，并用独立样本复测。
- **用结果决定取舍。** 报告改动前后的指标、退步场景、额外开销和适用范围。能重复验证的结论才用于决定主链路，复现步骤留在评测记录里。

## 配置与验证

- 依赖声明在 `pyproject.toml`，固定版本在 `requirements.lock`。本机 `.env`、`configs/*.local.*`、`data/` 和 `evals/records/` 不纳入版本控制。
- 修改接口、配置或存储字段时，同步检查相关示例、`docs/03-使用与验证/运行指南.md` 和 `src/knowledge_graph/adapters/metadata/migrations/`。
- 常用静态检查：

  ```bash
  .venv/bin/ruff check --no-cache src evals scripts
  .venv/bin/ruff format --check --no-cache src evals scripts
  .venv/bin/python -m evals.run --check
  ```

- `evals/README.md` 说明真实评测的环境、入口与结果口径。`--check` 只做静态预检；需要验证实际行为时，运行与改动对应的评测，并如实报告所用依赖和未覆盖范围。
- 完成前检查 diff 与 `git status`，确认没有无关格式改动、凭据或运行产物。
