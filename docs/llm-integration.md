# 接入真实大模型

当前 Agent 已完整实现，默认使用 `MockProvider`（无 Key、规则驱动，可离线演示）。
接入真实模型**不需要改任何 Agent / Tool / Planning 代码**，只需设置环境变量并重启。

## 1. 原理

`app/agent/factory.py` 在启动时执行：

```python
llm = from_config() or MockProvider()
```

- `config.json` 里配了 `llm.base_url`（或设置了 `LLM_BASE_URL`）→ 使用真实模型
- 未配置 → 使用 `MockProvider`（本地规则模式，可完成常见查询与规划）

`OpenAICompatibleProvider` 用标准库 `urllib` 调用 OpenAI 兼容的 `/chat/completions` 接口，
支持 **function calling（tools）** 和 **JSON 模式（response_format）**，不依赖任何 SDK。

## 2. 配置（推荐：config.json）

复制 `config.example.json` 为 `config.json`（已存在，且已被 .gitignore 排除），填入密钥：

```json
{
  "llm": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-你的密钥",
    "model": "gpt-4o-mini",
    "timeout": 30
  }
}
```

然后重启 `python run.py`。

也可以继续用环境变量（优先级高于 config.json）：

```bash
# Windows PowerShell
$env:LLM_BASE_URL = "https://api.openai.com/v1"
$env:LLM_API_KEY  = "sk-xxxxxxxx"
$env:LLM_MODEL    = "gpt-4o-mini"

python run.py
```

| 变量 | 必填 | 说明 | 示例 |
|---|---|---|---|
| `LLM_BASE_URL` | 是 | OpenAI 兼容 API 的 base URL（以 `/v1` 结尾） | `https://api.openai.com/v1` |
| `LLM_API_KEY` | 否 | API Key（本地 Ollama 等可留空） | `sk-xxx` |
| `LLM_MODEL` | 否 | 模型名，默认 `gpt-4o-mini` | `deepseek-chat` |
| `LLM_TIMEOUT` | 否 | 请求超时秒数，默认 30 | `30` |

## 3. 常见服务商配置

| 服务商 | LLM_BASE_URL | LLM_MODEL |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` / `gpt-4o` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问（DashScope 兼容） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| Moonshot Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| Ollama（本地） | `http://localhost:11434/v1` | `qwen2.5` 等已拉取的模型 |
| vLLM（本地） | `http://localhost:8000/v1` | 你的模型名 |

> 要求：模型必须支持 **function calling（tool calling）**。
> 不支持 tool calling 的模型无法驱动 Agent，只能做纯聊天。

## 4. 验证是否接入成功

启动后，在对话窗口（前端「对话助手」页）或直接调接口：

```bash
curl -X POST http://127.0.0.1:8000/api/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我安排明天"}'
```

- 响应里 `tool_calls` 出现真实工具名（如 `generate_plan`）→ 模型正在调用工具；
- `reply` 是自然语言解释；
- 页面显示「本地规则模式」→ 当前未使用真实模型；检查配置后重启进程。

## 5. 切换 / 新增其它 Provider

所有 Provider 实现 `app/llm/base.py` 里的 `LLMProvider` 接口：

```python
class LLMProvider(ABC):
    def chat(self, messages, tools=None) -> LLMResponse: ...
    def structured(self, messages, output_schema) -> dict: ...
```

- 想用 Anthropic / Gemini 等：新建 `app/llm/xxx.py` 实现该接口，
  然后在 `app/agent/factory.py` 里替换 `from_env()` 的选择逻辑即可。
- Agent 层只依赖抽象接口，换模型不影响 Task / Calendar / Planning Engine。
- 真实模型失败后，Agent 会临时进入本地规则模式 60 秒，避免连续请求反复等待超时。

## 6. 常见问题

- **模型不调用工具 / 一直闲聊**：该模型 tool calling 能力弱，换一个支持 function calling 的模型。
- **时间算错**：提醒模型「先调用 `get_current_time` 再推算」，System Prompt 已内置此规则。
- **想固定时区**：在对话里说明，或把偏好写进 `user_preferences`（`timezone` 键）。
- **密钥安全**：不要提交到 git；本地可用环境变量或 `.env` 文件（项目未引入 dotenv，需自行加载）。

## 7. 相关文件索引

| 文件 | 作用 |
|---|---|
| `app/llm/base.py` | Provider 抽象 + `LLMResponse` / `ToolCall` |
| `app/llm/mock.py` | MockProvider（无 Key 演示/测试） |
| `app/llm/openai_compat.py` | OpenAI 兼容 Provider（真实接入）+ `from_env()` |
| `app/agent/factory.py` | 通过 `from_config()` 选择 Provider 并组装 Agent 单例 |
| `app/agent/loop.py` | Agent Loop（决策 → 工具 → 观察 → 确认） |
| `app/tools/defaults.py` | 全部 28 个工具的注册 |
