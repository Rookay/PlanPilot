# Agent 评测与执行轨迹

PlanPilot 将每次对话处理保存为一个 `AgentRun`。轨迹只记录可审计事实，不保存模型隐藏推理：

- 请求匹配到的 Skill；
- 工具预览与执行状态；
- 每次工具调用耗时；
- 用户确认、取消或等待确认；
- 执行后冲突与通勤复查。

对话页会加载当前会话最近 20 次 Run，因此刷新后仍可回看。接口为：

- `GET /api/agent/sessions/{session_id}/runs`
- `POST /api/agent/chat`

## 回归评测

“Agent 评测”页运行五个确定性端到端场景。每个场景使用独立的内存数据库，实际经过 Agent Loop、Tool Registry、Confirmation 和 postflight audit，不会改动用户数据。

当前指标：

| 指标 | 含义 |
|---|---|
| 工具选择准确率 | 必需工具出现，禁止工具未出现 |
| 工具成功率 | 预览和执行成功的工具调用占比 |
| 约束通过率 | 最终时间冲突、通勤约束及场景状态断言通过 |
| 确认安全率 | 有副作用操作要求确认，只读操作不误确认 |
| 平均耗时 | 场景端到端执行耗时 |
| 模型 Tokens / 估算成本 | Provider 返回的用量，以及按配置单价计算的估算费用 |

综合分权重为工具选择 35%、工具成功 25%、约束通过 25%、确认安全 15%。

接口：

- `GET /api/evaluations/scenarios`
- `POST /api/evaluations/run`
- `GET /api/evaluations/latest`
- `GET /api/evaluations/runs`
