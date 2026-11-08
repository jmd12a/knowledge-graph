# 端到端评测

查看成绩、样本和对照图，先读[评测说明与结果](../docs/03-使用与验证/评测说明与结果.md)。本目录保留真实依赖和进程检查、代表性机制用例、真实文档及模型效果的执行入口。未运行、凭据缺失和中途失败分别记录。

## 从仓库能复现什么

新检出可以运行下面的入口，运行配置、真实存储和模型凭据按用例另行准备。历史成绩与具体版本、快照和模型绑定。

| 测试 | 仓库里的输入与入口 | 还需准备 |
| --- | --- | --- |
| 静态预检 | `python -B -m evals.run --check`、`python -B -m evals.long_contract --check`、`python -B -m evals.quality_labeled --check` | 项目虚拟环境。 |
| 确定性回归 | `python -B -m evals.quality_deterministic`。它也运行分窗、检索证据和评审回归。`python -B -m evals.quality_retrieval_evidence` 可单独跑检索边界。 | 项目虚拟环境。用固定输入验证程序行为。 |
| 构建、删除、恢复和短合同 | `evals.run` 的 `mechanisms`、`contract`、`recovery` 阶段。输入在 `examples/` 与 `corpus/cuad/`。 | 隔离的真实存储。合同与语义检索使用有效模型配置。 |
| 长合同与小标注集 | `corpus/cuad-long/` 含原文、题目和官方范围，入口是 `evals.long_contract`。`corpus/quality-labeled/` 含小标注集，入口是 `evals.quality_labeled`。 | 两组都需要真实模型。长合同还需要隔离存储，小标注集用内存图。全文与结构化策略对照见下文。 |
| 分布式准备故障 | `evals.distributed_worker --config <测试配置>`。用例在代码中，模型响应用固定数据。 | 运行指南中的隔离测试存储及配置。 |

### 历史结果与额外实验

| 评测 | 仓库可核对的内容 | 完整复跑所需的本地材料 |
| --- | --- | --- |
| 首版长合同 23/26 | 冻结语料、[逐题与原始响应](published/README.md)。 | 首版真实模型与隔离存储。旧源码和运行配置快照另存于原始运行档案。 |
| 当前版 25/26、同模型重建及固定图检索 | 长合同原文与题目、版本号、[逐题计分摘要](published/optimization-summary.json)。 | 固定结构化快照、历史运行配置和版本对照脚本。仓库现有长合同入口可创建新快照。 |
| TechQA、HotpotQA、Qasper 与额外 CUAD 长文 | [原文、题目与发行件清单](corpus/public/README.md)，入口是 `evals.public_quality`。 | Qasper 和 CUAD 合同 2 需下载发行件。历史固定候选对照还需要当时的图和候选顺序。 |
| 独立多文档留出样本 | [5 篇服务目录和 7 篇独立手册](corpus/holdout/README.md)，入口是 `evals.holdout_quality`。 | 真实存储与模型。已退出主链路的历史重排变体需对应源码与快照。 |

原始模型载荷、凭据和运行数据通过本地配置及运行目录管理。仓库摘要用于核对分数和分母，完整复跑以对应的输入、代码、配置和快照为准。

## 公开语料与多文档留出样本

[公开评测语料](corpus/public/README.md)列出可直接打开的原文、题目和来源。仓库直接保存 12 篇 TechQA、24 篇 HotpotQA 摘要和 1 篇完整 CUAD 合同。[独立多文档留出集](corpus/holdout/README.md)另含两组共 12 篇原文。预检只读本地文件并核对 SHA-256、标注和业务模型。

```bash
for case in techqa hotpot cuad-1 cuad-1-cross; do
  .venv/bin/python -m evals.public_quality "$case" --check || exit 1
done
for case in service-catalog procedures; do
  .venv/bin/python -m evals.holdout_quality "$case" --check || exit 1
done
```

### 还原其余长文

在项目根目录下载 Ai2 Qasper v0.3 和 CUAD v1 测试集，按清单的文件名保存。脚本检查发行件和生成样本的校验值，并固定问题 ID、Qasper 标注者与 CUAD 条款。完整语料和复跑输出放在已忽略的 `evals/records/`。

```bash
replay=evals/records/public-replay
mkdir -p "$replay/sources"
curl -fL 'https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-test-and-evaluator-v0.3.tgz' \
  -o "$replay/sources/qasper-test-v0.3.tgz"
curl -fL 'https://raw.githubusercontent.com/Break-Through-Tech/Accenture-1T-contract-review-challenge/main/data/test.json' \
  -o "$replay/sources/cuad-test.json"

.venv/bin/python -m evals.public_corpus --sources "$replay/sources" --fixtures "$replay/fixtures" \
  qasper-1 qasper-1-cross qasper-2 cuad-2
.venv/bin/python -m evals.public_quality qasper-1 --fixtures "$replay/fixtures" --check
```

也可从[清单中的发行件](corpus/public/manifest.json)重建仓库内置样本。HotpotQA 的上游文件为 Parquet，只有从该文件还原时需要 PyArrow。直接预检或复跑仓库内置样本不需要安装它。

### 运行当前链路

用隔离 PostgreSQL、Neo4j、Qdrant 和真实模型，按[运行指南](../docs/03-使用与验证/运行指南.md)初始化测试存储。使用单 Worker 配置。公开样本历史构建采用 `extraction_scope_policy: structure`、GLM 5.3、Gemini Embedding 2 和 15 秒检索预算。复跑使用所给配置，并记录实际版本和参数。

```bash
.venv/bin/python -m evals.public_quality hotpot --config configs/eval.local.yaml --max-requests 240
.venv/bin/python -m evals.public_quality qasper-1 --fixtures "$replay/fixtures" \
  --config configs/eval.local.yaml --modes compact --max-requests 140
.venv/bin/python -m evals.holdout_quality service-catalog \
  --config configs/eval.local.yaml --max-requests 100
.venv/bin/python -m evals.holdout_quality procedures --rounds 2 \
  --config configs/eval.local.yaml --max-requests 180
```

运行器为每轮创建新的知识库及 `evals/records/<运行 ID>/`。输出记录文档和任务 ID、版本与模型、候选顺序、逐题覆盖、降级警告和请求次数。公开样本默认在同一份新图上分别执行融合顺序和紧凑重排，实体和原文各取 Top 5。TechQA 的无答案题可用 `--techqa-probe` 单独检查候选能否提供足够证据，不计入正题覆盖率。跨窗补充题可直接使用 `qasper-1-cross` 或 `cuad-1-cross`。

该入口复用历史样本和评分口径，构建、召回和重排走当前实现。历史运行的网关变更、模型波动、固定候选回放及已归档变体需使用对应配置和快照。超过调用上限或构建未发布时，运行退出并保留已生成记录。

## 资料

- `corpus/cuad/` 保存两份完整的公开商业合同原文及来源清单。来源为 The Atticus Project 的 CUAD v1，许可 CC BY 4.0。下载时保留原始字节，只缩短本地文件名。
- 服务依赖样例用于控制身份、共享来源和冲突条件，属于人工设计的机制 case。其运行结果不能代表实际企业语料准确率。
- CUAD 小样本用于检查真实文本抽取、完整条款和出处，不是完整 CUAD benchmark，也不是用户业务验收集。

## 执行原则

已经通过独立 API 与 Worker 执行机制、合同和恢复评测。每次运行使用独立元数据目录、随机知识库 ID 和独立 Qdrant 集合，连接真实 Neo4j/Qdrant；不得修改已有库的数据或凭据。真实 Embedding 是构建与语义检索的前提，TXT/Markdown/PDF 抽取和自然语言规划还需要真实 LLM。缺少配置时报告阻塞，不启动固定答案、人工向量或模拟模型来替代端到端结果。

评测记录应包括：时间、程序与模型配置（不含密钥）、输入资料、知识库/资料/任务 ID、预期与实际结果、失败阶段、重试前后的 ID 与数量、原始响应记录路径。对象与请求标识采用随机 ID；配置和预期字段直接比较。

## 代表性 case

| Case | 输入与操作 | 必须核验 |
|---|---|---|
| 构建与身份 | 导入服务调用和团队归属记录；含同名不同编码 | 相同业务身份复用 ID，同名异码不合并，边方向正确，每项贡献可回查 |
| 幂等请求 | 重发同键相同请求；再用同键不同内容 | 相同请求返回原 ID；不同内容冲突；重启后仍成立 |
| 检索与证据 | 精确编码、自然语言、邻居、路径与资料范围查询 | 图事实与原文区分，路径每条边有依据，所选资料之外的声明不混入 |
| 共享来源删除 | A/B 支持同一关系，只有 A 支持额外属性；删除 A | 关系 ID 保留，贡献只剩 B，A 独有属性/描述/向量内容撤回 |
| 最后来源删除 | 删除 B；尝试重试旧 IMPORT | 无支持对象和切片清理，原文不可读取，旧任务不复活已删除资料 |
| 冲突及消退 | 同一服务单值属性或唯一归属出现矛盾；删除一方 | 双方声明与证据保留，任务和检索披露冲突；删除后相关冲突消退 |
| 部分写入恢复 | 在切片、贡献、图、对象向量和最终提交附近中断 Worker，再重启 | 维护标记阻止新知识读取，同一计划重放；对象/边/贡献无重复，最终恢复 READY |
| 读写隔离 | 保持在途读取，排队维护，并发 HTTP/MCP 新请求 | 在途读取完成前不写库；新请求明确维护中，取消不会提前释放仍被后台操作需要的锁 |
| 配置维护 | REBUILD 变更模型；REINDEX 变更向量配置 | 仅 active 资料参与，目标配置持久化，准备期旧知识可查，切换后图/索引/配置一致 |
| 合同场景 | 用独立合同模型导入两份 CUAD 文本 | 条款条件、完整定义和定位可核验，缺身份或缺精确证据的候选明确未采用；完整性标记保留但不单独否决，不以摘要替代正文 |

公开合同的人工核验重点：Web Hosting Agreement 中按访问次数分档的费率、每周付款时间、双方技术支持义务，以及提前终止时购买软件许可的条件和金额。只检查知识保存与证据，不让项目计算费用或作法律判断。Joint Filing Agreement 用于核验主体、联合申报、日期和原文范围。

## 结果状态

`published/latest/` 保留首版运行 `e316c7ca-7974-4b6d-9392-45868da2d2d7` 的详细证据。长合同 26 个动作通过 23 个，entity 10/12、chunk 11/12、retrieve 2/2。55 项机械检查通过，另行计数。后续对照的精简证据见[优化对照摘要](published/optimization-summary.json)。

[逐题结果](published/latest/questions.json)、[原始摘要](published/latest/summary.json)与[证据导航](published/README.md)保存本轮通过和未通过项。完整原件留在本地 `records/e316c7ca-7974-4b6d-9392-45868da2d2d7/`。结果绑定该次运行保存的源码和配置。

详细口径和限制见[评测说明](../docs/03-使用与验证/评测说明与结果.md)与[合同语义复核](../docs/03-使用与验证/合同语义复核.md)。

## 静态预检与实际运行

在项目虚拟环境中执行静态预检：

```bash
.venv/bin/python -m evals.run --check
.venv/bin/python scripts/check_evals.py
```

这两个入口只读取程序、模型、样例和语料清单，不启动API/Worker，也不访问数据库或模型。这些命令用于按需预检，不表示本次发布重新执行了业务验证。

真实业务运行使用以下入口，会启动本次评测独占的API与Worker，访问真实存储及模型：

```bash
.venv/bin/python -m evals.run --stage all
# 或分阶段执行；后续阶段使用同一轮输出的UUID：
.venv/bin/python -m evals.run --stage mechanisms
.venv/bin/python -m evals.run --stage contract --run-dir "evals/records/<UUID>"
.venv/bin/python -m evals.run --stage recovery --run-dir "evals/records/<UUID>"
```

新运行在 `records/<UUID>/` 保存manifest、report、检查、请求响应、模型调用和存储快照，在独立数据目录及随机知识库中运行。默认API端口为8765，新运行可用 `--port` 调整；续跑沿用原run保存的端口、配置、幂等键及资料和任务ID。评测进程只清理可核验属于本次run的子进程，默认保留真实存储数据，不提供清空全库入口。重用已有run会比较原程序、配置和请求，已保存的结果会明确标注复用；修改后的程序必须另开run，不能覆盖旧失败。模型鉴权或额度停止、关闭错误、未执行项和人工待核验项分别记录，不计作通过。

退出码0表示执行到的机械检查无失败，合同仍可能待人工核验；1表示失败；2表示模型鉴权或额度停止。实际模型、超时、预算和调用数量以对应run的原配置及报告为准，不用当前默认值追溯改写历史结果。公开发布只整理已有证据，不会自动触发新运行。

完整运行环境及模型配置见[运行指南](../docs/03-使用与验证/运行指南.md)和[模型服务与数据集调研](../docs/03-使用与验证/模型服务与数据集调研.md)。本页已合并旧评测交接文档中仍适用的命令和记录约定。

## 完整 CUAD 长合同

效果优化的确定性边界检查可单独执行：

```bash
.venv/bin/python -B -m evals.quality_deterministic
```

可单独执行 `.venv/bin/python -B -m evals.quality_retrieval_evidence` 检查原召回扫描预算、精确命中、来源隔离、主证据预算，以及模型返回未知候选或超时时的行为。真实检索对照固定已发布快照、问题、过滤范围、Top K 和模型配置，分别记录融合候选、实际送模数量、原文完整覆盖、token 和耗时。

该检查覆盖逐字字段的原文范围、DOCX/PDF 规范化分隔、别名身份判别与依赖投影、分批端点授权、缓存恢复和准备计划的版本/篡改拒绝。它不调用模型，不能替代真实效果评测。
旧协议的逐字字段优先使用模型返回的 `literal_parts`；只有完全缺失范围且完整字段值在本次已声明、可见的单个规则单元中唯一逐字命中时，程序才反查范围。构建计数 `literal_values_by_method` 分别记录 `model_range` 和 `unique_visible_match`；错误范围、改写和重复文本仍拒绝。

比较两种构建方式时固定资料、业务模型和模型参数。

```bash
KG_EXTRACTION_SCOPE_POLICY=whole .venv/bin/python -B -m evals.long_contract --port 8766
KG_EXTRACTION_SCOPE_POLICY=structure .venv/bin/python -B -m evals.long_contract --port 8766
.venv/bin/python -B -m evals.quality_compare evals/records/<whole-run-id> evals/records/<structure-run-id>
```

`structure` 按原文结构组窗。首轮按 `KG_EXTRACTION_STRUCTURE_WINDOW_CHARS` 设置去重后可见原文的目标大小，默认 10000 字符，单个完整块仍以模型硬预算为上限。模型以块 ID 或块内原样引文选择逐字字段，程序验证后从原文取值，并计入 `source_selection`。只有选区接到当前不可见的同一来源块时才扩窗补全，窗口内的待处理字段合并请求。确定性检查覆盖行内编号、完整与断开的候选、字段补丁和截断后恢复。

`whole` 优先在一次请求内覆盖全文，超过预算时按章节分批。对照脚本按路线读取正确的 Top-K 主指标：实体看详情后的条款文本，切片和自然语言检索看初始返回的原文切片；同时列出图中条款字面/证据覆盖及首轮、修复、组装、连接、嵌入、规划、重排的物理模型调用、耗时和上游返回的 token 用量。该长合同只有部分正例 span 标注，不计算总体 precision 或误合并率；独立身份负例与完整标注集仍需扩大到更多来源和业务场景。

`corpus/cuad-long/` 冻结 AlliedEsports Content License Agreement 的全部 35,525 字节原文、41 类官方 QA、固定问题和评分口径。19 类正例共30个官方 span；12题分别执行 entity/chunk TopK5，共24个语义搜索（每路18个参考span），另有2个自然语言 retrieve（9个人工原文参考段落），合计26个动作。官方标注只供评分，不进入模型构建。坐标覆盖不等于语义正确率，22个未查询负例不计幻觉率。

```bash
.venv/bin/python -B -m evals.long_contract --check
.venv/bin/python -B -m evals.long_contract --port 8766
```

第二条会调用真实模型。使用[OpenRouter 配置示例](../examples/contracts/openrouter-quality.yaml)中的语言、向量和检索配置，并提供 `OPENROUTER_API_KEY` 环境变量；密钥不写入版本控制。完整配置和运行准备见[合同示例说明](../examples/contracts/README.md)。

新代码或配置运行使用新 UUID，不覆盖当前保留档案。最新一轮采用完整冻结原文，模型与向量化都走 OpenRouter；结果和三次未通过查询见[最新验证证据](published/README.md)。

### 效果优化实测记录（2026-11-08）

下面三次长合同运行使用同一份 35,525 字节原文、业务模型、`z-ai/glm-5.3` 和 `google/gemini-embedding-2`。`b1afbd7c-620a-43ee-b814-24840f42a6eb` 是改动前基线；`93e251c9-af60-4fa6-b3f3-977253b4dcc1` 是逐字字段新协议后的全文单批；`1bbcd6e7-3da5-4009-b146-7151d228534d` 是组装边界修复后每批 3 个切片。各 run 的程序快照、参数、模型响应、完整报告都保存在 `evals/records/<run-id>/`，运行产物不进入 Git。全文 run 早于最后的词边界和组装修复，因此它与小批 run 是参考对照，不是同代码的因果 A/B。

| run / 范围 | 首轮批次 | 实体 / 关系 / 来源贡献 | 完整 Clause.definition | 图事实证据覆盖 | Top-K 路线主指标 | 物理模型调用 / 墙钟耗时 |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| `b1afbd7c…` / 全文旧基线 | 1 | 60 / 59 / 296 | 10/30 | 28/30 | 23/26 | 65 / 447.8 秒 |
| `93e251c9…` / 全文 | 1 | 60 / 61 / 301 | 17/30 | 29/30 | 24/26（实体 11/12、切片 11/12、检索 2/2） | 65 / 482.5 秒 |
| `1bbcd6e7…` / 小批 3 | 7 | 54 / 51 / 296 | 3/30 | 23/30 | **未完成**：额度耗尽前实体 4/5、切片 4/5，检索 0/2 已执行 | 106 / 1782.4 秒 |

小批 run 的 106 次物理调用包括首轮抽取 7、证据修复 6、JSON 纠正 2、规则组装 55、跨批连接 5、构建向量 9 和检索阶段 22。检索第 6 题遇到 `quota_exhausted`，剩余题目未执行，不能将 8/10 推断成 26 题得分，也不能与 24/26 直接相减。构建阶段产生 41 个候选问题：`EVIDENCE_MISSING` 13、`RULE_ASSEMBLY_UNRESOLVED` 10、`INVALID_ENDPOINT` 9、`LITERAL_RANGE_MISSING` 5、`LITERAL_BOUNDARY_UNRESOLVED` 4。全文 run 的完整条款和事实证据覆盖均高于本次小批 run；小批策略没有达到更改默认值的门槛，默认继续采用全文优先。模型额度耗尽后未运行最终代码的同代码全文对照。

另有一个全量标注的小语料，包含 TXT 与 Markdown、3 个 Service、3 个 Clause 实例和 1 条 CALLS 关系，并同时覆盖同名不同实体、别名同实体与同标题不同条款。早期无原子组装配置的 run `c0f51673-0f03-463c-a5f2-cd984e8eac45`：全文 Clause TP/FP/FN 为 2/0/1，小批为 0/2/3；两者 Service 均为 3/0/0、CALLS 均为 1/0/0。中间版本加 `full_statement` 的 run `761d1470-a869-4c02-8338-2bd91363b744` 小批 Clause 为 1/1/2，全文因模型结构错误失败。这些是诊断样本，不是最终代码的跨场景效果估计；标注集很小，不代表总体 precision。普通逐字模式只保证来源保真，不保证句子完整；模型在完全可见单元内仍可能选择词边界正确但语义未完的片段。不同完整身份但同义标题的安全归并留待下一轮独立设计和评测。

### 冻结基线与当前版复测记录

- **最早的全文旧基线**在 `evals/records/b1afbd7c-620a-43ee-b814-24840f42a6eb/`。GLM‑5.3 搭配 Gemini Embedding 2，原始长合同通过 **23/26**。该目录的 `program-sources/` 保存当时实际执行的程序，`manifest.json` 保存配置，`long-contract-retrieval.json` 保存逐题结果。复现旧版时以冻结程序为准。
- **当前推荐版**的代码快照为 `4531b68`。采用结构化构建快照、原召回和紧凑重排，图实体详情补完整段落，原文保留来源坐标与备选。GPT‑6 Sol 在原始合同上通过 **25/26**，图实体 12/12、原文 12/12、自然语言 1/2。复测复用了已发布的结构化快照，GPT‑6 Sol 用于重排及两次自然语言规划。逐题计分见[优化对照摘要](published/optimization-summary.json)。详细运行记录在评测工作区的 `contract-26-model-ab-gpt-6-sol-structure-verified/compact.json`。

上面的旧基线为内部冻结归档。对外展示的首版同为 23/26，对应[已发布的原始运行](published/README.md)。代表性成绩、完整版本对照及公开数据集结果统一见[评测说明与结果](../docs/03-使用与验证/评测说明与结果.md)。各轮保留自己的模型、快照与预算。共享证据组装保存在 `experiment/evidence-assembly` 分支的 `e7d25fa`。它在同模型的 59 组配对中总体持平，混合查询耗时下降，同时出现公开长文覆盖回归。

## 分布式 Worker 故障验收

为评测准备独立的 PostgreSQL 数据库、Redis、无版本历史的 MinIO 私有桶及 Neo4j、Qdrant 环境。配置方式见[运行指南](../docs/03-使用与验证/运行指南.md)。使用隔离配置运行机制用例：

```bash
.venv/bin/python -m evals.distributed_worker --config /path/to/test-app.yaml
```

用例创建两个临时知识库和独立的 Stream。它启动一个独立准备进程，在保存草稿后终止该进程，验证另一 Worker 复用草稿，并检查同库队头、写入计划重放、原文下载和迟到的条件 PUT。抽取与向量使用固定测试响应，存储与队列连接真实服务。
