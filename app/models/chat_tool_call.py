# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：chat_tool_call.py
"""工具调用轨迹模型：``chat_tool_calls`` 表。

补的是层一（全量流水）里一直缺的一截。``chat_messages`` 只存用户和助手的
**文本**，而一次真实请求里真正承载信息量的往往是中间那几轮工具调用——模型
拿什么参数调了哪个工具、返回了什么、哪一步失败了。这些原先只活在单次请求的
局部 ``messages`` 里，请求一结束就没了。

为什么现在补上：``chat_messages`` 只存文本，导致"全量聊天记录"这个说法在架构
上是站不住的——会话里发生过工具调用，回看时却只有一句"好的，已经排好了"。
排查"模型为什么选了错工具"这类问题时，缺的正是这条轨迹。

为什么单独建表而不是往 ``chat_messages`` 里塞带 ``tool_calls`` 字段的行：
两者生命周期和读取方式都不一样。文本消息是每轮都要读进 prompt 的，工具轨迹
默认不读（它是审计与回放用的），混进同一张表会让"读窗口"这个最热的查询顺带
把大字段捞出来。

``agent_run_id`` 指向一次请求，同一次请求内的多条调用按 ``id`` 排序即执行顺序。
"""

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.database import Base
from app.utils import utcnow


class ChatToolCall(Base):
    __tablename__ = "chat_tool_calls"

    id = Column(Integer, primary_key=True, index=True)

    session_id = Column(String(64), nullable=False, index=True)

    #: 指向 agent_runs.id。落库时机在 run 记录创建之后，所以允许为空
    #: （确认执行等旁路入口没有独立 run）。
    agent_run_id = Column(String(32), nullable=True, index=True)

    #: 同一次请求内的执行序号，从 0 开始。
    turn_index = Column(Integer, nullable=False, default=0)

    #: 模型给出的调用 id，用于把 call 和 observation 配对。回放时必须成对。
    tool_call_id = Column(String(64), nullable=False, default="")

    name = Column(String(80), nullable=False, default="")

    #: 模型传入的原始参数（JSON 文本，保持原样，不重新序列化）。
    arguments = Column(Text, nullable=False, default="")

    success = Column(Boolean, nullable=False, default=False)

    summary = Column(Text, nullable=True)

    error = Column(Text, nullable=True)

    #: 返回给模型的完整 observation（超长时截断并留标记）。
    result = Column(Text, nullable=True)

    #: 未截断前的原始字符数。与 ``result`` 长度不等就说明存的是截断版。
    result_chars = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=utcnow, nullable=False)
