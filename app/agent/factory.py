# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：factory.py
"""构建并持有全局单例 Agent。

Memory 与 PendingActionStore 是内存态，必须跨请求共享，
因此这里用一个模块级单例供 API 层复用。
"""

from app import config
from app.agent.agent import Agent
from app.agent.confirmation import PendingActionStore
from app.agent.memory import Memory
from app.llm import MockProvider, from_config
from app.skills import build_default_skills
from app.tools.defaults import build_default_registry


def build_agent() -> Agent:
    # 优先使用 config.json / 环境变量配置的真实模型；未配置时退回 MockProvider。
    llm = from_config() or MockProvider()
    return Agent(
        llm=llm,
        registry=build_default_registry(),
        memory=Memory(),
        confirmation_store=PendingActionStore(),
        skill_registry=build_default_skills(),
    )


def reconnect_agent() -> dict:
    """重新读取配置并用一次最小请求验证真实模型连接。"""

    config.reload()
    provider = from_config()
    if provider is None:
        error = "未配置大模型服务地址"
        agent.loop.replace_provider(MockProvider())
        agent.loop.mark_provider_unavailable(error)
        return {"connected": False, "error": error}

    agent.loop.replace_provider(provider)
    try:
        provider.chat(
            [
                {"role": "system", "content": "这是连接测试。"},
                {"role": "user", "content": "只回复 OK"},
            ]
        )
    except Exception as exc:  # noqa: BLE001
        agent.loop.mark_provider_unavailable(exc)
        return {"connected": False, "error": str(exc)}

    agent.loop.mark_provider_available()
    return {
        "connected": True,
        "provider": provider.__class__.__name__,
        "model": getattr(provider, "model", None),
    }


#: 全局单例（内存态会话与确认集跨请求共享）。
agent = build_agent()
