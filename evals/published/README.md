# 最新验证证据

仅保留运行 `e316c7ca-7974-4b6d-9392-45868da2d2d7`，旧运行和旧发布副本已按项目维护要求清理。

完整 CUAD 长合同的 **26 次查询有 23 次达到原文证据完整覆盖**：entity 10/12、chunk 11/12、retrieve 2/2。Top-K 为 5，原文、固定问题及评分口径未修改。55 项机械检查通过，不与查询通过数相加，也不表示业务语义验收完成。

| 文件 | 内容 |
|---|---|
| [questions.json](latest/questions.json) | 全部 26 次查询的原问题、路由、通过状态与证据计数，包含 3 次未通过 |
| [summary.json](latest/summary.json) | 原运行摘要、模型调用数量与检查计数 |
| [retrieval.json](latest/retrieval.json) | 原逐题排名、引用范围和覆盖明细 |
| [coverage.json](latest/coverage.json) | 图谱正文、fact、context 和切片覆盖，分别统计 |
| [issues.json](latest/issues.json) | 构建成功状态、数量与候选问题 |
| [build.json](latest/build.json)、[preflight.json](latest/preflight.json) | 原构建与输入冻结信息 |
| [图快照](latest/snapshots/bc867b9a-1e13-40c2-ba82-9cd6e5731793.json) | 本轮实体、关系、贡献和切片诊断快照 |
| [来源清单](provenance.json) | 每份发布文件对应的原始文件及本机路径脱敏次数 |

`latest/responses/` 保存各题直接引用的真实 HTTP 响应和实体详情。其他原始日志、模型请求与运行源码快照仍在本地 `evals/records/e316c7ca-7974-4b6d-9392-45868da2d2d7/`，没有随仓库上传。发布只把 `/srv/knowledge-graph/` 前缀替换为 `<LOCAL_PATH>/`，不修改问题、模型回答、数值或失败结果。原 JSON 内相对记录路径以原运行目录为基准。

构建与向量化均通过 OpenRouter：`z-ai/glm-5.3`、`google/gemini-embedding-2`（3072 维）。最新检索证据来自可读性整理前的真实运行；后续代码整理未重跑模型，不能算另一轮验证。

尚未通过：minimum_commitment/entity、license_grant/entity、license_grant/chunk。原因及口径见[评测说明](../../docs/03-使用与验证/评测说明与结果.md)。本轮不覆盖完整 CUAD、22 类负例的幻觉率或最终法律答案正确率。语料来源与 CC BY 4.0 许可见[清单](../corpus/cuad-long/source-manifest.json)。
