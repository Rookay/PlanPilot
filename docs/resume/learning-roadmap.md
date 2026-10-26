# AI Agent / LLM 应用开发 —— 知识补全路线图

> 定位：基于你当前的水平（南航 AI 硕二，有 PyTorch + 跨模态对比学习科研，有 PlanPilot 完整 Agent 项目），
> 对照「AI Agent / LLM 应用开发实习生」岗位要求，列出**真实缺口**与**学习路径**。
> 原则：只列你**不会**的；已经会的（Agent 可靠性、确定性引擎、评测）不重复灌。

---

## §0 你的能力画像

### ✅ 已有优势（面试时是你的主战场，别浪费在补基础上）

| 能力 | 证据 | 面试价值 |
|---|---|---|
| Agent 可靠性工程 | 异常分类 + 指数退避 + 补偿事务 + 工具白名单 + 死循环拦截 | ★★★★★ 最稀缺，实习生几乎没人做过 |
| 确定性业务逻辑与 LLM 解耦 | 规划引擎不读库不调模型，同输入同输出 | ★★★★★ 体现工程判断力 |
| Agent 评测 | 隔离 DB + 5 场景 + 5 指标 + 可视化页 | ★★★★☆ 大多数人只会看 demo |
| 约束优化 | OR-Tools CP-SAT 建模（可选区间、NoOverlap、依赖序） | ★★★★☆ |
| 端到端全栈 | FastAPI + SQLAlchemy + 原生前端，零构建部署 | ★★★☆☆ |
| 科研 | PyTorch + 跨模态对比学习 + THINGS-EEG Top-1 80.5% | ★★★★☆ |

### ❌ 三个真实缺口（按岗位 JD 权重排序）

| 缺口 | JD 出现频率 | 你的现状 | 后果 |
|---|---|---|---|
| **A. RAG / 检索增强** | 几乎所有 Agent 岗必写 | **完全没碰**。简历写"了解基本链路"= 一追问就穿 | 硬伤，必须补 |
| **B. 模型侧知识**（推理/微调/训练范式） | 中高级岗常写"了解推理优化/微调" | 只用过推理 API，没碰过训练/部署 | 中等伤，要补到"能聊" |
| **C. Agent 前沿协议与生态** | 2025 后 JD 大量出现 MCP / LangGraph | 自研 loop，没用过主流框架 | 会被问"为什么不用 LangGraph" |

---

## §1 P0 —— 面试前必补（2 周，每天 1.5–2 小时）

### P0-1 ★★★★★ RAG 全链路（**最高优先级，没有之一**）

**为什么**：你简历里"了解 RAG 检索增强生成基本链路"这七个字，是整份简历里最容易被追问击穿的地方。面试官会问："你的 chunking 怎么做的？""检索到了但答案错怎么办？""怎么评估 RAG 效果？""embedding 选型依据是什么？"——你现在一个都答不上。

**必须掌握的内容**：

| 模块 | 要学到什么 | 面试能答的深度 |
|---|---|---|
| **Embedding** | 什么维度、什么训练目标（对比学习 → 你科研正好懂）、归一化、维度 vs 效果的取舍、常见模型选型（BGE / text-embedding-3 / gte / m3e） | 能说清"我用 BGE-zh，768 维，因为中文检索场景且支持 dense+sparse 双路" |
| **Chunking** | 固定窗口 vs 重叠窗口 vs 按结构切（Markdown/代码/表格）、重叠率的影响、父子分块（small-to-big）、为什么切不好会毁掉整个 RAG | 能说出"我按 512 token + 15% 重叠切，并保留标题作为元数据" |
| **向量检索** | 暴力检索 vs 索引（HNSW / IVF / PQ）的取舍、召回率 vs 延迟、`ef_search` / `nprobe` 参数影响 | 能解释"HNSW 是近似检索，牺牲一点召回换数量级加速" |
| **混合检索** | 稀疏（BM25）+ 稠密向量，用 **RRF**（倒数排名融合）合并，**不要直接加权相加**（分数量纲不可比） | 能说清"BM25 擅长精确词，dense 擅长语义，RRF 不需要归一化就能融合" |
| **Rerank** | 为什么需要（检索是粗排，rerank 是精排）、cross-encoder vs bi-encoder、bge-reranker / Cohere rerank | 能解释"cross-encoder 能看到 query+doc 交互，所以准但慢，只对 top-50 重排" |
| **Query 改写** | HyDE（假设性答案）、多查询扩展、意图分类路由、query 分解（多跳） | 能说出"HyDE 让模型先生成一个假设答案，用它的 embedding 去检索" |
| **上下文组装** | 引用溯源、上下文压缩、过长截断的策略、去重 | 能说"我要让 LLM 知道这是检索结果不是指令，防 prompt injection" |
| **RAG 评估** | 检索侧：hit rate / recall@k / MRR / NDCG；生成侧：faithfulness（忠实度）/ answer relevancy / context precision | **这一条最值钱**，能区分"懂 RAG"和"只会调 API" |

**学会的验证标准**：
> 你能独立写一个 RAG 服务：给定一个中文技术文档集合，回答问题时能给出带引用的答案，并有一套评估脚本能输出 recall@5 和 faithfulness 分数。**能用数字说话，而不是"效果还不错"。**

**边学边做的项目**：
在 PlanPilot 里加一个「导入会议纪要 / 邮件 → 自动抽取待办」功能：
1. 文档 → chunk → embedding → 存 SQLite（用 `sqlite-vec` 或 `faiss-cpu`，不引重服务）
2. 用户问"上周组会说了什么" → 混合检索 → rerank → LLM 生成带引用的回答
3. 抽取出的待办 → 走**你已有的 `confirm` 级工具**进 `tasks` 表
4. 评估：构造 20 个问答对，算 recall@5

这个功能做完，你的简历上"RAG"就从"了解"变成"做过"，而且**和 Agent 主线串起来了**（RAG 抽取 → 工具 → 确认 → 排期）。

**资源**：
- DeepLearning.AI《Building Applications with Vector Databases》（免费，1 小时）
- DeepLearning.AI《Advanced RAG with LlamaIndex》（免费，讲 chunking/重排/评估）
- 《从 LLM 到大模型应用开发》（实际书名《LLM 应用开发与工程化》相关章节）/ 极客时间 RAG 系列
- 论文：**RAG**(Lewis 2020)、**RRF**(Cormack 2009)、**HyDE**(Gao 2022)、**ColBERT**(Khattab 2020)、**Self-RAG**(Asai 2023)

---

### P0-2 ★★★★☆ 大模型推理与部署基础

**为什么**：你项目里的 `openai_compat.py` 只做了"调用推理 API"。Agent 岗越来越要求懂**成本和延迟**——面试官问"你的 Agent 一次对话多少钱？""怎么控 token 成本？""流式怎么做？"，你目前答不上。

**必须掌握**：

| 主题 | 内容 | 面试话术 |
|---|---|---|
| **推理参数** | temperature / top_p / max_tokens / stop / seed 的实际影响，哪些能设 seed 让输出可复现 | "我做过排期引擎要求可复现，但 LLM 层我做的是**约束输出 + 结果校验**，不是靠 seed" |
| **KV Cache** | 原理（复用 K/V 避免重算）、prefix caching、**prompt caching 的计费差异**（Anthropic/OpenAI 都对相同前缀打折） | "我的 system prompt 50 行是固定前缀，用 prompt caching 能省 60% 输入成本" |
| **Continuous Batching** | 为什么要有、vLLM 怎么做、和静态 batch 的区别 | "vLLM 的 continuous batching 让请求可以插队，这是它比裸 transformers 快的关键" |
| **量化** | INT8 / FP8 / AWQ / GPTQ 的原理与精度损失、显存换算 | "7B 模型 FP16 要 14GB，INT4 量化后能压到 4GB 左右" |
| **推理服务** | vLLM / SGLang / Ollama 的定位、OpenAI 兼容端点、自建 vs 云 API 的成本对比 | "本地开发用 Ollama，生产用云 API 或 vLLM 自建" |
| **流式输出** | SSE 协议、增量 tool_call 的拼接、为什么流式下 tool_call 参数要累积 | "我在自己的项目里就踩过流式 tool_call 参数分片的问题" |
| **成本控制** | token 预算管理、上下文裁剪（你已做）、**模型路由**（简单问题走小模型）、缓存 | "我在 AgentLoop 里做了三级工具预算，本质是 token 预算的细粒度化" |

**学会的验证标准**：
> 能给你项目的 Agent 算一笔账：平均一次对话多少 input/output token、多少钱；如果用 prompt caching 能省多少；如果要压到 1/10 成本，从哪下手。

**资源**：
- vLLM 官方文档的 "PagedAttention" 和 "Continuous Batching" 两篇
- Anthropic Engineering Blog：*Prompt Caching*、*Effective Context Engineering for AI Agents*
- 《LLM Inference Handbook》相关章节

---

### P0-3 ★★★★☆ 主流 Agent 框架生态（**不是为了用，是为了能答"为什么不用"**）

**为什么**：面试官几乎一定会问"你为什么不用 LangChain / LangGraph？"。你现在只能说"我看过但没用过"，这在竞争力上很弱。**用过的和了解取舍的，差距巨大。**

**必须做到**：
1. **亲手跑通** LangGraph 的最小例子（一个带条件分支 + 循环 + 人工中断的 state graph）
2. **亲手跑通** LlamaIndex 的 RAG pipeline
3. **能对比**：你的自研 loop vs LangGraph vs LlamaIndex
4. **知道 MCP**（下一节）

**你要准备的答案框架**（这是重点）：

> "我项目初期评估过 LangChain，结论是没有用，理由有三：
> 1. **控制精度**：我需要在循环层精确控制迭代预算、逐工具调用上限、参数级重复检测、Observation 压缩策略。框架的抽象（AgentExecutor 的 max_iterations 是一个粗粒度开关）不够我拆。
> 2. **依赖体积**：我的目标是 5 个依赖，LangChain 生态会引入 20+ 个。
> 3. **学习价值**：自己写一遍才真正理解 function-calling 协议的细节和失败模式。
>
> 但我后来用 LangGraph 补做了一遍同样的场景，发现它有两个东西我做得好：**显式的状态管理**（我的 `state` 对象是全局的，多 Agent 分支时状态传递会很乱）和**可中断恢复**（human-in-the-loop 的中断点管理）。
>
> 所以我现在的判断是：**自研适合核心链路，框架适合快速验证和周边能力。** 如果重来我会在 RAG 那一层用 LlamaIndex，Agent 核心仍然自研。"

**资源**：
- LangGraph 官方文档的 "Graph API" 和 "Persistence" 两章
- LangChain 表达式语言（LCEL）—— 理解"链式组合"和"Agent 循环"的抽象差异
- LlamaIndex《Embedding Overview》《Advanced Retrieval Strategies》

---

### P0-4 ★★★☆☆ MCP（Model Context Protocol）

**为什么**：2025 之后 Agent 岗 JD 里 MCP 出现率极高。成本低、收益高 —— **一个下午能入门，两周能吃透**。

**必须掌握**：
- **协议结构**：Server 暴露 Tools / Resources / Prompts；Client 连接 Server
- **传输方式**：stdio（本地子进程）、SSE / Streamable HTTP（远程）
- **能力协商**：`initialize` 握手里协商协议版本与能力
- **和 Function Calling 的关系**：MCP 是"工具怎么被标准化暴露"，Function Calling 是"模型怎么表达要用哪个工具"。**两者正交**
- **生态**：MCP Server 的官方 registry、社区 server（filesystem / git / postgres / fetch）

**和你的项目怎么接**（这个答案会加分）：
> "我的 37 个工具是进程内 Python 函数，套 MCP 会引入 JSON-RPC 往返，反而更慢。MCP 真正适合我的是 `app/integrations/` 这一层——把高德、Open-Meteo 这类外部服务做成 MCP Server，或者未来接公司内部 API。**核心链路自研 + 外部能力走标准协议**，这是我认为的合理分层。"

**资源**：
- modelcontextprotocol.io 官方文档（很短）
- MCP Python SDK 官方仓库的 examples 目录
- 一个现成的第三方 MCP Server 源码（读一遍 server 端实现）

---

## §2 P1 —— 岗位高频，1–2 个月内补

### P1-1 ★★★★☆ 检索与排序基础（补 RAG 的数学底子）

- **BM25**：词频饱和、逆文档频率、为什么它比 TF-IDF 稳
- **稀疏向量**：SPLADE / BGE-M3 的 learned sparse，学习稀疏与稠密的互补
- **ANN 索引**：HNSW 的分层图、IVF 的聚类、PQ 的乘积量化
- **查询路由**：什么时候该用 dense、什么时候必须用 keyword（产品名、错误码、人名）
- **知识库工程**：文档清洗、表格/图片处理、增量更新、去重

### P1-2 ★★★★☆ 模型微调与训练范式（**从"用模型"到"懂模型"**）

- **训练三阶段**：预训练 → SFT（监督微调）→ 对齐（RLHF / DPO）
- **PEFT / LoRA**：低秩矩阵、rank / alpha 的影响、**为什么大部分场景 LoRA 够用而不用全参**
- **QLoRA**：4bit 量化底座 + LoRA，显存再降
- **数据比模型重要**：SFT 数据构造、指令数据清洗、去重与污染、**最难的是造高质量负例**
- **偏好对齐**：DPO 相对 RLHF 的简化（不需要训 reward model，直接偏好对）
- **RLVR / GRPO**：可验证奖励（数学答案、代码能否通过测试）—— Agent 训练的新范式，2025 热点
- **Agent 场景的微调要点**：SFT 教"格式和工具选择"，RL 教"多步决策的正确性"

**面试话术**：
> "我做过对比学习训练，知道**数据质量的天花板效应**——这在 SFT 里同样成立。我的判断是：90% 的 Agent 效果提升来自更好的 prompt 和更好的工具设计，而不是微调。微调适合解决'输出格式不稳定'和'领域术语不懂'这两类问题。所以如果让我给一个团队建议，我会先做评测和工具设计，最后才考虑微调。"

### P1-3 ★★★☆☆ Agent 记忆与上下文工程

- **记忆分层**：短期（工作记忆）/ 长期（语义记忆）/ 情景记忆 / 程序记忆
- **MemGPT / Letta 的思路**：把上下文当"有限资源"，主动换出到外部存储
- **上下文工程 vs prompt engineering**：不只是写好 prompt，是**决定每次调用放什么进去**
- **长上下文的"中间遗忘"**（Lost in the Middle 论文）：关键信息放首尾
- **上下文压缩策略**：摘要、抽取、选择性保留（你已经做了一部分）
- **你的项目怎么接**：短期用 Redis/内存，长期存向量库或关系表

### P1-4 ★★★☆☆ Agent 评测方法论（**你的强项，往深了走就是差异化**）

- **结果评测 vs 轨迹评测**：只看最终答案 vs 评估中间每一步工具选择
- **LLM-as-a-Judge**：怎么做才可信（多模型投票、rubric、pairwise 优于单点打分）
- **pass^k**：连续 k 次都对的概率，比 pass@k 更能反映稳定性
- **公开基准**：SWE-bench、GAIA、AgentBench、WebArena、τ-bench
- **回归测试化**：把 Agent 评测当单元测试，每次改 prompt 自动跑

### P1-5 ★★★☆☆ 工程化与安全

- **异步与并发**：asyncio、FastAPI async endpoint、连接池
- **缓存**：Redis、多级缓存、**prompt cache**（和 LLM 侧的 KV cache 区分开）
- **限流与熔断**：令牌桶、指数退避（你已做 LLM 侧）、Sentinel/resilience4j 思想
- **可观测性**：OpenTelemetry、结构化日志、trace（你的 `agent_runs` 就是雏形，可以升级成标准 trace）
- **Prompt Injection 防御**：这是 Agent 岗的高频安全题
  - 不可信内容（网页、文档、用户输入）与指令隔离
  - 工具权限最小化、路径白名单、命令沙箱
  - 输出侧做敏感信息过滤
- **多租户与鉴权**：你的项目是单用户本地工具，进生产必须补（RBAC、数据隔离、审计日志）

---

## §3 P2 —— 进阶，看方向和团队

| 方向 | 内容 | 什么时候需要 |
|---|---|---|
| **多智能体** | AutoGen / LangGraph 多 agent、角色分工、通信协议、**什么时候不该用多智能体** | 团队在做多 agent 产品 |
| **Computer Use / GUI Agent** | 截图理解、坐标定位、动作循环（Anthropic computer use、OpenAI CUA） | 做 RPA / 自动化 |
| **模型压缩与小型化** | 蒸馏、剪枝、量化、MoE | 边缘部署 / 成本敏感 |
| **Agentic RAG** | 自反思检索、迭代检索、查询路由树、GraphRAG | 做知识库问答 |
| **代码 Agent** | Repo 级理解、工具（编辑/运行/测试）、SWE-bench 优化 | 做编程助手 |
| **多模态 Agent** | 视觉理解 + 工具调用（这正好接你的 EEG/视觉背景） | 差异化方向 |
| **强化学习 for LLM** | PPO/DPO/GRPO/RLVR、reward hacking、过程奖励 PRM | 做模型侧研发 |
| **推理加速** | speculative decoding、Medusa、EAGLE、算子融合 | 做推理基础设施 |

---

## §4 结合 PlanPilot 的实战补强（**学以致用，比看课强 10 倍**）

这是我最推荐的方式：**每学一块，就往项目里加一个真实功能**。既学得会，又有简历可写，还能顺手补掉你项目现在缺的能力维度。

| 学什么 | 在 PlanPilot 加什么 | 补掉的能力缺口 |
|---|---|---|
| **RAG 全链路** | 「导入邮件/会议纪要 → 抽取待办任务」：文档切分 → embedding → 混合检索 → rerank → LLM 抽取 → 走已有 confirm 工具进 tasks 表 | RAG、检索、评估 |
| **MCP** | 把 `app/integrations/` 的地图/天气封成 MCP Server，另写一个 MCP Client 接入 | MCP、协议、集成 |
| **流式输出** | 聊天窗口改成 SSE 流式，前端用 `EventSource` 接收，tool_call 增量拼接 | 前端实时性、协议细节 |
| **异步** | 把 `/api/agent/chat` 改成 async endpoint，LLM 调用用 `httpx.AsyncClient` + asyncio 并发调多个工具 | asyncio、性能 |
| **成本控制** | 加 token 预算 + 模型路由（简单意图走小模型）+ 响应缓存 | 成本意识、模型工程 |
| **可观测性** | 把 `agent_runs` 升级成标准 OpenTelemetry trace，接一个可视化后端 | 生产级可观测 |
| **Agent 评测** | 把评测场景从 5 个扩到 15+ 个，加 LLM-as-judge 打分 | 评测深度（你的强项） |
| **多租户/鉴权** | 加用户表、session 归属校验、API key | 生产化准备（面试讲"我知道缺什么"） |
| **RAG 评估** | 写 `scripts/eval_rag.py`，输出 recall@5 / faithfulness / 成本 | 量化能力 |

**节奏建议**：每 1–2 周加一个功能，每个功能一个 commit，commit message 写清"学了什么 + 为什么这么设计"。到面试时你讲的不再是"我看过"，而是"我实现过并评估过"。

---

## §5 资源清单

### 中文字幕 / 中文优先
- **《动手学深度学习》**（d2l.ai）—— 补深度学习基础，特别是 Transformer 章节
- **李沐的《动手学深度学习》B 站视频** —— 论文精读系列（Attention Is All You Need / BERT / GPT 系列）
- **《LLM 应用开发与工程化》**类中文技术书 —— RAG / Agent 工程部分
- **B 站**：跟读 LangChain / LlamaIndex / vLLM 实战
- **Hugging Face 中文社区**（hf.co/docs/zh）

### 英文一手资料（技术岗必须读原文）
- **Anthropic Engineering Blog** —— *Building Effective Agents*、*Effective Context Engineering*、*Writing Tools for Agents*、*Prompt Caching*
  > ⭐ **如果只读四篇，就读这四篇。** "Building Effective Agents" 那篇把 workflow vs agent 的边界讲得最清楚，是我见过最好的 Agent 工程入门。
- **Hugging Face smol-course / LLMs Course** —— 最好的动手 LLM 入门
- **DeepLearning.AI 短课**（免费，每门 1–2 小时）——
  - Building Applications with Vector Databases
  - Advanced RAG with LlamaIndex
  - AI Agents in LangGraph
  - Quality and Safety for LLM Applications
  - Evaluating and Debugging Generative AI
- **Chip Huyen《AI Engineering》**（2025 新书）—— 讲工程落地不讲模型
- **《Designing Machine Learning Systems》**—— 系统设计的思维（ML 侧通用）

### 论文（挑 10 篇读就够）
| 方向 | 论文 |
|---|---|
| Agent 范式 | **ReAct**、**Reflexion**、**Toolformer**、**SWE-bench** |
| 规划 | **Tree of Thoughts**、**Plan-and-Solve**、**Graph of Thoughts** |
| RAG | **RAG**、**RRF**、**HyDE**、**ColBERT**、**Self-RAG**、**Lost in the Middle** |
| 记忆 | **MemGPT**、**Generative Agents** |
| 训练 | **LoRA**、**DPO**、**GRPO/RLVR**、**Self-Instruct** |
| 评测 | **AgentBench**、**GAIA**、**τ-bench** |

### 工具/框架（按需上手）
- **vLLM** —— 本地跑开源模型 + 看推理优化实现
- **LangGraph** —— 学状态图与 human-in-the-loop
- **LlamaIndex** —— 学 RAG 的工程抽象
- **sentence-transformers** + **FlagEmbedding（BGE）** —— embedding 与 rerank
- **Unstructured / Docling** —— 文档解析（PDF/表格/图片）
- **Ragas / DeepEval** —— RAG 与 LLM 评估
- **Promptfoo** —— prompt 回归测试

---

## §6 90 天路线图

### 第 1–2 周：补最大的洞（RAG）
- Day 1–3：Embedding / chunking 原理 + 跑通 DeepLearning.AI 向量库课
- Day 4–7：混合检索 + RRF + rerank；读完 RAG / RRF / HyDE 三篇论文
- Day 8–10：**在 PlanPilot 里实现「邮件导入抽待办」**
- Day 11–14：写 RAG 评估脚本（recall@5 / faithfulness）；读 Anthropic《Building Effective Agents》

### 第 3–4 周：Agent 生态 + 推理基础
- LangGraph 最小例子 + 状态图/持久化（1 天）
- LlamaIndex RAG pipeline（1 天）
- MCP 官方文档 + 跑通 2 个第三方 Server + 读 server 端源码（2 天）
- vLLM 文档（PagedAttention / Continuous Batching / 量化）+ 本地跑一个 7B（3 天）
- 读 Anthropic 4 篇工程博客（3 天）
- **在 PlanPilot 加：MCP 集成 + 流式输出 + 异步化**（本周）

### 第 5–8 周：模型侧 + 工程化
- 读 LoRA / DPO / GRPO 三篇；用 PEFT 跑一次 LoRA 微调（3 天）
- 读 Chip Huyen《AI Engineering》重点章节（3 天）
- OpenTelemetry 接入 + Agent 评测扩到 15 场景 + LLM-as-judge（1 周）
- 读安全相关：OWASP LLM Top 10、prompt injection 防御（2 天）
- **在 PlanPilot 加：token 预算 + 模型路由 + 追踪**

### 第 9–12 周：补全与作品化
- 依目标团队方向选 P2 专题（多智能体 / Computer Use / 多模态 Agent）
- 写一份**技术博客**：《为什么我用自研 Agent loop 而不是 LangGraph》《LLM 直接排期 vs 确定性引擎的 A/B 结果》
  > ⭐ 博客是简历之外最好的证明。GitHub 仓库有 README + 评测数据 + 博客，面试官自己会去看。
- 整理简历：把新加的功能、新的数字补进去
- 模拟面试（对着我的题库过一遍）

---

## §7 学完之后怎么验证（**别学完就忘**）

每学一块，用这三个问题检验自己：

1. **能不能讲清"为什么这样做"和"另一种做法为什么不行"？**
   > 只知道"这么做"= 会用；能说清"为什么不那样"= 懂了。

2. **能不能在 PlanPilot 里指出对应代码 / 说出"如果用新学到的东西，我会怎么改"？**
   > 这一条最狠。**面试时能把新知识挂到已有项目上，你的可信度会瞬间拉满。**

3. **能不能指出自己方案的边界和失败模式？**
   > 我在 §7 里给的每个回答都带"我的判断/我的短板/我会怎么做"。这不是谦虚，是**工程成熟度的直接证据**。

---

## §8 最后：不要学的东西（避免浪费时间）

| 别学 | 原因 |
|---|---|
| **从头训一个 LLM** | 你已经有 PyTorch 基础，知道流程就够了。实际工作里没人从头训 |
| **大量框架的 API 细节** | 框架会变，**设计思想不变**。学思想，别背 API |
| **CUDA kernel 手写** | 除非投推理 infra 岗，否则 ROI 极低 |
| **花哨的 Agent 框架全家桶** | 深度不如 1 个框架 + 1 次自研 |
| **模型结构魔改** | 除非做研究，否则不如把评测和工具设计做扎实 |
| **前端框架** | 你的零构建前端够用，别在这上面花时间（除非面全栈岗且要求 React） |

---

## §9 一句话总结

> **你的技术画像已经比 90% 的 Agent 岗候选人硬了**——你有真实的可靠性工程和确定性引擎，这是"能上线"和"能演示"的分界线。
>
> **接下来要补的只有两块**：
> 1. **RAG 全链路**（2 周，权重最大，直接堵住简历漏洞）
> 2. **模型侧认知**（推理成本 + 微调范式，能聊就行，不必动手深挖）
>
> 剩下的时间**全部投到"在 PlanPilot 里加真实功能"**上。学一个东西、落一个功能、留一个 commit、写一篇博客——这是从"看过"变成"做过"的唯一路径。
