# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat_service.py
"""对话服务。

当前为占位实现，返回一条 mock 回复，用于先跑通「前端对话窗口 -> 后端接口」的
整条链路。后续接入真实大模型时，只需改写 :func:`generate_reply` 的内部逻辑，
例如调用 OpenAI / 通义 / 文心等模型 API，或通过 SDK 调用本地模型。

接入时请注意：
- ``history`` 形如 ``[{"role": "user", "content": "..."}, ...]``，
  已按时间顺序排列，可直接作为模型的 messages 参数。
- 如果需要流式返回，可在此基础上把接口升级为 SSE（Server-Sent Events）。
"""

from typing import List


def generate_reply(content: str, history: List[dict]) -> str:
    """根据用户消息和上下文生成助手回复（占位实现）。

    Args:
        content: 用户最新一条消息。
        history: 完整对话历史，已包含最新这条用户消息。

    Returns:
        助手回复文本。
    """

    # TODO: 在这里接入真实的大模型。
    # 示例（接入后替换 return 部分即可）：
    #   client = create_client(api_key=..., base_url=...)
    #   return client.chat(history)
    return (
        "（模型尚未接入）我已经收到你的消息：\n"
        f"「{content}」\n\n"
        "等接入大模型后，这里会返回真正的回答。"
    )
