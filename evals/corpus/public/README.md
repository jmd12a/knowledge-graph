# 公开评测原文与题目

每组使用历史评测冻结的样本。仓库内置的三组可直接阅读并复跑，发布件和生成样本的 SHA-256 记录在[清单](manifest.json)。

| 样本 | 目录或准备方式 | 题目与标注 |
| --- | --- | --- |
| TechQA，12 篇 IBM 技术说明 | [原文目录](techqa/)。可从[一篇技术说明](techqa/swg24017225.txt)读起。 | [16 道题](techqa/questions.json)。8 道有答案题带原文支持范围，8 道无答案题用于单独的证据充分性探测。 |
| HotpotQA，24 篇百科摘要 | [原文目录](hotpot/)。每组三篇中包含两篇支持文档和一篇干扰文档，例如[文档一](hotpot/wiki-001.md)、[文档二](hotpot/wiki-002.md)、[干扰文档](hotpot/wiki-003.md)。 | [8 道题](hotpot/questions.json)，分别有 4 道桥接题和 4 道比较题，题目给出各自的支持句。 |
| CUAD 合同 1，52,252 字符 | [完整战略合作协议](cuad-1/strategic-alliance-agreement.txt)。 | [6 道主评测题](cuad-1/questions.json)和[1 道跨窗补充题](cuad-1/cross-questions.json)，按官方标注的半开字符范围计分。 |
| Qasper 论文 1、论文 2，CUAD 合同 2 | 用 [公开发行件还原命令](../../README.md#公开语料与多文档留出样本)生成，保留完整文档和题目。 | Qasper 论文 1 为 6 道主评测题和 1 道跨窗题，论文 2 为 3 道。CUAD 合同 2 有 6 道冻结题，历史实测止于准备。 |

问题、支持句和标注文件只供计分使用。构建输入只包含原文，检索输入只包含题目和必要的范围条件。

## 来源与许可

- TechQA 技术说明和 DEV 题来自 IBM TechQA 的 NVIDIA RAG 发行版。取出选定说明原文与选定 QA，按文档和题目分别保存。 [NVIDIA 发行版](https://huggingface.co/datasets/nvidia/TechQA-RAG-Eval) · [IBM TechQA](https://github.com/IBM/techqa) · [Apache 2.0](techqa/LICENSE-2.0.txt)
- HotpotQA 摘要和题来自 HotpotQA distractor 验证集。每篇以原标题保存，摘要句按发布顺序连接为 Markdown，原文章标题见题目文件的文档表。沿用 HotpotQA 的 CC BY-SA 4.0。 [HotpotQA 发行版](https://huggingface.co/datasets/hotpotqa/hotpot_qa) · [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- Qasper 论文与支持段落来自 Ai2 的 v0.3 测试归档，按发布顺序保留标题、摘要、章节和段落。 [Qasper 发行版](https://huggingface.co/datasets/allenai/qasper) · [v0.3 文件校验值](https://huggingface.co/datasets/allenai/qasper/discussions/2/files)
- CUAD 合同与官方题来自 The Atticus Project 的 CUAD v1 测试集。保存原始合同正文，另存本轮使用的题目与字符标注，沿用 CC BY 4.0。 [CUAD](https://github.com/The-Atticus-Project/cuad) · [测试集镜像](https://github.com/Break-Through-Tech/Accenture-1T-contract-review-challenge/tree/main/data) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
