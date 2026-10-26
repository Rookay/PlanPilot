# -*- coding: UTF-8 -*-
"""同主题的事实被改了值：旧值必须退出注入。

这是真实会话里撞出来的问题：用户先说"季度总结截止 10-30"，第 11 轮改成
10-29。指纹是全文哈希，值一变就是新指纹，于是两条并存，system prompt 里同时
给出两个互相矛盾的截止日——模型只能猜，而猜错的那次看起来像是它幻觉。

修法是给事实一个"主题"，同主题的新值把旧值标成 ``superseded``：退出注入，
但保留以便追溯"改过什么"。
"""

from unittest import TestCase

from app.models.fact import (
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
)
from app.services import fact_service
from tests.helpers import make_session


def _one(db, text, *, category="constraint", subject=None):
    return fact_service.record_facts(
        db, [{"fact": text, "category": category, "subject": subject}]
    )[0]


class SupersedeBySkeletonTests(TestCase):
    """模型没给 subject 时，靠"剥离日期数字后的骨架"兜底。"""

    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_new_deadline_supersedes_the_old_one(self):
        old = _one(self.db, "季度总结截止 10-30")
        self.assertEqual(old.status, STATUS_CANDIDATE)

        new = _one(self.db, "季度总结截止 10-29")

        self.assertNotEqual(old.id, new.id, "值变了就是两条记录，指纹不同")
        self.assertEqual(new.status, STATUS_CANDIDATE)
        self.assertEqual(
            old.status,
            STATUS_SUPERSEDED,
            "旧截止日必须退出，否则 prompt 里同时出现两个日期",
        )

    def test_old_confirmed_value_also_steps_aside(self):
        old = _one(self.db, "季度总结截止 10-30")
        _one(self.db, "季度总结截止 10-30")  # 第二次提到：转 confirmed
        self.assertEqual(old.status, STATUS_CONFIRMED)

        _one(self.db, "季度总结截止 10-29")

        self.assertEqual(old.status, STATUS_SUPERSEDED)

    def test_superseded_fact_is_not_injected(self):
        _one(self.db, "季度总结截止 10-30")
        _one(self.db, "季度总结截止 10-30")
        _one(self.db, "季度总结截止 10-29")
        _one(self.db, "季度总结截止 10-29")

        injected = fact_service.confirmed_facts(self.db)
        texts = [item.fact for item in injected]

        self.assertIn("季度总结截止 10-29", texts)
        self.assertNotIn("季度总结截止 10-30", texts)
        self.assertEqual(len(texts), 1, f"只该剩一条：{texts}")

    def test_different_topics_survive_together(self):
        lecture = _one(self.db, "每周三晚上有组会", category="entity")

        _one(self.db, "季度总结截止 10-29", category="constraint")

        self.assertEqual(
            lecture.status,
            STATUS_CANDIDATE,
            "说的是两件事，不能因为批次相邻就被顶掉",
        )

    def test_tiny_skeleton_does_not_match(self):
        first = _one(self.db, "1 号楼", category="entity")

        _one(self.db, "2 号楼", category="entity")

        self.assertEqual(
            first.status,
            STATUS_CANDIDATE,
            "剥掉数字只剩两个字，匹配上没有意义，不能当同一主题",
        )

    def test_rejected_fact_is_never_superseded(self):
        rejected = _one(self.db, "季度总结截止 10-30")
        fact_service.set_status(self.db, rejected.id, STATUS_REJECTED)

        _one(self.db, "季度总结截止 10-29")

        self.assertEqual(
            rejected.status,
            STATUS_REJECTED,
            "用户的否决优先于后续提取，不能被新值悄悄改掉",
        )

    def test_old_value_reappearing_is_restored(self):
        old = _one(self.db, "季度总结截止 10-30")
        new = _one(self.db, "季度总结截止 10-29")
        self.assertEqual(old.status, STATUS_SUPERSEDED)

        # 用户又改回 10-30：旧值重新参与候选，新值退出。
        restored = _one(self.db, "季度总结截止 10-30")

        self.assertEqual(restored.id, old.id, "同值复用同一条记录")
        self.assertEqual(restored.status, STATUS_CANDIDATE)
        self.assertEqual(restored.hit_count, 1, "按正常门槛重新积累，不借旧历史")
        self.assertEqual(new.status, STATUS_SUPERSEDED)


class SupersedeBySubjectTests(TestCase):
    """模型明确给了 subject 时，它比骨架更准。"""

    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_same_subject_with_different_names_supersedes(self):
        old = _one(self.db, "导师姓王", category="entity", subject="导师的姓氏")

        new = _one(self.db, "导师姓李", category="entity", subject="导师的姓氏")

        # 骨架完全不同（"导师姓王" vs "导师姓李"），只有 subject 能匹配上。
        self.assertNotEqual(
            fact_service._skeleton(old.fact), fact_service._skeleton(new.fact)
        )
        self.assertEqual(old.status, STATUS_SUPERSEDED)

    def test_different_subjects_do_not_collide(self):
        lecture = _one(self.db, "组会在周三", category="entity", subject="组会时间")
        meeting = _one(self.db, "例会在周五", category="entity", subject="例会时间")

        self.assertEqual(lecture.status, STATUS_CANDIDATE)
        self.assertEqual(meeting.status, STATUS_CANDIDATE)

    def test_subject_is_stored_for_later_comparisons(self):
        record = _one(self.db, "季度总结截止 10-29", subject="季度总结截止日期")
        self.assertEqual(record.subject, "季度总结截止日期")

    def test_subject_filled_in_later_still_matches(self):
        # 第一次模型漏给 subject，第二次补上——不能因此变成两条并存的事实。
        old = _one(self.db, "季度总结截止 10-30")
        new = _one(self.db, "季度总结截止 10-29", subject="季度总结截止日期")

        self.assertEqual(old.status, STATUS_SUPERSEDED)
        self.assertEqual(new.subject, "季度总结截止日期")
