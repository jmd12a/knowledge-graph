# 集中端到端 case 计划

状态：当前仅保留完整长合同的最新一轮，26 次查询中 23 次达到原文证据完整覆盖。以下机制与恢复条目保留为验收计划，旧验证档案已清理；计划项不能直接计为当前版本通过。

## 服务目录机制 case

使用显式 Service、Team 类型及 CALLS、OWNED_BY 关系，业务身份分别是 app_id、team_code。资料中包含：

- 同一 app_id 在 A/B 两份资料重复出现，要求复用对象 ID，但两组来源贡献保留。
- pay-core 与 pay-demo 同名为“支付服务”，要求身份分开；自然语言图起点不能仅按向量第一名选择。
- A/B 对同一服务的单值 status 给出不同值、唯一归属指向不同团队，要求披露冲突而不是覆盖。
- 只有 A 支持的描述、别名；B 只支持端点身份时，删除 A 后相关字段撤回，端点可保留 identity_only。
- 有向调用链和循环，用于验证 incoming/outgoing、简单路径、跳数与边预算。

所有构建使用真实 Embedding。结构化映射无需 LLM 的部分应保持确定性，机制 case 的人工输入不能被记录为企业真实业务语料。

执行顺序为导入、幂等重放、检索与范围、冲突、删 A、删 B、旧任务重试拒绝。独立知识库用于隔离检查与 REBUILD/REINDEX，避免 case 前置条件互相污染。

## 真实合同 case

采用本目录 corpus/cuad 的两份完整公开合同及独立合同业务模型。输入文本不插入标注答案，不人为修饰以迁就抽取器。

Web Hosting Agreement 核验：

- 付款主体、收款主体及原文名字拼写差异的处理有明确身份依据；无法可靠合并时披露，不猜测。
- 每次访问 0.01 美元、超过 400,000 次后的 0.005 美元属于同一费率条款，两个分档条件不能丢失。
- 按前一周 Monday–Sunday 的访问量，最迟 Wednesday 支付，条款和证据完整。
- 24 小时、每周 7 天支持义务的双方、地点和联系方式条件。
- Galacticomm 选择终止时，Horst 的软件许可购买权与 15,000 美元金额同时保留。

Joint Filing Agreement 核验：主体、共同申报、证券与申报文件范围、2019-12-31 口径日期和 2020-01-22 签署日期不混淆。该 case 不执行法律判断或费用计算。

语料过小，不报告完整 CUAD benchmark 成绩。记录逐条期望、实际知识/证据、未采用原因和人工核验结论。

## 恢复及接口 case

在真实索引、贡献、图写入及最终元数据提交附近终止独立 Worker，重启后检查同一 plan_id、对象 ID 和来源集合；不得重新调用模型生成不同结果。故障注入只控制执行中断，不伪造模型或数据库返回。

另验证同一知识库在途读取持有共享锁时的维护等待；维护标记出现后新 HTTP/MCP 请求明确拒绝，读取取消后没有后台线程继续越过已释放的锁。记录各进程、任务、请求与维护状态证据。

HTTP 与 MCP 对同一请求比较知识内容、范围、冲突和错误码，忽略随机 request_id。任何通道降级、额度不足、未执行项目均明确记为未验证或失败，不计入通过。


## 完整长合同：冻结计划与当前结果

AlliedEsports Content License Agreement 的完整35,525字节原文、官方41类QA、问题及评分在模型调用前冻结于 `corpus/cuad-long/`。12题分别执行entity/chunk各Top-K 5，另有2题retrieve，共26次查询。坐标覆盖、初始与详情证据、独立语义复核分别统计。完整分母与规则见[固定case](corpus/cuad-long/fixed-case.json)。

保留运行 `e316c7ca-7974-4b6d-9392-45868da2d2d7`，entity 10/12、chunk 11/12、retrieve 2/2，合计23/26。未通过为minimum_commitment/entity、license_grant/entity、license_grant/chunk；原题、预期与实际结果全部保留。

55项机械检查通过，不计入查询通过数，也不替代语义验收。两类模型均走OpenRouter，配置见[合同示例](../examples/contracts/README.md)。本轮未执行上文完整机制与恢复矩阵；该结果仅对应保存的运行源码与配置。

[逐题结果](published/latest/questions.json)、[原始摘要](published/latest/summary.json)和[证据导航](published/README.md)提供当前依据。[来源清单](published/provenance.json)标注原始文件与脱敏位置。详细边界见[评测说明](../docs/03-使用与验证/评测说明与结果.md)、[合同语义复核](../docs/03-使用与验证/合同语义复核.md)与[工程验证与限制](../docs/03-使用与验证/工程验证与限制.md)。
