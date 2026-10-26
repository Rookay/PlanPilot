# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：compaction.py
"""上下文压缩：token 估算、语义化文本压缩、工具结果瘦身。

三条设计原则：

1. **按 token 说话，不按字符。** 中文一字约一 token，ASCII 约 3.5 字符一 token，
   同一段字符预算在两种内容上能差出三倍。用字符阈值做裁剪，等于在 JSON 上放宽、
   在中文上收紧，两头都不准。
2. **按信息密度取舍，不按位置。** 首尾截断会把任务清单的中间整段丢掉，而排期恰恰
   依赖中间那些条目。这里给每一段打分，约束词、时间、ID、数字加分，再贪心填充。
3. **压缩必须留痕。** 丢掉的内容要给出数量和提示，让模型知道有缺口、可以重新查询，
   而不是以为眼前看到的就是全部。
"""

import json
import math
import re
from typing import Any, List, Optional, Tuple

#: 中文字符近似按 1 字 1 token 计（略保守，宁可多算）。
_CJK_TOKEN_RATIO = 1.0
#: 非中文按 3.5 字符 1 token 计。
_ASCII_CHARS_PER_TOKEN = 3.5
#: 统一安全系数，避免估算偏低导致实际超预算。
_SAFETY_RATIO = 1.1

_CJK_RANGES = (
    (0x2E80, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)

_CONSTRAINT_RE = re.compile(
    r"必须|不能|不得|只能|截止|锁定|之前|不要|禁止|需在|至少|至多"
    r"|deadline|required|must not|cannot",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}|UTC[+-]?\d*|周[一二三四五六日天]|上午|下午|晚上|今天|明天"
)
_ID_RE = re.compile(r"#\d+|\bid[=:\s]|\bID\b")
_DIGIT_RE = re.compile(r"\d")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])")

#: 列表类字段的保留策略：头部条数 / 尾部条数。
_LIST_HEAD = 12
_LIST_TAIL = 4
_STEPS_HEAD = 6
_STEPS_TAIL = 2
#: 单条消息的角色/分隔固定开销，估算与预算裁剪都要用它，否则口径会差几条 token。
_MESSAGE_OVERHEAD_TOKENS = 4
#: 单个字符串字段超过这个字符数就做语义压缩。
_LONG_STRING_CHARS = 1200
_LONG_STRING_TOKENS = 350


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。空文本返回 0。

    不引入 tiktoken：它是按模型分词的，跟具体厂商绑定，而且给项目凭空加一个
    几十 MB 的依赖。对"预算控制"这个用途来说，10% 以内的误差完全可以接受。
    """

    if not text:
        return 0

    cjk = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
    other = len(text) - cjk

    raw = cjk * _CJK_TOKEN_RATIO + other / _ASCII_CHARS_PER_TOKEN
    return max(1, math.ceil(raw * _SAFETY_RATIO))


def estimate_message_tokens(message: dict) -> int:
    """估算单条消息的 token，含角色和分隔开销。"""

    total = _MESSAGE_OVERHEAD_TOKENS
    total += estimate_tokens(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        total += estimate_tokens(json.dumps(call, ensure_ascii=False, default=str))
    return total


def estimate_messages_tokens(messages: List[dict]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def _split_segments(text: str) -> List[str]:
    """按行切；只有一行时退化为按句切。"""

    if "\n" in text:
        return text.split("\n")
    return [segment for segment in _SENTENCE_SPLIT_RE.split(text) if segment]


def _score_segment(segment: str) -> int:
    """信息密度打分：约束词 > 时间 > ID / 数字；过长和过短都降权。"""

    stripped = segment.strip()
    if not stripped:
        return -5

    score = 0
    if _CONSTRAINT_RE.search(stripped):
        score += 3
    if _TIME_RE.search(stripped):
        score += 2
    if _ID_RE.search(stripped):
        score += 1
    if _DIGIT_RE.search(stripped):
        score += 1
    if len(stripped) < 8:
        score -= 1
    score -= min(2, len(stripped) // 500)
    return score


def _truncate_middle(text: str, max_tokens: int, marker: str) -> str:
    """无结构文本的兜底：保头保尾，用二分找能塞进去的最大长度。"""

    cut = f"...[{marker}]..."
    if estimate_tokens(cut) >= max_tokens:
        return cut

    best = cut
    low, high = 0, len(text)
    while low <= high:
        middle = (low + high) // 2
        tail = text[-min(middle // 4, 400):] if middle else ""
        candidate = text[:middle] + cut + tail
        if estimate_tokens(candidate) <= max_tokens:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _assemble(segments: List[str], keep: set, marker: str, joiner: str) -> str:
    """按原顺序拼接保留段，跳过的连续段落折成一条省略标记。"""

    pieces: List[str] = []
    skipped = 0
    for index, segment in enumerate(segments):
        if index in keep:
            if skipped:
                pieces.append(f"...[{skipped} 段{marker}]...")
                skipped = 0
            pieces.append(segment)
        else:
            skipped += 1
    if skipped:
        pieces.append(f"...[{skipped} 段{marker}]...")
    return joiner.join(pieces)


def compress_text(text: str, max_tokens: int, marker: str = "较长历史内容已压缩") -> str:
    """把文本压到 ``max_tokens`` 以内，按信息密度而不是位置取舍。

    贪心填充之后必须复核总量，因为省略标记本身占 token。复核不通过时**丢掉分数
    最低的中间段再重排**，而不是回头做首尾截断——后者会把刚保住的关键句拦腰切断。
    """

    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    segments = _split_segments(text)
    if len(segments) <= 2:
        return _truncate_middle(text, max_tokens, marker)

    joiner = "\n" if "\n" in text else ""
    last_index = len(segments) - 1

    # 预算里先扣掉省略标记的开销，最多可能出现在头尾两处。
    reserve = estimate_tokens(f"...[999 段{marker}]...") * 2 + 2
    segment_budget = max(32, max_tokens - reserve)

    # 首尾永远保留：开头通常交代背景，结尾通常是结论。
    keep = {0, last_index}
    used = estimate_tokens(segments[0]) + estimate_tokens(segments[last_index])

    order = sorted(
        range(1, last_index),
        key=lambda index: (-_score_segment(segments[index]), index),
    )
    for index in order:
        cost = estimate_tokens(segments[index])
        if used + cost > segment_budget:
            continue
        keep.add(index)
        used += cost

    result = _assemble(segments, keep, marker, joiner)
    while estimate_tokens(result) > max_tokens:
        candidates = [index for index in keep if index not in (0, last_index)]
        if not candidates:
            break
        # 分数最低的先丢；同分时丢更靠后的，保留前文的叙述顺序。
        victim = min(candidates, key=lambda i: (_score_segment(segments[i]), -i))
        keep.discard(victim)
        result = _assemble(segments, keep, marker, joiner)

    if estimate_tokens(result) > max_tokens:
        return _truncate_middle(result, max_tokens, marker)
    return result


def _compact_list(items: list, key: str, depth: int) -> Any:
    if key == "path":
        # 路线几何点动辄上百个，只留起点、终点和点数。
        return {
            "point_count": len(items),
            "start": compact_payload(items[0], depth=depth + 1) if items else None,
            "end": compact_payload(items[-1], depth=depth + 1) if items else None,
        }

    if key == "steps":
        head_count, tail_count = _STEPS_HEAD, _STEPS_TAIL
    else:
        head_count, tail_count = _LIST_HEAD, _LIST_TAIL

    if len(items) <= head_count + tail_count:
        return [compact_payload(item, depth=depth + 1) for item in items]

    head = [compact_payload(item, depth=depth + 1) for item in items[:head_count]]
    tail = [compact_payload(item, depth=depth + 1) for item in items[-tail_count:]]
    omitted = len(items) - head_count - tail_count
    return head + [{"omitted_count": omitted, "note": "中间条目已省略"}] + tail


def compact_payload(value: Any, key: str = "", depth: int = 0) -> Any:
    """工具结果瘦身：只留决策相关字段，丢掉空值和超长内容。

    - 递归深度超过 7 层直接截断，避免病态嵌套；
    - ``None`` / 空串 / 空列表 / 空字典整键丢弃，这类字段占位不占信息；
    - 列表保留头 12 + 尾 4，中间折成 ``omitted_count``；
    - 超长字符串走 :func:`compress_text`，保留含时间与约束的句子。
    """

    if depth >= 7:
        return "[嵌套内容已省略]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")

    if isinstance(value, dict):
        compacted = {}
        for item_key, item_value in value.items():
            if item_value is None or item_value == "" or item_value == [] or item_value == {}:
                continue
            compacted[item_key] = compact_payload(item_value, item_key, depth + 1)
        return compacted

    if isinstance(value, list):
        return _compact_list(value, key, depth)

    if isinstance(value, str) and len(value) > _LONG_STRING_CHARS:
        return compress_text(value, _LONG_STRING_TOKENS, marker="内容已压缩")

    return value


def degrade_tool_message(message: dict) -> Optional[dict]:
    """把一条 tool 消息的 ``data`` 剥掉，只留 success / summary / error。

    返回 ``None`` 表示这条消息已经没有可再压缩的内容。保留 summary 是关键——
    模型即使看不到原始数据，也知道这个工具当时做了什么、成功与否。
    """

    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("data_omitted"):
        return None

    condensed = {
        "success": payload.get("success"),
        "summary": payload.get("summary"),
        "data": None,
        "error": payload.get("error"),
        "data_omitted": True,
        "note": "原始数据已为节省上下文移除；需要具体值请重新调用该工具",
    }
    return {**message, "content": json.dumps(condensed, ensure_ascii=False, default=str)}


def prepare_history(
    messages: List[dict],
    message_token_limit: int,
    total_token_limit: int,
) -> Tuple[List[dict], int]:
    """逐条压缩 + 按总预算从最新往回保留。

    返回 ``(保留的消息, 被丢弃的条数)``。至少保留一条：最新的一条永远是当前问题的
    上下文，宁可超一点预算也不能丢。
    """

    compacted = []
    # 单条上限不能超过总预算，且要扣掉角色开销，否则「至少保留一条」的兜底会突破总量闸门。
    per_message_limit = max(
        32,
        int(min(message_token_limit, total_token_limit)) - _MESSAGE_OVERHEAD_TOKENS,
    )
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = compress_text(content, per_message_limit)
        compacted.append(item)

    kept: List[dict] = []
    used = 0
    for item in reversed(compacted):
        cost = estimate_message_tokens(item)
        if kept and used + cost > total_token_limit:
            break
        kept.append(item)
        used += cost

    kept.reverse()
    return kept, len(compacted) - len(kept)
