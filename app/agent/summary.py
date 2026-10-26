# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：summary.py
"""层二：把溢出窗口的对话折成摘要。

为什么需要这一层：``prepare_history`` 处理超预算的做法是"从最老的丢"。丢掉的
消息永久消失——用户三小时前说过的"周五之前必须交"就此蒸发，后面再问只会得到
一句"我不记得有这件事"。摘要层在丢弃之前先把它折成结构化要点，于是历史可以
无限长，而 prompt 体积有界。

三级降级：

1. **模型摘要**：五段结构（约束 / 决定 / 纠正 / 悬置 / 实体），带引用编号；
2. **确定性摘要**：模型不可用、超时、或输出不合格时，按信息密度把原话压成一段；
3. **不折**：连压缩结果都是空的，就维持原摘要不动，本轮只做窗口裁剪。

第 2 级不是摆设。本地优先的项目里模型经常不在线（离线演示、没配 Key、熔断期），
如果摘要层强依赖模型，那层二在离线场景等于不存在，而离线恰恰是最需要"别丢上下文"
的时候。代价要讲清楚：确定性摘要只是把原话揉短，不是归纳，所以渲染时标成
"要点（自动压缩）"，不能当成结论用。
"""

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.agent.compaction import compress_text, estimate_tokens
from app.models.fact import CATEGORIES
from app.services import chat_service, fact_service, summary_service

logger = logging.getLogger(__name__)

#: 摘要的最大 token 数。400 是"够装下大概十几条要点"的经验值，
#: 再大就会挤占真正要用的历史窗口——摘要的作用是省 token，不是多记东西。
SUMMARY_MAX_TOKENS = 400

#: 单条消息折进摘要输入前先限到这么长。原文动辄上千 token，直接喂给摘要模型
#: 等于把要省的钱又花回去。
SUMMARY_INPUT_MESSAGE_TOKENS = 120

#: 一次折叠最多吃进多少 token 的对话。超出的更早部分按"已有摘要"的既有结论处理
#: （或直接放弃——它们是这次溢出的最早部分，价值最低）。
SUMMARY_INPUT_TOKEN_BUDGET = 2000

_MAX_ITEMS_PER_SECTION = 6
_MAX_ITEM_CHARS = 120
#: 一次折叠最多提几条事实。上限压得很低：事实每轮都会进 system prompt，
#: 多提一条就是永久成本，宁可少提，等它下次再被提到时补上。
_MAX_FACTS_PER_FOLD = 3

SECTION_TITLES: Tuple[Tuple[str, str], ...] = (
    ("constraints", "约束"),
    ("decisions", "已定事项"),
    ("corrections", "用户纠正"),
    ("suspended", "悬置问题"),
    ("entities", "关键实体"),
)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        **{
            key: {"type": "array", "items": {"type": "string"}}
            for key, _ in SECTION_TITLES
        },
        # 事实区（层三）的原料。放在摘要调用里顺带产出，不额外发一次请求：
        # 折叠时这段对话本来就在模型手上，单独再调一次纯属重复付费。
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "subject": {"type": "string"},
                },
                "required": ["fact"],
                "additionalProperties": False,
            },
        },
    },
    "required": [key for key, _ in SECTION_TITLES],
    "additionalProperties": False,
}

_INSTRUCTION = """你在维护一段对话的滚动摘要。输入是【已有摘要】和【新增对话片段】。

输出 JSON，五个摘要字段都是字符串数组，没有内容就给空数组：
- constraints：用户明确表达过的约束（截止时间、不能占用的时段、硬性要求）
- decisions：已经确认的决定（选了哪个方案、改成了什么）
- corrections：用户纠正过的错误理解（最容易再犯，必须留）
- suspended：提出了但还没解决的问题
- entities：反复出现的关键实体（任务名、地点、人名、行程名）

再加一个 facts 字段：从这段对话里能沉淀下来、以后还用得上的稳定信息，
每条是 {"fact": "一句话", "category": "...", "subject": "..."}，category 取
constraint / decision / entity / preference / other 之一。
subject 是这条事实在讲"哪件事"，**不含具体值**——比如 fact 是
"季度总结截止 10-29"，subject 就写"季度总结截止日期"。同一 subject 出现
新值时系统会用新值替换旧值，所以 subject 要稳定，不要带日期和数字。
只写有明确依据的（称呼、身份关系、固定安排、长期习惯），最多 3 条；
一次性的任务、临时的时间调整不要写。没有就给空数组。

规则：
1. 只写对话里明确出现过的事实，不要推断，不要补充你认为合理的设定；
2. 不写寒暄、感谢、"我可以帮你"这类空话；
3. 具体数据只留名字和关键时间，任务 id 与日程细节留给工具查询；
4. 有【已有摘要】时做增量合并，重复的只留一条；
5. 每条不超过 40 字，每段最多 6 条；
6. 条目末尾用（见 n）标注来源片段编号。"""


@dataclass
class FoldResult:
    """一次折叠的结果。"""

    #: 折叠后仍需发给模型的消息（已去掉被折进摘要的部分）。
    history: List[dict] = field(default_factory=list)
    #: 可直接拼进 system prompt 的摘要文本；没有摘要时为 None。
    summary_text: Optional[str] = None
    #: 本次折进摘要的条数。
    folded: int = 0


def fold_history(
    db: Session,
    provider,
    session_id: str,
    *,
    window_limit: int,
    keep_messages: int,
    min_fold_messages: int,
) -> FoldResult:
    """把超出保留窗口的消息折进摘要，返回折叠后的历史。

    阈值用条数而不是 token：``prepare_history`` 已经在按 token 兜底，这里如果也
    按 token 判断，两个阈值会互相掩盖——大消息先触发 token 闸门被丢掉，摘要永远
    等不到条数达标，反而更容易丢约束。条数管"折叠时机"，token 管"最终体积"。
    """

    rows = chat_service.window_messages(db, session_id, limit=window_limit)
    record = summary_service.get_summary(db, session_id)
    covered_to = record.covered_to_message_id if record is not None else 0

    # 只处理摘要还没覆盖到的消息。已经折叠过的不再重复折叠，否则同一段对话会被
    # 反复归纳，既浪费调用，也会让摘要越滚越偏。
    pending = [row for row in rows if row["id"] > covered_to]

    folded = 0
    overflow = len(pending) - keep_messages
    if overflow >= min_fold_messages:
        to_fold = pending[:overflow]
        content, source, kept_from, facts = _summarize(
            provider,
            record.content if record is not None else None,
            to_fold,
        )
        if content:
            # 摘要输入有 token 上限，装不下更早的部分时只能宣告覆盖到"实际纳入的
            # 那一段之前"。否则会骗自己说已经折过，那几条既不在摘要里也不在窗口里，
            # 下次再也不会被读到——静默丢数据比丢细节严重得多。
            covered_index = overflow - 1
            if 0 < kept_from < overflow:
                covered_index = kept_from - 1

            if covered_index >= 0:
                previous_count = record.covered_count if record is not None else 0
                record = summary_service.save_summary(
                    db,
                    session_id,
                    content=content,
                    covered_to_message_id=pending[covered_index]["id"],
                    covered_count=previous_count + covered_index + 1,
                    source=source,
                )
                pending = pending[covered_index + 1 :]
                folded = covered_index + 1

                # 事实只在摘要真正前进时写入：折叠没推进说明这批对话下次还会重来，
                # 现在记一次等于让 hit_count 虚高，候选会假性转正。
                if facts:
                    fact_service.record_facts(db, facts, session_id=session_id)

    return FoldResult(
        history=[{"role": row["role"], "content": row["content"]} for row in pending],
        summary_text=render_summary(record),
        folded=folded,
    )


def summarize(
    provider,
    previous_content: Optional[str],
    messages: List[dict],
) -> Tuple[str, str]:
    """把一批消息折成摘要，返回 ``(JSON 文本, 来源)``。

    来源为 ``model`` / ``deterministic`` / ``empty``。任一环节失败都不抛异常——
    摘要失败不能把整轮对话带崩，最差退回"没摘要"，比直接 500 好。
    """

    content, source, _, _ = _summarize(provider, previous_content, messages)
    return content, source


def _summarize(
    provider,
    previous_content: Optional[str],
    messages: List[dict],
) -> Tuple[str, str, int, List[dict]]:
    """:func:`summarize` 的内部实现，额外返回纳入下标与提取到的事实。

    ``kept_from`` 为 0 表示全部消息都进了输入；大于 0 表示前面的被 token 预算
    挡在外面，调用方不能把它们标记成"已覆盖"。``facts`` 是顺带提取的长期事实，
    确定性降级时恒为空——离线模式没有归纳能力，就不该往事实区写东西。
    """

    transcript, kept_from = _build_transcript(messages)
    if not transcript:
        return "", "empty", kept_from, []

    raw = None
    if provider is None:
        # 没有可用 Provider 时直接走确定性压缩，不打异常日志：这不是故障，
        # 是离线模式的正常路径。
        pass
    else:
        try:
            raw = provider.structured(
                _build_prompt(previous_content, transcript),
                SUMMARY_SCHEMA,
            )
        except Exception as error:  # noqa: BLE001 —— 摘要失败一律降级，不挑异常类型
            logger.warning("摘要折叠失败，改用确定性压缩：%s", error)

    parsed = _normalize(raw)
    if parsed is not None:
        sections, facts = parsed
        return (
            json.dumps(sections, ensure_ascii=False),
            "model",
            kept_from,
            facts,
        )

    digest = compress_text(transcript, SUMMARY_MAX_TOKENS, marker="…")
    if not digest.strip():
        return "", "empty", kept_from, []
    return (
        json.dumps({"digest": digest}, ensure_ascii=False),
        "deterministic",
        kept_from,
        [],
    )


def render_summary(record) -> Optional[str]:
    """把摘要记录渲染成可拼进 system prompt 的文本。"""

    if record is None or not record.content:
        return None

    try:
        data = json.loads(record.content)
    except (json.JSONDecodeError, TypeError):
        # 历史数据损坏时不要把 JSON 噪音塞进 prompt，降级成一句提示。
        logger.warning("摘要内容不是合法 JSON，按缺失处理")
        return None

    if not isinstance(data, dict):
        return None

    if data.get("digest"):
        return (
            f"【更早对话要点（自动压缩，共 {record.covered_count} 条）】\n"
            f"{data['digest']}"
        )

    lines = [
        f"【更早对话摘要（已折叠 {record.covered_count} 条）】",
        "以下是此前对话的归纳，用于理解指代和约束；涉及具体任务、日程、"
        "天气等数据时，一律以工具最新结果为准。",
    ]
    for key, title in SECTION_TITLES:
        items = data.get(key) or []
        if items:
            lines.append(f"- {title}：" + "；".join(str(item) for item in items))

    if len(lines) == 2:
        return None
    return "\n".join(lines)


# ----------------------------------------------------------------------


def _build_transcript(messages: List[dict]) -> Tuple[str, int]:
    """把消息拼成带编号的片段，从最新往回装到预算上限。

    返回 ``(文本, 实际纳入的最早一条的下标)``。编号是给模型引用用的（"见 3"）。
    真实 ``chat_messages.id`` 对模型没有意义，还会诱导它去猜数据库结构，所以这里
    用批内序号。

    装不下时**先丢最老的**：最新几轮决定"现在在聊什么"，而更早的约束应该已经在
    上一版摘要里了。但调用方必须知道丢了哪些——返回下标就是为了这个。
    """

    lines: List[str] = []
    used = 0
    total = len(messages)
    kept_from = total

    for offset in range(total - 1, -1, -1):
        message = messages[offset]
        text = compress_text(
            str(message.get("content") or ""),
            SUMMARY_INPUT_MESSAGE_TOKENS,
            marker="…",
        )
        if not text.strip():
            # 空消息不占预算，也不算"没覆盖"——里面本来就没有信息。
            kept_from = offset
            continue

        line = f"[{offset + 1}] {message.get('role', 'user')}：{text}"
        cost = estimate_tokens(line)
        if used + cost > SUMMARY_INPUT_TOKEN_BUDGET:
            break
        used += cost
        lines.append(line)
        kept_from = offset

    lines.reverse()
    return "\n".join(lines), kept_from


def _build_prompt(previous_content: Optional[str], transcript: str) -> List[dict]:
    previous = _render_previous(previous_content)
    user = f"【已有摘要】\n{previous}\n\n【新增对话片段】\n{transcript}"
    return [
        {"role": "system", "content": _INSTRUCTION},
        {"role": "user", "content": user},
    ]


def _render_previous(previous_content: Optional[str]) -> str:
    if not previous_content:
        return "（无）"
    try:
        data = json.loads(previous_content)
    except (json.JSONDecodeError, TypeError):
        return "（无）"
    if not isinstance(data, dict):
        return "（无）"
    if data.get("digest"):
        return str(data["digest"])
    lines = []
    for key, title in SECTION_TITLES:
        items = data.get(key) or []
        if items:
            lines.append(f"- {title}：" + "；".join(str(item) for item in items))
    return "\n".join(lines) or "（无）"


def _normalize(raw) -> Optional[Tuple[dict, List[dict]]]:
    """校验并裁剪模型输出，返回 ``(五段, 事实)``。

    五段和事实都为空时返回 ``None``，交由调用方降级——模型给了个空壳，说明这次
    调用没有产出，不能拿它覆盖已有摘要。
    """

    if not isinstance(raw, dict):
        return None

    normalized = {}
    for key, _ in SECTION_TITLES:
        value = raw.get(key)
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = [str(item) for item in value]
        else:
            candidates = []

        items = []
        for candidate in candidates:
            text = candidate.strip()
            if not text:
                continue
            if len(text) > _MAX_ITEM_CHARS:
                text = text[: _MAX_ITEM_CHARS - 1] + "…"
            items.append(text)

        normalized[key] = items[:_MAX_ITEMS_PER_SECTION]

    facts = _normalize_facts(raw.get("facts"))

    if not any(normalized.values()) and not facts:
        return None
    return normalized, facts


def _normalize_facts(value) -> List[dict]:
    """清洗事实数组。允许模型直接给字符串（有些模型会忽略子结构）。"""

    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    facts: List[dict] = []
    seen = set()
    for item in value[:_MAX_FACTS_PER_FOLD]:
        if isinstance(item, str):
            text, category, subject = item, "other", None
        elif isinstance(item, dict):
            text = str(item.get("fact") or "")
            category = str(item.get("category") or "other").strip().lower()
            subject = str(item.get("subject") or "").strip() or None
        else:
            continue

        text = text.strip()
        if not text or len(text) > fact_service.MAX_FACT_CHARS:
            continue
        if category not in CATEGORIES:
            category = "other"

        key = fact_service.fingerprint(text)
        if key in seen:
            continue
        seen.add(key)
        # subject 只能是"主题"而不是值本身：它用来判断两条事实说的是不是同一件事，
        # 同一主题的新值要顶掉旧值。模型没给就留空，由后端用骨架兜底。
        facts.append(
            {"fact": text, "category": category, "subject": (subject or "")[:120] or None}
        )

    return facts
