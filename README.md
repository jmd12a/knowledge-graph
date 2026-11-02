# 可配置知识图谱服务

将文档和结构化资料构建为可追溯的实体、关系与规则知识，通过 HTTP API 和 MCP 提供检索。业务接入方定义类型、身份字段、关系和合并规则；服务负责资料解析、知识构建、来源维护与失败恢复。

Neo4j 保存当前图知识，Qdrant 辅助定位实体和原文证据，SQLite 保存资料、任务与来源贡献。最终回答、业务计算和操作执行由调用方或外部 Agent 完成。

图解阅读：[项目概览 HTML](docs/project-overview.html)（下载后用浏览器打开，含架构、流程、验证结果与使用入口）。

## 核心能力

- **按业务模型构建知识**：支持 TXT、Markdown、文本型 PDF，以及显式映射的 CSV/JSON。依据业务身份对齐实体，保留无法采用的候选及原因。
- **追溯知识来源**：属性、关系和完整规则保留来源贡献与精确位置。不同来源的规则字段不会拼成一条没有原文支持的声明。
- **维护共享知识**：删除资料时撤回其贡献，重新聚合仍受其他来源支持的对象；冲突值与各自来源同时保留。
- **检索图与原文**：提供精确、关键词和语义候选检索，以及邻居、遍历、路径和自然语言组合检索；返回歧义、冲突、证据及覆盖范围。
- **恢复中断任务**：正式写入前持久化完整计划；中断后沿用原计划、随机 ID 和已计算向量重放，核验完成后恢复知识查询。

身份与幂等使用随机 UUID、明确业务字段、数据库约束和完整内容比较，不计算内容摘要、配置指纹或身份哈希。

## 架构

![总体架构](docs/assets/diagrams/图01-总体架构.png)

| 模块 | 职责 |
|---|---|
| HTTP / MCP / CLI | 接收请求、校验协议参数、调用共享服务 |
| 业务服务 | 模型配置、资料登记、知识构建维护和检索 |
| 领域与端口 | 业务规则、结果契约及存储能力接口 |
| 适配器 | Neo4j、Qdrant、SQLite、文件、解析器及模型调用 |
| 运行时 | 查询门禁、进程锁、后台任务和取消处理 |

依赖方向为 `interfaces → services → domain/ports`，适配器由 `bootstrap` 统一装配。首版部署为同一 Linux 主机上的单 API 进程与单 Worker，维护任务串行执行。

构建分为准备和正式写入两个阶段：准备期间旧知识仍可读；正式写入前等待在途读取退出，维护完成前拒绝新的知识查询。完整设计见[架构设计](docs/02-项目设计/架构设计.md)与[实施设计](docs/02-项目设计/实施设计.md)。

## 快速开始

需要 Linux 或 WSL Ubuntu、Python 3.12、uv，以及可访问的 Neo4j、Qdrant 和模型服务。项目使用 Linux 文件锁，运行数据应位于 Linux 本地文件系统。以下以仓库路径 `/srv/knowledge-graph` 为例。

### 1. 安装依赖

```bash
cd /srv/knowledge-graph
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.lock
uv pip install --python .venv/bin/python --no-deps -e .
```

已有项目虚拟环境时跳过创建步骤。正式依赖由项目独立管理。

### 2. 配置服务与凭据

```bash
cp --no-clobber configs/app.example.yaml configs/app.local.yaml
cp --no-clobber configs/environment.example .env
chmod 600 .env
export KG_CONFIG=/srv/knowledge-graph/configs/app.local.yaml
```

编辑配置中的存储地址、数据目录、模型端点和名称；在 `.env` 中取消所需变量的注释并填写凭据。示例使用 OpenRouter 的 GLM 5.3 和 Gemini Embedding 2，设置 `OPENROUTER_API_KEY` 后可接入；数据库地址与凭据需按实际环境配置。配置加载顺序、不同安装路径和部署方式见[运行指南](docs/03-使用与验证/运行指南.md)。

结构化映射可以不调用 LLM，但构建向量索引仍需要真实 Embedding；文本抽取和自然语言规划还需要 LLM。项目不提供模拟模型运行模式。

### 3. 初始化并启动

API 与 Worker 均停止时初始化存储：

```bash
.venv/bin/kg init-storage
```

随后在两个终端分别启动，均使用同一份配置：

```bash
export KG_CONFIG=/srv/knowledge-graph/configs/app.local.yaml
.venv/bin/kg serve
```

```bash
export KG_CONFIG=/srv/knowledge-graph/configs/app.local.yaml
.venv/bin/kg worker
```

| 入口 | 默认地址 |
|---|---|
| HTTP API | `http://127.0.0.1:8000/api/v1` |
| OpenAPI 文档 | `http://127.0.0.1:8000/docs` |
| MCP Streamable HTTP | `http://127.0.0.1:8000/mcp` |
| 就绪检查 | `http://127.0.0.1:8000/api/v1/health/ready` |

### 4. 导入与检索

```bash
.venv/bin/kg model validate examples/service-catalog/model.yaml
.venv/bin/kg kb create service-demo examples/service-catalog/model.yaml
```

将返回值分别代入 `KB_ID`、`JOB_ID`：

```bash
.venv/bin/kg documents upload KB_ID examples/service-catalog/source-a.csv --key import-a
.venv/bin/kg jobs get JOB_ID
.venv/bin/kg jobs issues JOB_ID
.venv/bin/kg search KB_ID pay-core --no-semantic
```

上传成功表示任务已登记；Worker 成功提交后资料才参与当前知识库。完整删除、重建、重试及检索用法见[运行指南](docs/03-使用与验证/运行指南.md)。

MCP 提供五个只读工具：`describe_model`、`search_knowledge`、`get_knowledge`、`query_graph`、`retrieve`。检索结果包含 `status`、`coverage`、`conflicts` 和待消歧对象；候选 Top-K 不代表完整图集合，也不自动确认实体身份。

## 验证情况与边界

当前保留一轮完整 CUAD 长合同验证：35,525 字节原文、固定 Top-K 5，**26 次查询通过 23 次**。通过指找齐问题对应的原文证据，不是最终生成答案准确率。

| 检索方式 | 原文证据完整覆盖 |
|---|---|
| 图谱实体检索，含详情取证 | 10/12 |
| 原文切片检索 | 11/12 |
| 自然语言组合检索 | 2/2 |

本轮连接真实 Neo4j、Qdrant，并通过 OpenRouter 使用 GLM 5.3 与 Gemini Embedding 2。构建得到 68 个实体（63 个 Clause）、67 条关系、21 个切片，无候选拒绝；55 项机械检查通过。已知限制：付款清单拆分过细、许可段落 Top-K 召回不全，合计3次查询未达到完整覆盖。

运行标识 `e316c7ca-7974-4b6d-9392-45868da2d2d7`。完整结果和剩余失败见[评测说明](docs/03-使用与验证/评测说明与结果.md)与[最新证据](evals/published/README.md)。推荐配置见[合同示例](examples/contracts/README.md)。验证结果绑定该次运行保存的源码、模型配置和输入资料。

当前边界：

- 只维护当前知识，资料更新采用删除成功后重新导入；不提供历史版本查询或直接编辑图事实的管理入口。
- 公开合同小样本尚未完成业务方语义验收；扫描 PDF、复杂表格和企业私有语料没有形成验收结论。
- 单 Worker 和包含整库读取的维护流程尚未经过生产负载、容量和高可用验证。
- 本轮检索预算为180秒，代码默认检索预算为15秒，发布示例为180秒；已有结果不能作为默认预算下的延迟或成功率保证。
- 当前应用面向可信环境，完整多用户权限体系不在首版范围内。

## 仓库结构

```text
src/knowledge_graph/    分层应用源码
configs/                运行配置与凭据变量模板
deploy/                 API、Worker 的 systemd 模板
examples/               服务目录和合同业务模型与样例
evals/                  评测程序、语料、case计划及精选发布证据
scripts/                本地检查入口
docs/                   学习笔记、设计、运行与验证说明
```

本机凭据、运行数据、完整评测档案、缓存及本机配置不纳入版本控制。`evals/published/` 是带来源标识的脱敏发布副本，仅最新一轮原始运行档案保留在本地。

## 文档

- [文档导航](docs/README.md)：按概念、设计、运行和验证查找资料。
- [架构设计](docs/02-项目设计/架构设计.md)：系统边界、存储归属和维护语义。
- [实施设计](docs/02-项目设计/实施设计.md)：代码分层、数据结构、接口和恢复机制。
- [运行指南](docs/03-使用与验证/运行指南.md)：安装、配置、启动、使用与排错。
- [评测说明与结果](docs/03-使用与验证/评测说明与结果.md)：复现方式、实际结果和未验证范围。

第三方语料的来源和许可保留在[语料来源清单](evals/corpus/cuad/source-manifest.json)中。
