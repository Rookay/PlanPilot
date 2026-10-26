# -*- coding: UTF-8 -*-
# Project ：mine
# File    ：fact_service.py
"""``memory_facts`` 的读写与候选转正。

核心规则只有三条，但每条都是为了防一个具体的坏结果：

1. **按指纹去重**：同一件事换个标点、加个"的"不该变成两条。不归一化的话，
   重复提取会让表无限膨胀，而 hit_count 也就失去了"独立提及"的含义。
2. **满两次才转正**：模型归纳出来的东西第一次就信，等于把幻觉直接写进 system
   prompt——它比用户亲口说的权重还高，因为每次请求都会带。二次确认是最低成本
   的降噪。
3. **rejected 永不自动复活**：用户否决过的，再提也不拉回候选。否则"否决"只是
   一次性的，下次折叠又被提取回来，用户会觉得这个开关根本没用。
4. **同主题的新值顶掉旧值**：指纹是全文哈希，值一变就是新指纹，于是"截止
   10-30"和"截止 10-29"会并存注入 prompt。旧的那条标成 ``superseded``：
   保留可追溯，但不再参与注入。这就是 ``subject`` 存在的理由。
"""

import hashlib
import re
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.fact import (
    CATEGORIES,
    CONFIRM_HIT_THRESHOLD,
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    MemoryFact,
)
from app.utils import utcnow

#: 单条事实的长度上限。太长的一定是句子而不是"事实"，抽象成一句反而更准。
MAX_FACT_CHARS = 200

#: 兜底骨架的最短长度。"1 号楼"剥掉数字只剩两个字，拿它判同主题会连累无关事实。
MIN_SKELETON_CHARS = 3

_STRIP_RE = re.compile(r"[\s，。；、！？,.;!?：:（）()\[\]【】\"'“”‘’]")
_NUMBER_RE = re.compile(r"\d+")
#: "10-30"、"2026/10/30" 这类日期整体剥掉，留下"截止日期"这样的骨架。
_DATE_RE = re.compile(
    r"\d{2,4}\s*[-/年.]\s*\d{1,2}\s*([-/月.]\s*\d{1,2}\s*日?)?"
)


def fingerprint(fact: str) -> str:
    """归一化文本的哈希，作为去重键。"""

    normalized = _STRIP_RE.sub("", fact).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _skeleton(fact: str) -> str:
    """剥掉日期、数字与标点后剩下的骨架，用来兜底判断同主题。"""

    text = _DATE_RE.sub("", fact)
    text = _STRIP_RE.sub("", text).lower()
    return _NUMBER_RE.sub("", text)


def subject_key(fact: str, subject: Optional[str] = None) -> str:
    """主题键：优先用模型给的 subject，没有则退回骨架。

    模型给的更准（能识别"导师姓王"→"导师姓李"是同一属性），骨架只在
    模型漏填时兜底。
    """

    if subject:
        normalized = _STRIP_RE.sub("", str(subject)).lower()
        if len(normalized) >= 2:
            return normalized
    return _skeleton(fact)


def _record_subject_key(record: MemoryFact) -> str:
    return subject_key(record.fact, record.subject)


def _same_subject(left: MemoryFact, right_key: str) -> bool:
    """两条事实是否在说同一件事。

    相等是第一优先；一方包含另一方也算（"季度总结截止" vs
    "季度总结截止日期从改为"），但短的那方不能短于阈值。
    """

    a = _record_subject_key(left)
    b = right_key
    if not a or not b:
        return False
    if a == b:
        return len(a) >= MIN_SKELETON_CHARS
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= MIN_SKELETON_CHARS and short in long


def _supersede_siblings(
    db: Session, record: MemoryFact, key: str
) -> List[MemoryFact]:
    """把同主题的旧事实标成 superseded。

    只动 candidate / confirmed：rejected 是用户的否决，不该被后来的提取顶掉；
    已经是 superseded 的不用重复标记。
    """

    siblings = (
        db.query(MemoryFact)
        .filter(
            MemoryFact.id != record.id,
            MemoryFact.status.in_([STATUS_CANDIDATE, STATUS_CONFIRMED]),
        )
        .all()
    )

    superseded: List[MemoryFact] = []
    for other in siblings:
        if not _same_subject(other, key):
            continue
        other.status = STATUS_SUPERSEDED
        other.updated_at = utcnow()
        superseded.append(other)
    return superseded


def record_facts(
    db: Session,
    facts: Iterable[dict],
    *,
    session_id: Optional[str] = None,
) -> List[MemoryFact]:
    """把一次提取的事实写进表（候选制）。**不 commit**。

    返回本次真正新增或更新的记录，便于调用方统计。
    """

    touched: List[MemoryFact] = []
    #: 同一批里重复提到只算一次，否则模型一次给两条同义事实就会造两条候选，
    #: 各自 hit_count=1，谁都不够转正。
    seen: dict = {}

    for item in facts:
        if not isinstance(item, dict):
            continue
        text = str(item.get("fact") or "").strip()
        if not text or len(text) > MAX_FACT_CHARS:
            continue

        category = str(item.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            category = "other"

        raw_subject = item.get("subject")
        raw_subject = str(raw_subject).strip()[:120] if raw_subject else None

        key = fingerprint(text)
        if key in seen:
            continue

        record = (
            db.query(MemoryFact).filter(MemoryFact.fingerprint == key).one_or_none()
        )

        if record is None:
            record = MemoryFact(
                fingerprint=key,
                subject=raw_subject,
                fact=text,
                category=category,
                status=STATUS_CANDIDATE,
                hit_count=1,
                session_id=session_id,
            )
            db.add(record)
            # 生产的 Session 是 autoflush=False，不显式 flush 的话同一事务内下一次
            # 查询看不到这条 pending 记录，会再插一条同指纹的，commit 时撞唯一约束。
            db.flush()
            seen[key] = record
            touched.append(record)
            _supersede_siblings(db, record, subject_key(text, raw_subject))
            continue

        if record.status == STATUS_REJECTED:
            # 用户否决过：只记"又提了一次"，状态不动。
            record.last_seen_at = utcnow()
            seen[key] = record
            touched.append(record)
            continue

        if record.status == STATUS_SUPERSEDED:
            # 旧值又被提出来：说明用户改回去了，重新参与候选。
            # hit_count 归 1，让它按正常门槛重新积累，而不是借旧值的历史。
            record.status = STATUS_CANDIDATE
            record.hit_count = 1
            record.subject = record.subject or raw_subject
            record.last_seen_at = utcnow()
            seen[key] = record
            touched.append(record)
            _supersede_siblings(db, record, _record_subject_key(record))
            continue

        record.hit_count += 1
        record.last_seen_at = utcnow()
        # 模型这次给了主题就补上，之前漏填的也能被后续提取修正。
        if raw_subject and not record.subject:
            record.subject = raw_subject
        if (
            record.status == STATUS_CANDIDATE
            and record.hit_count >= CONFIRM_HIT_THRESHOLD
        ):
            record.status = STATUS_CONFIRMED
        seen[key] = record
        touched.append(record)
        _supersede_siblings(db, record, _record_subject_key(record))

    # 生产的 Session 是 autoflush=False：不 flush 的话，本次刚改的 status 只存在
    # 对象上，同一事务里接着查 confirmed_facts 会拿到改之前的结果——折叠完立刻
    # 拼下一轮 prompt 正好走这条路径。
    db.flush()
    return touched


def list_facts(
    db: Session,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[MemoryFact]:
    query = db.query(MemoryFact)
    if status:
        query = query.filter(MemoryFact.status == status)
    return query.order_by(MemoryFact.hit_count.desc(), MemoryFact.id.desc()).limit(limit).all()


def set_status(db: Session, fact_id: int, status: str) -> Optional[MemoryFact]:
    """人工确认 / 否决。**不 commit**。"""

    if status not in (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_CANDIDATE):
        return None
    record = db.query(MemoryFact).filter(MemoryFact.id == fact_id).one_or_none()
    if record is None:
        return None
    record.status = status
    # 人工确认就是把 hit_count 顶到阈值，避免下次提取又把它算成"候选证据不足"。
    if status == STATUS_CONFIRMED:
        record.hit_count = max(record.hit_count, CONFIRM_HIT_THRESHOLD)
    return record


def delete_fact(db: Session, fact_id: int) -> bool:
    """物理删除。**不 commit**。

    删除而不是标记：一条错到需要删的事实，留着只会被下次提取再次命中。
    """

    deleted = (
        db.query(MemoryFact)
        .filter(MemoryFact.id == fact_id)
        .delete(synchronize_session=False)
    )
    return bool(deleted)


def confirmed_facts(db: Session, limit: int = 12) -> List[MemoryFact]:
    return list_facts(db, status=STATUS_CONFIRMED, limit=limit)


def render_facts(facts: List[MemoryFact]) -> Optional[str]:
    """渲染成 system prompt 的一段；没有已确认事实时返回 None。"""

    if not facts:
        return None

    lines = [
        "【长期事实（跨会话归纳）】",
        "这些是此前对话里归纳出的稳定信息，用于理解称呼和背景；"
        "涉及任务、日程、时间等具体数据时，仍以工具查询结果为准。",
    ]
    for item in facts:
        lines.append(f"- {item.fact}")
    return "\n".join(lines)
