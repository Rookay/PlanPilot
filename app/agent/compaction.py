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
   首尾段有位置加成（通常是背景与结论），但加成不是豁免——预算真不够时一样会被丢。
3. **压缩必须留痕，且不能把结构压坏。** 丢掉的内容要给出数量和提示，让模型知道
   有缺口、可以重新查询；结构化内容（JSON）压缩后必须仍然是**合法 JSON**。把
   一条工具结果压成解析不了的半截文本，比不压更糟——模型连"少了什么"都判断不出来。
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
#: 硬截断时允许把切点回退到的位置，避免拦腰切断一句话。
_BREAK_CHARS = set("。！？；，、：,.!?;:\n）】」』》")

#: 首段 / 尾段的位置加成。与约束词同量级，够它优先被保留，但不足以豁免预算。
_EDGE_BONUS = 3

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
#: 预留给省略占位的最小段预算，防止预算极小时逐段试探到负数。
_MIN_SEGMENT_BUDGET = 24
#: JSON 逐级收紧的档位：(列表头, 列表尾, 长字符串 token 上限)。
#: 从"保留结构原貌"一路收到"只剩骨架"，每一档都还是一个完整对象。
_JSON_SHRINK_STEPS = (
    (_LIST_HEAD, _LIST_TAIL, _LONG_STRING_TOKENS),
    (8, 3, 200),
    (4, 2, 120),
    (2, 1, 60),
    (1, 1, 40),
    (0, 0, 20),
)


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


# ---------------------------------------------------------------------------
# 纯文本：切句 + 打分取舍
# ---------------------------------------------------------------------------


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


def _segment_score(segments: List[str], index: int) -> int:
    """信息密度 + 位置加成。

    入选和剔除必须用同一个口径，否则会出现"靠加成入选、剔除时按裸分被提前
    丢掉"的自相矛盾。
    """

    score = _score_segment(segments[index])
    if index == 0 or index == len(segments) - 1:
        score += _EDGE_BONUS
    return score


def _split_text(text: str) -> Tuple[List[str], str]:
    """按行切；只有一行时退化为按句切。返回 ``(段列表, 连接符)``。"""

    if "\n" in text:
        return text.split("\n"), "\n"
    return [segment for segment in _SENTENCE_SPLIT_RE.split(text) if segment], ""


def _assemble(segments: List[str], keep: set, joiner: str, marker: str) -> str:
    """按原顺序拼接保留段，连续的被跳过段落折成一条省略标记。"""

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


def _align_cut(text: str, cut: int, window: int = 30) -> int:
    """把切点往前挪到最近的标点之后，别把一句话从中间劈开。"""

    if cut <= 0:
        return 0
    start = max(0, cut - window)
    for offset in range(cut - 1, start - 1, -1):
        if text[offset] in _BREAK_CHARS:
            return offset + 1
    return cut


def _truncate_middle(text: str, max_tokens: int, marker: str) -> str:
    """无结构文本的兜底：保头保尾，用二分找能塞进去的最大长度。

    切点对齐到标点边界；对齐会让长度变得不完全单调，所以每一轮同时试原始
    切点和对齐后的切点，取能过预算的更长者。
    """

    cut = f"...[{marker}]..."
    if estimate_tokens(cut) >= max_tokens:
        return cut

    def candidate(head_len: int) -> str:
        tail = text[-min(head_len // 4, 400):] if head_len else ""
        return text[:head_len] + cut + tail

    best = cut
    low, high = 0, len(text)
    while low <= high:
        middle = (low + high) // 2
        accepted: Optional[Tuple[int, str]] = None
        for head_len in (middle, _align_cut(text, middle)):
            candidate_text = candidate(head_len)
            if estimate_tokens(candidate_text) <= max_tokens:
                if accepted is None or head_len > accepted[0]:
                    accepted = (head_len, candidate_text)
        if accepted is not None:
            best = accepted[1]
            low = middle + 1
        else:
            high = middle - 1
    return best


def _compress_free_text(text: str, max_tokens: int, marker: str) -> str:
    """自由文本路径：切段 → 打分 → 贪心填充 → 复核。"""

    segments, joiner = _split_text(text)
    if len(segments) <= 2:
        return _truncate_middle(text, max_tokens, marker)

    # 只预留一处占位：省略段连续时本来也只会生成一个标记。按最坏情况预留
    # （头尾各一个长标记）会把大半预算吃掉，那是上一版的实测问题。
    reserve = estimate_tokens(f"...[99 段{marker}]...")
    segment_budget = max(_MIN_SEGMENT_BUDGET, max_tokens - reserve)

    costs = [estimate_tokens(segment) for segment in segments]
    keep: set = set()
    used = 0
    order = sorted(
        range(len(segments)),
        key=lambda index: (-_segment_score(segments, index), index),
    )
    for index in order:
        if used + costs[index] <= segment_budget:
            keep.add(index)
            used += costs[index]

    result = _assemble(segments, keep, joiner, marker)
    while estimate_tokens(result) > max_tokens and keep:
        # 分数最低的先丢；同分时丢更靠后的，保留前文的叙述顺序。
        victim = min(keep, key=lambda index: (_segment_score(segments, index), -index))
        keep.discard(victim)
        result = _assemble(segments, keep, joiner, marker)

    if estimate_tokens(result) > max_tokens:
        return _truncate_middle(result, max_tokens, marker)
    return result


# ---------------------------------------------------------------------------
# 结构化内容：交给 compact_payload 逐级收紧
# ---------------------------------------------------------------------------


def _shrink_json_text(text: str, max_tokens: int) -> Optional[str]:
    """把单行 JSON 压到预算内，保持可解析。不是 JSON 时返回 ``None``。

    文本级切段在 JSON 上行不通：外层往往只有三五个 key，其中承载数据的那一个
    体积最大，按"段太大就跳过"的规则会被整段丢掉，剩下的是一堆无关紧要的
    元字段。所以这里走结构化的路——逐档收紧列表保留条数和长字符串上限，
    每一档都还能 ``json.loads``。
    """

    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None

    for head, tail, string_tokens in _JSON_SHRINK_STEPS:
        candidate = json.dumps(
            compact_payload(
                payload,
                list_head=head,
                list_tail=tail,
                string_tokens=string_tokens,
            ),
            ensure_ascii=False,
            default=str,
        )
        if estimate_tokens(candidate) <= max_tokens:
            return candidate
    # 收紧到只剩骨架还是超预算：这已经不是"压缩"能解决的问题，交给兜底截断。
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def compress_text(
    text: str, max_tokens: int, marker: str = "较长历史内容已压缩"
) -> str:
    """把文本压到 ``max_tokens`` 以内。

    分两条路：像 JSON 的走 :func:`_shrink_json_text`（保结构），其余走
    :func:`_compress_free_text`（按信息密度取舍）。两者都以"压完还能被读懂"
    为前提——省 token 不是目的，让模型在有限预算内拿到可决策的信息才是。
    """

    if not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    if "\n" not in text:
        structured = _shrink_json_text(text, max_tokens)
        if structured is not None:
            return structured

    return _compress_free_text(text, max_tokens, marker)


# ---------------------------------------------------------------------------
# 工具结果瘦身
# ---------------------------------------------------------------------------


def _compact_list(
    items: list,
    key: str,
    depth: int,
    head: int,
    tail: int,
    string_tokens: int,
) -> Any:
    if key == "path":
        # 路线几何点动辄上百个，只留起点、终点和点数。
        return {
            "point_count": len(items),
            "start": _compact(items[0], "", depth + 1, head, tail, string_tokens) if items else None,
            "end": _compact(items[-1], "", depth + 1, head, tail, string_tokens) if items else None,
        }

    if key == "steps":
        # 步骤序列通常更长，收紧时也跟着缩，但保持"头重尾轻"的比例。
        head_count, tail_count = max(1, head // 2), max(0, tail // 2)
    else:
        head_count, tail_count = head, tail

    if len(items) <= head_count + tail_count:
        return [_compact(item, "", depth + 1, head, tail, string_tokens) for item in items]

    kept_head = [
        _compact(item, "", depth + 1, head, tail, string_tokens)
        for item in items[:head_count]
    ]
    kept_tail = (
        [
            _compact(item, "", depth + 1, head, tail, string_tokens)
            for item in items[-tail_count:]
        ]
        if tail_count
        else []
    )
    omitted = len(items) - head_count - tail_count
    return kept_head + [{"omitted_count": omitted, "note": "中间条目已省略"}] + kept_tail


def _compact(value: Any, key: str, depth: int, head: int, tail: int, string_tokens: int) -> Any:
    if depth >= 7:
        return "[嵌套内容已省略]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")

    if isinstance(value, dict):
        compacted = {}
        for item_key, item_value in value.items():
            if item_value is None or item_value == "" or item_value == [] or item_value == {}:
                continue
            compacted[item_key] = _compact(
                item_value, item_key, depth + 1, head, tail, string_tokens
            )
        return compacted

    if isinstance(value, list):
        return _compact_list(value, key, depth, head, tail, string_tokens)

    if isinstance(value, str) and len(value) > _LONG_STRING_CHARS:
        return compress_text(value, string_tokens, marker="内容已压缩")

    return value


def compact_payload(
    value: Any,
    key: str = "",
    depth: int = 0,
    *,
    list_head: int = _LIST_HEAD,
    list_tail: int = _LIST_TAIL,
    string_tokens: int = _LONG_STRING_TOKENS,
) -> Any:
    """工具结果瘦身：只留决策相关字段，丢掉空值和超长内容。

    - 递归深度超过 7 层直接截断，避免病态嵌套；
    - ``None`` / 空串 / 空列表 / 空字典整键丢弃，这类字段占位不占信息；
    - 列表默认保留头 12 + 尾 4，中间折成 ``omitted_count``；
    - 超长字符串走 :func:`compress_text`，保留含时间与约束的句子。

    ``list_head`` / ``list_tail`` / ``string_tokens`` 是给
    :func:`_shrink_json_text` 用的收紧旋钮，正常调用保持默认即可。
    """

    return _compact(value, key, depth, list_head, list_tail, string_tokens)


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
