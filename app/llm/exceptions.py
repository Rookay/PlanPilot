# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：exceptions.py
"""LLM Provider 异常分类。

不同错误类型在 AgentLoop 中走不同恢复路径:
- LLMAuthError         —— 配置错误,永久禁用真模型,直到进程重启。
- LLMRateLimitError    —— 上游限流,按指数退避自动重试。
- LLMTransientError    —— 服务端故障 / 超时 / 网络错误,按指数退避自动重试。

``error_kind`` 是给前端和 /api/agent/status 用的稳定字符串标签。
"""


class LLMError(Exception):
    """所有 LLM Provider 异常的基类。"""

    error_kind: str = "unknown"


class LLMAuthError(LLMError):
    """401 / 403 —— 配置错误(密钥无效、配额已用完等),不会自行恢复。"""

    error_kind = "auth"


class LLMRateLimitError(LLMError):
    """429 —— 上游限流,需要退避后重试。"""

    error_kind = "rate_limit"


class LLMTransientError(LLMError):
    """5xx / 超时 / 网络错误 —— 临时性故障,可退避后重试。"""

    error_kind = "transient"