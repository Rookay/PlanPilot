# PlanPilot Agent 架构设计

> 本文前半部分保留了最初的架构分析，便于说明设计演进；仓库当前实现状态以本节和 README 为准。

---

## 实施状态（已落地）

架构已完成可用版本，核心闭环如下：

- ✅ Planning Engine 抽取到 `app/planning/engine.py`（generate / find_free_slots / check_conflicts / evaluate_plan）
- ✅ 业务逻辑抽取到 `app/services/`（task / event / schedule）
- ✅ Tool 骨架 + 33 个工具（`app/tools/`）
- ✅ Skill 系统（`app/skills/`，第一版为提示词档案）
- ✅ Agent Loop / Context / Memory / Confirmation（`app/agent/`）
- ✅ LLM Provider 抽象 + MockProvider + OpenAICompatibleProvider（`app/llm/`）
- ✅ Agent API：`POST /api/agent/chat`（`app/api/agent.py`）
- ✅ 前端对话窗口接入 Agent，支持持久化会话、确认/取消和执行轨迹
- ✅ 用户偏好真正驱动本地工作时段、午休、间隔、周末和排序策略
- ✅ 规划批次、质量评分、安排原因、预览和整批确认
- ✅ 重复事件、任务筛选、状态联动与手动日程冲突保护
- ✅ Open-Meteo 天气工具 + 网络失败降级
- ✅ 地点搜索、地址解析、路线估算接口 + 高德地图适配器
- ✅ 标准库自动化测试覆盖核心规划与 Agent 多步流程
- ✅ 长任务拆分、任务依赖拓扑排序与循环依赖校验
- ✅ 日程锁定、指定范围局部重排、计划版本 diff 与一键撤销
- ✅ Agent 多工具确认批次的原子回滚与执行后计划审计
- ✅ 日历拖动/缩放前的冲突与相邻地点通勤校验
- ✅ 模型连接故障自动降级、代理失败直连重试与用户主动重连
- ✅ 旅行意图独立路由、逐段地图验证、批量行程预览与原子写入
- ✅ 行程组整体顺延、批量删改、交通方式切换与原子确认
- ✅ Agent Run 持久化轨迹、阶段状态与工具耗时
- ✅ 隔离数据库的端到端 Agent 评测场景、聚合指标与前端评测页

接入真实大模型见 [docs/llm-integration.md](./llm-integration.md)。

---

## 当前执行链路

```text
用户输入
  -> Context Builder 提供最小必要上下文
  -> LLM / Mock Provider 生成结构化工具调用
  -> Agent Loop 按“读取、验证、规划、执行”记录轨迹
  -> Confirmation 汇总有副作用的调用
  -> AgentDatabaseSnapshot 建立执行前快照
  -> Tool Registry 依次执行
  -> 任一步失败：恢复快照
  -> 全部成功：运行时间冲突与通勤约束审计
  -> 持久化 Agent Run，并返回自然语言结果、变更集和结构化执行轨迹
```

### 规划修改的安全边界

1. `Planning Engine` 是纯计算模块，只接收任务、忙碌区间和偏好，不读写数据库。
2. `schedule_service` 是计划修改的唯一业务入口，负责锁定规则、局部重排和版本记录。
3. 每次修改保存修改前快照，并计算 added / removed / updated 三类 diff。
4. 用户可撤销最近一个仍处于 active 状态的版本，恢复日程和任务状态。
5. Agent 的确认批次可能跨多个 Service 事务，因此使用补偿事务保证批次级原子性。
6. 修改完成后再次审计当前计划，避免工具组合产生时间或通勤冲突。

### 局部重排约束

- 已锁定日程和已确认日程作为忙碌区间保留。
- 只处理与指定范围相交的可移动日程，并可选择是否纳入尚未安排的任务。
- 相同任务的分段尽量原位更新，只有分段数量变化时才新增或删除记录。
- 若原本已安排的任务无法重新放置，整次重排失败，不提交半成品。
- 拖动单条日程时，仅验证受影响的前后相邻路段，避免被无关历史问题阻塞。

---


## 1. 初始项目架构分析（历史快照）

### 1.1 实际目录结构（以仓库为准）

```
mine/
├── run.py                     # uvicorn 启动入口（0.0.0.0:8000，reload）
├── requirements.txt           # fastapi / uvicorn / SQLAlchemy / pydantic（无 LLM SDK）
├── schedule.db                # SQLite（运行时生成/复用）
├── app/
│   ├── main.py                # FastAPI 装配、CORS、lifespan 建表、SQLAlchemy 异常处理
│   ├── database.py            # 连接串解析、engine、SessionLocal、Base、ensure_schema、check_database
│   ├── utils.py               # utcnow / to_naive_utc（统一 naive UTC）
│   ├── models/
│   │   ├── task.py            # Task
│   │   ├── event.py           # Event
│   │   ├── schedule.py        # Schedule
│   │   └── chat.py            # ChatMessage
│   ├── schemas/               # Pydantic：task / event / schedule / chat
│   ├── api/
│   │   ├── tasks.py           # Task CRUD
│   │   ├── events.py          # Event CRUD
│   │   ├── schedules.py       # Schedule + 规划算法（内嵌于此）
│   │   └── agent.py           # Agent 对话、状态与持久化会话
│   └── services/
│       └── chat_service.py    # mock 回复（后续接模型）
└── frontend/
    ├── index.html             # 单页：侧边栏 + 5 个 page
    ├── app.js                 # 状态 + fetch + 渲染（无框架）
    └── style.css
```

> 说明：任务描述里写的是 `backend/`，实际后端目录名为 `app/`。

### 1.2 初始已实现功能

| 模块 | 状态 | 说明 |
|---|---|---|
| Task CRUD | ✅ | 列表（按 status 过滤）、创建、单查、更新、删除 |
| Event CRUD | ✅（仅 API） | 前端**没有** Event 增删改 UI，只有日历只读展示 |
| Schedule | ✅ | 列表、生成、清空 proposed、PATCH 确认、删除 |
| Calendar | ✅ | 周视图（6:00–24:00），展示 Event/Schedule/Task |
| Planning | ✅ | 贪心排期算法，内嵌在 `app/api/schedules.py` |
| Chat | ✅ | 对话窗口 + `POST /api/agent/chat`；支持真实模型和本地规则模式 |
| Health | ✅ | `/health` + 数据库连通性探测 |

### 1.3 初始缺口

- 上述 Event UI、Agent、Planning Engine、冲突检查、计划评估、重规划、天气、用户偏好和 recurrence 均已实现。
- task dependencies、task location 与地图通勤属于后续可选扩展，不在当前单用户核心版本内。

---

## 2. 初始系统问题与设计依据（历史快照）

> 本节记录重构前的问题。当前代码已处理其中的解耦、状态同步、批次字段、时区、迁移和会话问题。

1. **规划引擎与 HTTP 路由耦合**：排期算法全部写在 `app/api/schedules.py` 的 `generate_schedules` 里，Agent 无法直接调用，必须经过 HTTP 自调用或复制代码。→ 后续抽取为独立 `Planning Engine`。
2. **业务逻辑集中在路由函数**：Task/Event/Schedule 的校验、级联删除等逻辑直接写在 API 层，Tool 层未来无法复用。→ 抽取到 `services/` 层。
3. **`preferred_time` 语义不明确**：`Task.preferred_time` 是 `String(20)`，到底是「HH:MM 时间段」还是自由文本没有约定，Agent 无法可靠使用。
4. **`Task.start_time` 语义过载**：既可能是「用户指定的固定开始时间」，也可能是「偏好开始时间」；规划算法把它当作 `earliest` 使用，语义模糊。
5. **Task ↔ Schedule 关系缺少约束**：结构上允许一个 Task 多条 Schedule（无唯一约束），但 `generate_schedules` 用 `scheduled_task_ids` 集合让「同一任务在一次排期里只安排一次」，且没有区分「历史 Schedule」与「当前有效 Schedule」。
6. **Schedule 缺字段**：没有 `updated_at`，没有「计划批次/版本号」，导致 replan 时无法 diff、回滚或对账。
7. **任务状态与日程状态不同步**：`complete_task` 不会自动把关联 Schedule 标记为 `completed`，反之亦然，缺少状态机。
8. **Event 无前端 UI**：Agent 后续会产出 Event，但用户当前无法在界面上管理，需要补齐前端。
9. **时区假设**：全系统统一 naive UTC，前端负责转换；对单用户可接受，但接入天气/交通等外部工具时需要「用户本地时区」上下文。
10. **`ensure_schema` 迁移能力有限**：只补 `tasks.start_time`，未来加列需要手动扩展迁移逻辑。
11. **Chat 表结构未来会变**：当前 `chat_messages` 无 `session_id`，多会话时需要改造。
12. **无鉴权/多用户**：当前是单用户假设，Agent 阶段可继续接受，但要记录。

---

## 3. 最终 Agent Architecture

### 3.1 总览

```
                       ┌──────────────────────────────────────────────┐
                       │                  User / Frontend              │
                       └──────────────────────┬───────────────────────┘
                                              │ 自然语言 / 确认
                                              ▼
                       ┌──────────────────────────────────────────────┐
                       │                    Agent                       │
                       │  ┌────────────┐ ┌─────────────┐ ┌──────────┐ │
                       │  │ Agent Loop │ │ Context     │ │ Memory   │ │
                       │  │ (决策循环) │ │ Builder     │ │ (短期+偏好)│ │
                       │  └─────┬──────┘ └──────┬──────┘ └────┬─────┘ │
                       │        │               │              │       │
                       │  ┌─────▼───────────────▼──────────────▼─────┐ │
                       │  │         Skill Registry / 选择器          │ │
                       │  └──────────────────────┬───────────────────┘ │
                       │  ┌──────────────────────▼───────────────────┐ │
                       │  │            LLM Provider (可替换)          │ │
                       │  └──────────────────────────────────────────┘ │
                       └───────────┬─────────────────────┬────────────┘
                                   │ Tool 调用            │ 结构化输出
                       ┌───────────▼──────────┐          │
                       │    Tool Registry     │◄─────────┘
                       │ (原子能力，确定性)     │
                       └───────────┬──────────┘
                                   │ 调用
                       ┌───────────▼──────────────────────────┐
                       │        Backend Services (业务逻辑)    │
                       │  tasks / events / schedules / prefs   │
                       └───────┬───────────────────┬──────────┘
                               │                   │
                  ┌────────────▼──────────┐  ┌─────▼──────────────┐
                  │   Planning Engine     │  │     Database       │
                  │ (确定性排期/冲突检查)   │  │  (SQLite / SQL)     │
                  └───────────────────────┘  └────────────────────┘
```

### 3.2 模块清单

| 模块 | 文件（建议） | 职责 |
|---|---|---|
| Agent | `app/agent/agent.py` | 总编排：持有 Loop、Provider、Registry、Context、Memory、Confirmation |
| Agent Loop | `app/agent/loop.py` | 决策循环、步数限制、工具调度、错误恢复、防循环 |
| Context Builder | `app/agent/context.py` | 按请求动态构造最小上下文（分层） |
| Memory | `app/agent/memory.py` | 短期会话 + 长期偏好 |
| Confirmation | `app/agent/confirmation.py` | 待确认变更集的管理与提交 |
| LLM Provider | `app/llm/base.py` 等 | 模型抽象、工具调用、结构化输出、解析 |
| Tool | `app/tools/*` | 原子能力（CRUD、查询、规划） |
| Tool Registry | `app/tools/registry.py` | 工具注册、schema 汇总、分发执行 |
| Skill | `app/skills/*` | 多步工作流 |
| Planning Engine | `app/planning/engine.py` | 确定性排期、冲突检查、评估（**无 LLM**） |
| Backend Service | `app/services/*` | CRUD + 业务规则 + 事务 + 校验 |

---

## 4. 职责划分

### 4.1 LLM 负责什么

- 理解自然语言意图、抽取实体（时间、任务名、时长、优先级、地点）。
- 根据 Tool schema 选择「调用哪个工具、传什么参数」。
- 观察工具结果后进行推理（结果是否可行、是否冲突）。
- 生成面向用户的自然语言解释。
- 在多个候选方案中做权衡（例如「上午 vs 下午哪个更合适」）。

### 4.2 LLM 不负责什么

- **不直接读写数据库**。
- **不执行排期算法**（不自己计算空闲槽位）。
- **不直接产生最终 Schedule 数据**（只通过 `generate_plan` 等工具间接触发）。
- **不做确定性校验**（时间冲突、deadline 是否满足由 Planning Engine 判断）。

### 4.3 Planning Engine 负责什么

- 输入：tasks、events、已有 schedules、规划参数（时间窗口、工作日时段、粒度）。
- 输出：proposed schedules、unscheduled 列表、conflicts。
- 确定性、可测试、幂等（同一输入 → 同一输出）。
- 是「可行性」的最终裁判，LLM 的推理不能绕过它。

### 4.4 Backend Service 负责什么

- 每个实体的 CRUD 与校验（字段、状态机、级联）。
- 数据库事务边界。
- 供 API 路由与 Tool 复用（**Tool 不直接碰 DB**）。

### 4.5 Tool 负责什么

- 封装一个原子、确定性操作（查询或副作用）。
- 声明自己的 name / description / input schema / output schema / 是否需要确认。
- 返回结构化 `ToolResult`（成功 / 失败 / 数据），供 LLM 观察。

### 4.6 Skill 负责什么

- 封装一个「完整问题的解决流程」（多步 Tool 调用 + 可选 LLM 子推理）。
- 编码领域知识：如「规划 = 查询上下文 → generate → evaluate → confirm」。
- 可以纯代码实现（确定流程），也可以 LLM 引导（开放流程）。

---

## 5. Tool Architecture

### 5.1 核心原则

- **原子性**：一个 Tool 只做一件事。
- **确定性**：Tool 内部无 LLM，不引入随机性。
- **声明式**：LLM 通过 name/description/schema 自动选择与传参。
- **失败可观察**：错误返回给 LLM，而不是中断整个 Agent。

### 5.2 Tool 基类 / Protocol

```python
from typing import Any, Protocol, Literal, Optional
from pydantic import BaseModel


class ToolResult(BaseModel):
    """工具执行结果，所有工具统一返回。"""
    success: bool
    data: Any = None
    error: Optional[str] = None
    # 供前端展示/确认使用的结构化摘要
    summary: Optional[str] = None


ConfirmationLevel = Literal["none", "auto", "confirm"]


class Tool(Protocol):
    name: str
    description: str            # 自然语言，说明“何时用、做什么”
    input_schema: dict          # JSON Schema（给 LLM function calling）
    output_schema: dict         # JSON Schema
    confirmation: ConfirmationLevel = "none"

    def execute(self, **kwargs: Any) -> ToolResult:
        """执行工具。不抛异常；错误封装进 ToolResult。"""
        ...
```

### 5.3 Tool metadata（供 LLM 自动选择）

每个 Tool 需要提供：

1. `name`：唯一标识，如 `create_task`。
2. `description`：自然语言，含触发条件。例如 `create_task` 的描述：「在待办列表中创建一条任务。当用户提到要做什么事情、希望安排某个任务时使用。」
3. `input_schema`：JSON Schema，定义参数名、类型、必填、约束（如 `estimated_duration` 必须是分钟整数）。
4. `output_schema`：JSON Schema，描述返回结构。
5. `confirmation`：`none`（只读）/ `auto`（自动执行）/ `confirm`（需确认）。

### 5.4 Tool Registry

```python
class ToolRegistry:
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...
    def schemas(self) -> list[dict]:
        """汇总所有工具的 function-calling schema 给 LLM。"""
    def execute(self, name: str, args: dict) -> ToolResult:
        """统一入口：校验工具存在、校验参数、捕获异常。"""
```

`execute()` 的处理逻辑：

1. 工具不存在 → `ToolResult(success=False, error="unknown tool: ...")`。
2. 参数校验失败（类型/必填/约束）→ 返回明确的参数错误。
3. 工具内部异常 → 捕获并转成 `ToolResult(success=False, error=...)`。
4. 需要确认的工具 → 返回 `pending_confirmation` 状态，暂停 Loop（见第 7 章）。

### 5.5 第一版核心 Tool 清单

| Tool | 描述 | 输入（关键字段） | 输出 | 确认 | 调用 |
|---|---|---|---|---|---|
| `get_tasks` | 查询任务，可按 status/日期/关键字过滤 | `status?`, `due_before?`, `q?` | `tasks[]` | none | services |
| `get_task` | 单查任务 | `task_id` | `task` | none | services |
| `create_task` | 创建任务 | `title`, `description?`, `estimated_duration`, `priority?`, `deadline?`, `earliest_start?`, `preferred_time?` | `task` | auto | services |
| `update_task` | 更新任务字段 | `task_id`, `...fields` | `task` | auto | services |
| `complete_task` | 完成任务（联动日程） | `task_id` | `task` | auto | services |
| `delete_task` | 删除任务（级联日程） | `task_id` | `ok` | confirm | services |
| `get_events` | 查询事件（按时间范围） | `start?`, `end?` | `events[]` | none | services |
| `get_event` | 单查事件 | `event_id` | `event` | none | services |
| `create_event` | 创建固定事件（不可移动） | `title`, `start_time`, `end_time`, `location?` | `event` | auto | services |
| `update_event` | 更新事件 | `event_id`, `...` | `event` | auto | services |
| `delete_event` | 删除事件 | `event_id` | `ok` | confirm | services |
| `get_schedules` | 查询日程（按状态/时间） | `status?`, `start?`, `end?` | `schedules[]` | none | services |
| `find_free_slots` | 查找空闲时间段 | `date`, `duration`, `work_hours?` | `slots[]` | none | Planning Engine |
| `create_schedule` | 手动创建日程 | `task_id`, `start_time`, `end_time` | `schedule` | auto | services |
| `update_schedule` | 修改日程时间/状态 | `schedule_id`, `...` | `schedule` | auto/confirm | services |
| `delete_schedule` | 删除单条日程 | `schedule_id` | `ok` | confirm | services |
| `generate_plan` | 生成计划（proposed） | `start?`, `days?`, `include_task_ids?` | `schedules[]`, `unscheduled[]` | auto(proposed) | Planning Engine |
| `check_conflicts` | 检查冲突 | `schedules[]` 或时间范围 | `conflicts[]` | none | Planning Engine |
| `evaluate_plan` | 评估计划质量/约束满足 | `plan_id?` / `schedules[]` | `score`, `issues[]` | none | Planning Engine |
| `replan` | 动态重排 | `start?`, `keep_confirmed=true`, `new_constraints` | `proposed[]`, `removed[]`, `conflicts[]` | confirm | Planning Engine |

### 5.6 External Tools

| Tool | 说明 |
|---|---|
| `get_current_time` | 获取当前时间/时区（Agent 时间锚点） |
| `get_weather` | 天气查询 |
| `get_location` | 地点/地址解析 |
| `get_route` | 路线规划 |
| `get_travel_time` | 两点间预计通行时间 |

---

## 6. Skill Architecture

### 6.1 Tool 与 Skill 的区别

| | Tool | Skill |
|---|---|---|
| 粒度 | 原子操作 | 完整工作流 |
| 组合 | 不可再分 | 由多个 Tool + 可选 LLM 子步骤组成 |
| 决策 | 无决策（确定性） | 有流程决策（先做什么后做什么） |
| 调用者 | Agent Loop / Skill | Agent Loop |

### 6.2 Skill 基类 / Protocol

```python
class SkillResult(BaseModel):
    success: bool
    response: str                  # 面向用户的话术
    proposed_changes: list = []    # 待确认的变更
    applied_changes: list = []     # 已自动应用的变更
    needs_confirmation: bool = False
    context: Any = None            # 后续 Skill / Loop 可复用的中间态


class Skill(Protocol):
    name: str
    description: str               # “何时使用本 Skill”
    input_schema: dict

    def run(self, agent: "Agent", inputs: dict) -> SkillResult:
        """执行工作流。agent 提供 llm / registry / context / memory。"""
        ...
```

### 6.3 Skill 生命周期

```
意图识别（LLM 或规则）
      ↓
选择 Skill（Skill Registry / 选择器）
      ↓
Skill.run(agent, inputs)
      ↓
  ├─ 查询上下文（Tool）
  ├─ 调用 Planning Engine（Tool）
  ├─ 评估（Tool）
  ├─ 需要时请求确认（Confirmation）
  └─ 返回 SkillResult
      ↓
Agent Loop 继续（观察结果 / 追问 / 回复用户）
```

### 6.4 第一版 Skill

1. **Task Understanding Skill**
   - 输入：自然语言。
   - 流程：LLM 结构化抽取 → 产出结构化 Task/Constraint（JSON）→ 交给后续 Skill 或直接 `create_task`。
   - 输出：`{intent, task: {...}, constraints: [...]}`。

2. **Schedule Planning Skill**
   - 输入：Task 集合 + Event 集合 + 约束 + 时间窗口。
   - 流程：`get_tasks` / `get_events` → `generate_plan` → `evaluate_plan` → 有冲突则 `check_conflicts` → 返回 proposed plan。

3. **Schedule Replanning Skill**
   - 输入：当前 Schedule + 新约束（新事件/任务变更/取消）。
   - 流程：定位受影响日程 → `replan`（保留 confirmed）→ 对比 diff → 请求确认 → 提交。

4. **Daily Planning Skill**
   - 输入：「帮我安排明天」。
   - 流程：`get_current_time` + 解析日期 → `get_tasks`(pending) → `get_events`(明天) → `get_schedules` → `generate_plan` → 汇总解释。

5. **Conflict Resolution Skill**
   - 输入：检测到的冲突。
   - 流程：`check_conflicts` → 对每个冲突枚举解决方案（移动/缩短/取消/拆任务）→ LLM 选择 → 生成 proposed 修改。

---

## 7. Agent Loop

### 7.1 目标流程

```
User Request
    ↓
Understand（LLM：意图 + 实体抽取）
    ↓
Observe Context（Context Builder 按意图取数）
    ↓
Select Skill（LLM 或规则选择）
    ↓
Call Tools（Tool Registry 执行）
    ↓
Observe Tool Results（结果回灌 LLM）
    ↓
Reason（LLM 判断是否足够 / 是否冲突）
    ↓
Call Planning Engine（generate_plan / check_conflicts）
    ↓
Evaluate Result（evaluate_plan）
    ↓
Replan if necessary（replan）
    ↓
Ask Confirmation if necessary（暂停，等用户确认）
    ↓
Commit Changes（apply 待确认变更）
    ↓
Respond to User（自然语言解释）
```

### 7.2 Loop 伪代码

```python
class AgentLoop:
    MAX_ITERATIONS = 8       # 防止无限循环
    MAX_TOOL_CALLS = 12

    def run(self, user_message: str, session_id: str) -> AgentResult:
        messages = self.context.build_messages(user_message, session_id)
        state = LoopState()

        for step in range(self.MAX_ITERATIONS):
            response = self.llm.chat(messages, tools=self.registry.schemas())

            if response.is_text():
                # LLM 认为已经完成，给出最终回复
                return self._finalize(response.text)

            tool_calls = self._validate_tool_calls(response.tool_calls)
            if tool_calls is None:
                # 非法 tool call：把错误作为 assistant 消息回传，让 LLM 修正
                messages.append(self._invalid_call_message(response))
                continue

            for call in tool_calls:
                if self._is_repeat(call, state):
                    # 相同工具+相同参数重复调用 → 视为死循环，打断
                    return self._finalize("检测到重复调用，已停止。")

                result = self.registry.execute(call.name, call.args)

                if result.needs_confirmation:
                    # 暂停：返回待确认给用户
                    return self._pause_for_confirmation(call, result)

                state.record(call, result)
                messages.append(assistant_tool_call(call))
                messages.append(tool_result_message(result))

            if state.tool_call_count >= self.MAX_TOOL_CALLS:
                return self._finalize("达到最大工具调用次数。")

        return self._finalize("达到最大推理步数，请重新描述需求。")
```

### 7.3 关键控制点

| 问题 | 处理 |
|---|---|
| 最大循环次数 | `MAX_ITERATIONS` + `MAX_TOOL_CALLS` 双上限 |
| Tool Error | 错误以 `ToolResult(success=False)` 回灌 LLM，让其换参数重试；连续失败则中止 |
| 非法 Tool Call | 校验（工具名、参数、JSON），失败时把错误信息回传 LLM 修正，且计一次迭代 |
| 无限循环 | 记录 `(tool_name, args_hash)` 调用历史；同一调用重复出现 → 强制终止 |
| 需要确认 | 暂停 Loop，返回 `confirmation_required`；用户确认后从断点继续 |
| 只读操作 | 直接执行，不确认 |
| 副作用操作 | 按确认策略（auto / confirm）处理 |

---

## 8. Context Architecture

### 8.1 原则

- **不把整个数据库发给 LLM**。
- 按意图动态取数，先取最小集合，必要时再扩展。
- 所有时间戳归一化为带时区标注的 UTC，并附带「用户本地时区」。

### 8.2 分层机制

| 层 | 内容 | 触发 |
|---|---|---|
| L0 | 无业务数据（仅 system prompt + 当前时间） | 闲聊 / 简单问答 |
| L1 | 单实体：用户提到的那个 Task/Event | 「把论文实验推迟」 |
| L2 | 时间窗实体：某天的 tasks/events/schedules | 「帮我安排明天」 |
| L3 | 扩展：偏好 + 统计 + 多天窗口 | 「安排下一周」「整体复盘」 |

### 8.3 Context Builder 示例

```python
class ContextBuilder:
    def build(self, request: AgentRequest) -> Context:
        intent = request.intent               # 由 Understand 阶段产出
        if intent == "query_task":
            return self.l1_single_task(request.entity_ids)
        if intent == "plan_day":
            return self.l2_day(request.date)
        if intent == "plan_week":
            return self.l3_week(request.date)
        return self.l0()
```

- 用户说「**帮我安排明天**」→ L2：当前时间、明天日期、pending tasks、deadlines、明天 fixed events、明天已有 schedule、工作时段偏好。
- 用户说「**把论文实验推迟到下午**」→ L1：对应 Task、该 Task 当前 Schedule、当天 Events、当天可用时间。

---

## 9. Memory Architecture

> 第一版不做复杂长期记忆。

### 9.1 短期上下文（Short-term Context）

- 当前会话的对话历史（`chat_messages` 需要增加 `session_id`）。
- 当前规划任务的中间状态（proposed plan、待确认变更）。

### 9.2 长期用户偏好（Long-term User Preferences）

- 第一版只保留**简单键值配置**，例如：

```json
{
  "work_hours": "09:00-18:00",
  "lunch_time": "12:00-13:00",
  "default_task_duration_min": 60,
  "prefer_high_priority_in_morning": true,
  "timezone": "Asia/Shanghai"
}
```

- 存储建议：新增 `user_preferences` 表（`key` / `value` / `updated_at`），或单行 JSON 配置表。
- 接口：`Memory.get_preferences()` / `Memory.set_preference(key, value)`。
- 由 Agent 在需要时「读偏好」，在用户明确表达偏好时「写偏好」（写偏好需确认）。

---

## 10. LLM Provider Architecture

### 10.1 抽象接口

```python
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] | None

class ToolCall:
    name: str
    arguments: dict

class LLMProvider(Protocol):
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...

    def structured(
        self,
        messages: list[dict],
        output_schema: dict,
    ) -> dict: ...
```

### 10.2 Provider 实现

```
LLMProvider
 ├── MockProvider        # 第一阶段：无 API Key 即可跑通整个 Agent Loop
 ├── OpenAIProvider      # 后续
 └── AnthropicProvider   # 后续
```

### 10.3 Provider 必须支持

| 能力 | 说明 |
|---|---|
| messages | 多轮对话（system / user / assistant / tool） |
| tools | function calling（工具 schema 传入、tool_call 返回） |
| tool calling | 一次响应可返回多个 tool_call |
| structured output | 按 JSON Schema 输出结构化数据（用于意图/实体抽取） |
| response parsing | 解析失败、JSON 非法、缺参时返回结构化错误 |
| error handling | 超时/限流重试，错误不崩掉 Agent Loop |

### 10.4 MockProvider 设计

- 无网络、无 Key。
- 内置一个脚本化决策器：
  - 对「理解类」请求 → 返回固定的结构化 JSON（如 `Task Understanding` 的抽取结果）。
  - 对「执行类」请求 → 返回预设的 tool_call 序列，验证 Agent Loop 能正确编排。
- 作用：让 Task Understanding、Planning、Replanning、Confirmation 全链路**在无 LLM 的情况下可测试**。

---

## 11. Confirmation Policy

### 11.1 三级确认

| 级别 | 含义 | 典型操作 |
|---|---|---|
| `none` | 只读，直接执行 | 所有 get_* / find_free_slots / check_conflicts |
| `auto` | 副作用，但低风险/可撤销，自动执行并告知 | create_task、update_task、complete_task、create_event、generate_plan（产生 proposed） |
| `confirm` | 破坏性/批量，必须用户确认 | delete_task、delete_event、delete_schedule、replan（批量改动）、confirm_all |

### 11.2 统一机制

```
Agent 执行产生副作用
        ↓
副作用归类
 ├─ none/auto  → 直接 apply，记入 applied_changes
 └─ confirm    → 暂存到 PendingChanges，生成 confirmation_request
                    ↓
              Agent 暂停，返回给用户（含 action_id + 变更预览）
                    ↓
              用户确认 / 拒绝 / 修改
                    ↓
              确认 → apply（在事务中提交）
              拒绝 → 丢弃，Agent 重新询问
```

### 11.3 数据模型（概念）

```json
{
  "action_id": "conf_123",
  "summary": "将删除 3 条日程并重新安排 2 个任务",
  "changes": [
    {"type": "delete_schedule", "target_id": 12},
    {"type": "create_schedule", "preview": {...}},
    {"type": "update_task", "target_id": 5, "fields": {"status": "completed"}}
  ]
}
```

### 11.4 决策表

| 操作 | 策略 | 理由 |
|---|---|---|
| 查询 | none | 无副作用 |
| 创建 Task | auto | 低风险、可编辑/删除 |
| 修改 Task | auto | 低风险 |
| 完成任务 | auto | 可逆（恢复） |
| 生成 Schedule（proposed） | auto | 本就是「提议」状态，未破坏已确认日程 |
| 单条 Schedule 时间调整 | auto | 影响小 |
| 删除单条 Task | confirm | 级联删除日程，破坏性 |
| 删除 Event | confirm | 可能影响既有规划 |
| 批量 replan（改多条日程） | confirm | 影响大，需要预览 diff |
| 写用户偏好 | confirm | 影响未来所有规划 |

---

## 12. Agent API Design

> 本阶段只设计，不实现。

### 12.1 主对话接口

`POST /api/agent/chat`

请求：

```json
{
  "session_id": "uuid-optional",
  "message": "帮我安排明天的论文实验",
  "confirm_action_id": null
}
```

响应（可表达多种情况）：

```json
{
  "session_id": "uuid",
  "reply": "我已经为你生成明天的计划：……",
  "tool_calls": [
    {"name": "get_tasks", "arguments": {"status": "pending"}}
  ],
  "proposed_schedules": [
    {"task_id": 3, "start_time": "...", "end_time": "...", "status": "proposed"}
  ],
  "applied_changes": [
    {"type": "create_task", "target_id": 9}
  ],
  "confirmation": {
    "required": false,
    "action_id": null,
    "summary": null,
    "changes": []
  }
}
```

当需要确认时：

```json
{
  "reply": "这次调整会删除 3 条未确认日程并重新安排 2 个任务，是否继续？",
  "confirmation": {
    "required": true,
    "action_id": "conf_123",
    "summary": "删除 3 条日程，重新安排 2 个任务",
    "changes": [...]
  }
}
```

### 12.2 确认接口

`POST /api/agent/confirm`

```json
{
  "session_id": "uuid",
  "action_id": "conf_123",
  "decision": "accept"   // accept | reject | modify
}
```

### 12.3 结构化响应字段设计

| 字段 | 作用 |
|---|---|
| `reply` | 面向用户自然语言 |
| `tool_calls` | 本次实际调用的工具（可观测/调试） |
| `proposed_schedules` | 生成的提议日程（前端高亮预览） |
| `applied_changes` | 已落库的变更 |
| `confirmation` | 待确认变更（含 action_id、summary、changes） |

> 流式输出（SSE）作为后续可选升级，第一版用一次性 JSON 响应即可。

---

## 13. 5 个完整使用场景

> 图中 LLM 承担「理解/选择/推理」，确定性计算一律交给 Planning Engine 和 Tool。

### Scenario 1：「帮我安排明天完成论文实验。」

1. User → Agent：发送消息。
2. LLM（Understand）：意图 = `plan_day`，实体 = {任务: 论文实验, 时间: 明天}。
3. Context Builder：L2 → 取明天 pending tasks、deadlines、events、schedules、当前时间。
4. Skill 选择：`Daily Planning Skill`（或先 `Task Understanding` 建任务，再 `Schedule Planning`）。
5. Tool `get_tasks` → 确认「论文实验」是否已存在；不存在则 Tool `create_task`（auto）。
6. Tool `generate_plan` → Planning Engine 计算 → 返回 proposed schedules + unscheduled。
7. Tool `evaluate_plan` → 检查 deadline/冲突 → 无冲突。
8. LLM（Reason）：汇总结果，生成解释。
9. User：收到回复 + 前端预览 proposed schedule（无需确认，因为是 proposed 状态）。

### Scenario 2：「明天下午三点到五点我要开会，帮我重新安排。」

1. LLM（Understand）：意图 = `replan`，实体 = {新事件: 开会, 15:00-17:00, 明天}。
2. Tool `create_event`（auto）→ 落库新 Event。
3. Skill：`Schedule Replanning Skill`。
4. Tool `get_schedules`（明天）+ `check_conflicts` → 找出与开会冲突的日程。
5. Tool `replan`（keep_confirmed=true）→ Planning Engine 重排冲突任务 → 返回 `proposed[]` + `removed[]`。
6. Confirmation：因为涉及**多条日程变更** → `confirm` 级别，暂停，返回 diff 预览。
7. User：确认。
8. Agent：`apply` 变更，提交事务。
9. LLM：解释调整结果。

### Scenario 3：「我今天状态不好，论文实验明天再做。」

1. LLM（Understand）：意图 = `reschedule_task`，实体 = {任务: 论文实验, 从今天移到明天}。
2. Tool `get_tasks`（q=论文实验）→ 找到 Task。
3. Tool `get_schedules`（该任务今天的日程）→ 找到待移的 Schedule。
4. Skill：`Schedule Replanning Skill`。
5. Tool `replan`（该任务推迟到明天）→ Planning Engine 找明天空闲槽。
6. Confirmation：单条日程移动 → 可 `auto`（低风险），或按策略 `confirm`。
7. Tool `update_schedule` / 提交变更。
8. LLM：告知新时间。

### Scenario 4：「明天下雨，而且我要去公司，帮我调整一下安排。」

1. LLM（Understand）：意图 = `replan`，含外部条件（天气、通勤）。
2. Tool `get_weather`（明天）→ 返回下雨。
3. Tool `get_location` / `get_travel_time`（家 → 公司）→ 返回通勤耗时。
4. Context：明天已有日程 + 新增「通勤时间」作为 busy 区间（或 Event）。
5. Skill：`Schedule Replanning Skill`。
6. Tool `replan` → Planning Engine 避开通勤时段、给户外任务降级/改室内建议。
7. Confirmation：批量变更 → `confirm`。
8. User：确认。
9. LLM：解释「因为下雨 + 通勤，我把 X 移到 Y」。

### Scenario 5：「我晚上七点和朋友吃饭，帮我安排交通和其他事情。」

1. LLM（Understand）：意图 = `plan_evening`，实体 = {事件: 吃饭, 19:00}。
2. Tool `create_event`（19:00 吃饭，auto）。
3. Tool `get_route` / `get_travel_time`（当前位置 → 餐厅）→ 计算出发时间。
4. Tool `generate_plan`（当天）→ 安排其余任务，避开 19:00 事件和交通时段。
5. Tool `evaluate_plan` → 确认无冲突。
6. Confirmation：`generate_plan` 为 proposed → 不需要确认。
7. LLM：给出「建议 18:20 出发，其余安排如下」的完整解释。

---

## 14. 后续开发阶段

| 阶段 | 内容 | 依赖 | 交付物 |
|---|---|---|---|
| **P0 现状** | 现有 CRUD + 基础 Planning | — | 已完成 |
| **P1 解耦** | 抽取 Planning Engine 到 `app/planning/`；抽取业务到 `services/`；新增 `get_current_time`；Tool 基类 + Registry；MockProvider | P0 | 可独立调用的确定性引擎 + Tool 骨架 |
| **P2 理解与任务工具** | Task Understanding Skill（LLM 结构化抽取）；Task/Event 系列 Tool 接入 services；Agent Loop 最小闭环 | P1 | 自然语言 → 结构化 Task → 落库 |
| **P3 规划与确认** | Planning Skill、Replanning Skill、Confirmation 机制、`/api/agent/chat` | P2 | 自然语言 → proposed plan → 确认 → 提交 |
| **P4 上下文与记忆** | Context Builder 分层、用户偏好存储、Daily Planning / Conflict Resolution Skill | P3 | 按需取数 + 偏好驱动的规划 |
| **P5 外部工具与真实模型** | 天气/地图/交通工具、OpenAI/Anthropic Provider、流式输出（可选） | P4 | 真实 LLM 驱动的完整 Agent |
| **P6 工程化** | 测试、日志、鉴权、前端 Event 管理 UI、迁移脚本 | P5 | 可上线版本 |

### 14.1 阶段依赖关系

```
P0 ──> P1 ──> P2 ──> P3 ──> P4 ──> P5 ──> P6
```

- P1 是**地基**：没有独立 Planning Engine 和 Tool 骨架，后续都无法开展。
- P2/P3 可以并行一部分，但 P3 的 Confirmation 依赖 P2 的 Tool 能产生副作用。
- P4 与 P5 相对独立：外部工具可以在真实 LLM 之前先用 Mock 测试。
- P6 不阻塞功能，但上线前必须完成。

---

## 15. 推荐的项目目录结构

```
mine/
├── run.py
├── requirements.txt
├── docs/
│   └── agent-architecture.md
├── app/
│   ├── main.py
│   ├── database.py
│   ├── utils.py
│   ├── models/                 # 数据模型（保持不变 + 按需少量扩展）
│   │   ├── task.py
│   │   ├── event.py
│   │   ├── schedule.py
│   │   ├── chat.py
│   │   └── preference.py       # 新增：用户偏好（P4）
│   ├── schemas/                # Pydantic（保持不变 + 按需扩展）
│   ├── api/                    # HTTP 层（变薄，只做路由绑定）
│   │   ├── tasks.py
│   │   ├── events.py
│   │   ├── schedules.py
│   │   ├── chat.py
│   │   └── agent.py            # 新增：/api/agent/chat（P3）
│   ├── services/               # 业务服务（P1 抽出）
│   │   ├── task_service.py
│   │   ├── event_service.py
│   │   ├── schedule_service.py
│   │   └── chat_service.py
│   ├── planning/               # 确定性规划引擎（P1 抽出）
│   │   ├── __init__.py
│   │   ├── engine.py           # generate_plan / check_conflicts / evaluate / replan
│   │   └── free_slots.py       # 空闲槽位查找
│   ├── agent/                  # Agent 层（独立、可替换 LLM）
│   │   ├── __init__.py
│   │   ├── agent.py
│   │   ├── loop.py
│   │   ├── context.py
│   │   ├── memory.py
│   │   └── confirmation.py
│   ├── llm/                    # LLM Provider 抽象
│   │   ├── base.py
│   │   ├── mock.py
│   │   ├── openai.py           # 后续
│   │   └── anthropic.py        # 后续
│   ├── tools/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── task_tools.py
│   │   ├── event_tools.py
│   │   ├── schedule_tools.py
│   │   ├── planning_tools.py
│   │   └── external_tools.py   # 后续
│   └── skills/
│       ├── base.py
│       ├── task_understanding.py
│       ├── schedule_planning.py
│       ├── replanning.py
│       ├── daily_planning.py
│       └── conflict_resolution.py
└── frontend/
    ├── index.html
    ├── app.js
    └── style.css
```

---

## A. 当前代码需要修改的地方

1. **`app/api/schedules.py` → `app/planning/engine.py`**：把排期算法抽成纯函数/类，API 路由只做参数解析与响应组装。
2. **`app/api/{tasks,events,schedules}.py` 的业务逻辑 → `app/services/*`**：让 Tool 复用，避免自调 HTTP。
3. **`app/models/chat.py`**：加 `session_id`（支持多会话，P4）。
4. **`app/models/task.py`**：明确 `preferred_time` 语义（建议改成明确的 `HH:MM` 或拆字段）；澄清 `start_time` 语义。
5. **`app/models/schedule.py`**：考虑加 `updated_at`、`plan_batch_id`（P3 replan 需要 diff/对账）。
6. **`app/database.py` 的 `ensure_schema`**：扩展迁移能力，支持未来新增列。
7. **`app/main.py`**：注册未来的 agent 路由（P3）。

## B. 保持不变的地方

- **Task / Event / Schedule 三个核心模型的字段语义**（除上面明确列出的澄清项）。
- **现有 CRUD API 的路径与前端调用约定**（`/api/tasks`、`/api/events`、`/api/schedules`）。
- **前端的页面结构**（dashboard/tasks/calendar/planning/chat）。
- **确定性规划算法本身**（迁移位置，不改行为）。
- **统一 naive UTC 约定**。

## C. 应该新增的功能

1. `app/planning/` 独立 Planning Engine（generate / check_conflicts / evaluate / replan）。
2. `app/services/` 业务服务层。
3. `app/tools/` Tool 基类 + Registry + 核心 Tool 集。
4. `app/skills/` Skill 基类 + 5 个 Skill。
5. `app/llm/` Provider 抽象 + MockProvider（第一阶段）。
6. `app/agent/` Agent / Loop / Context / Memory / Confirmation。
7. `/api/agent/chat` + `/api/agent/confirm`。
8. `user_preferences` 存储。
9. 前端：Event 管理 UI、Agent 对话接入、proposed/确认的预览展示。

## D. 各开发阶段的依赖关系

- **P1 必须最先做**：它是后续所有阶段的地基，且风险最低、不影响现有功能。
- **P2 依赖 P1**：需要 Tool 骨架 + services 才能实现 Task 工具和结构化抽取。
- **P3 依赖 P2**：需要「能产生副作用的工具」才有 Confirmation 意义。
- **P4 依赖 P3**：Context/Memory 服务于完整的规划闭环。
- **P5 依赖 P4**：外部工具和真实 LLM 需要完整的 Agent Loop 与上下文基础设施。
- **P6 贯穿全程**：测试应在 P1 起就同步写，不能等到最后。
