# PlanPilot

PlanPilot 是一个本地优先的智能任务与日程规划助手。它把大模型用于理解需求和选择工具，把时间冲突、空闲时段、任务排序等关键计算交给确定性规划引擎，因此即使模型暂时不可用，核心任务管理和自动排期仍可使用。

## 已完成功能

- 任务：创建、编辑、完成、取消、删除、搜索、筛选和排序
- 固定事件：单次或按天、工作日、每周重复创建
- 日历：任务、事件、待确认日程和已确认日程的周视图
- 智能规划：本地工作时段、午休、周末开关、任务间隔和偏好时段
- 三种策略：均衡安排、截止优先、优先级优先
- 计划分析：质量评分、已安排时长、未安排原因和安排依据
- 计划工作流：预览、生成、单条确认、整批确认、清空和重规划
- Agent：任务、事件、日程、偏好、冲突、空闲时间、天气和地图工具
- 对话可靠性：持久化会话、操作确认、重复调用保护、步数保护和离线降级
- 大模型：兼容 OpenAI、DeepSeek、通义千问、智谱、Kimi、Ollama 和 vLLM 的 OpenAI 风格接口
- 天气：基于 [Open-Meteo](https://open-meteo.com/) 的城市解析和未来天气查询，无需个人 API Key
- 地图接口：地点搜索、地址解析以及步行/驾车/公交路线估算；填入高德 Web 服务 Key 后启用
- 常用地点：保存家、学校、公司等精确坐标，管理默认出发点，并向 Agent 提供位置上下文
- 一日行程地图：按日期展示时间轴、编号地点、真实路线、建议出发时间和通勤冲突
- 统一行程表达：固定行程表示不可移动的外部约束，任务安排表示规划器为任务生成的可调整时间块
- 对话格式：安全渲染 Markdown 表格、列表、标题、粗体和行内代码

## 结构

```text
frontend -> FastAPI routes -> services -> SQLAlchemy / planning engine
                    |
                    +-> Agent loop -> skills -> tools -> services
                    |
                    +-> OpenAI-compatible provider / local rule fallback
```

规划引擎不访问数据库、不调用模型，同一输入会得到同一结果。所有数据库时间统一保存为不带时区标记的 UTC，界面负责按用户本地时区显示。

## 运行

项目当前使用的解释器：

```powershell
& "G:\Anaconda\envs\agent\python.exe" run.py
```

打开 `http://127.0.0.1:8000/frontend/`。API 文档位于 `http://127.0.0.1:8000/docs`。

默认不会开启热重载。开发时可先设置 `$env:RELOAD="true"`。数据库默认位于项目根目录的 `schedule.db`，也可通过 `DB_PATH` 或 `DATABASE_URL` 修改。

## 配置大模型

复制 `config.example.json` 为 `config.json`，填写服务地址、密钥和模型名。`config.json` 已被 Git 忽略，不会提交密钥。

```json
{
  "llm": {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "你的密钥",
    "model": "deepseek-chat",
    "timeout": 30
  }
}
```

未配置模型时，系统自动使用本地规则模式。真实模型连接失败后会进入 60 秒冷却期，避免每条消息都等待网络超时。详细配置见 [大模型接入说明](docs/llm-integration.md)。

## 配置地图

地图能力默认关闭且不会主动联网。在 `config.json` 的 `external.maps.api_key` 中填入高德开放平台的 Web 服务 Key 后即可使用，也可设置环境变量 `MAPS_API_KEY`。配置示例、接口参数和扩展方式见 [地图接口接入说明](docs/map-integration.md)。

## 测试

测试只使用 Python 标准库 `unittest` 和项目已有依赖，不会启动 Web 服务：

```powershell
& "G:\Anaconda\envs\agent\python.exe" -m unittest discover -s tests -v
```

当前测试覆盖规划优先级与间隔、偏好时段、UTC/本地时间换算、午休避让、计划批次和确认、任务状态联动、重复事件，以及“论文比较急，尽快安排，其余的安排到后面，留出空闲”的离线 Agent 多步流程。

## 数据与安全

- 本项目按单用户本地工具设计，默认未提供账号和多租户隔离，不建议直接暴露到公网。
- 删除类 Agent 操作和长期偏好修改需要确认，确认请求会在 10 分钟后失效。
- `schedule.db`、`config.json` 和 `.env` 均不会进入 Git。
- 天气查询会把城市名发送给 Open-Meteo；不需要此能力时无需额外配置，也不会主动调用。
- 地图功能仅在配置 Key 且实际调用地点或路线能力时访问所选地图服务商。

## 进一步扩展

核心版本已经闭环。面向多人或公开部署时，后续工作应集中在账号鉴权、正式迁移工具、PostgreSQL、多实例共享会话，以及把通勤耗时正式纳入自动排期约束；这些属于部署形态扩展，不影响当前单用户版本使用。
