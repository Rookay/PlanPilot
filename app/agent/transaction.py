# -*- coding: UTF-8 -*-
"""Agent 批量确认的补偿式事务快照。

工作原理：
确认流（``Agent._handle_confirmation``）在批量执行用户确认的多个 tool 之前，
调用 :meth:`AgentDatabaseSnapshot.capture` 把当前数据库里六张关键表的全部行
原样抓到一个快照里；执行期间任意一步失败时调用 :meth:`restore`，回滚当前事务
→ 全表删除 → 按依赖顺序重灌快照。

⚠️ 重要约束（所有 ``app/tools/*.py`` 的作者必读）：
    执行期间被串行调用的所有 tool **不得自己** ``db.commit()``、**不得**新开
    Session。所有改动必须留在当前请求的 Session 中，等待 restore 时整体回滚。
    原因：本快照只能复原当前 Session 范围内的状态；如果某个 tool 提前 commit，
    那么真实数据库已经动了，``rollback()`` 与重建都救不回来，restore 之后的状态
    与真实 DB 出现分叉。

可观察性：
    ``restore`` 成功后立刻 ``db.expire_all()``，所有挂着的 ORM 对象都会被重新
    加载；如果有调用方曾持有被删行的引用，再次访问会触发 ``DetachedInstanceError``，
    这是有意为之：迫使上层重新查询、避免误用陈旧缓存。

依赖顺序：
    :data:`_DELETE_ORDER` 必须满足外键约束（先删子表 / 后删父表）；
    :data:`_RESTORE_ORDER` 反过来，先恢复父表再恢复子表。当前两张表里的 ``trip_id``
    仅是业务标记，无外键，但仍然按"创建在前、依赖在后"的直觉顺序重建。
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.plan_version import PlanVersion
from app.models.preference import UserPreference
from app.models.schedule import Schedule
from app.models.task import Task, TaskDependency


_MODELS = (Task, Event, TaskDependency, Schedule, UserPreference, PlanVersion)
_DELETE_ORDER = (Schedule, TaskDependency, Event, Task, UserPreference, PlanVersion)
_RESTORE_ORDER = (Task, Event, TaskDependency, Schedule, UserPreference, PlanVersion)


# 用一个模块级异常类型标记 — 当前没启用，后续若想强制执行"tool 不能 commit"的
# 约束，可以用 AST / monkey-patch / 中间件方式检测到 ``session.commit()`` 被调用
# 时抛错，作为对工具作者纪律的硬保障。
class ToolCommitNotAllowedError(RuntimeError):
    """在确认流执行期间，工具不应直接 ``db.commit()``。

    现阶段只是文档化该约束，不强制抛错——强制需要更深入的 monkey-patch，
    留给后续加固。
    """


@dataclass
class AgentDatabaseSnapshot:
    """整个 Agent 批量确认流的"撤销点"。

    实例方法：
    - :meth:`capture` 在批量执行前调用一次，序列化 6 张表的当前内容。
    - :meth:`restore` 在执行失败时调用，把数据库擦回快照时刻。
    """

    rows: dict[type, list[dict]]

    @classmethod
    def capture(cls, db: Session) -> "AgentDatabaseSnapshot":
        rows = {}
        for model in _MODELS:
            rows[model] = [
                {column.name: getattr(item, column.name) for column in model.__table__.columns}
                for item in db.query(model).all()
            ]
        return cls(rows=rows)

    def restore(self, db: Session) -> None:
        """回滚到快照时刻。

        步骤：
        1. ``db.rollback()`` —— 丢弃当前 Session 未提交的改动。
        2. 按 :data:`_DELETE_ORDER` 删除所有表（满足外键约束）。
        3. ``db.flush()`` 让删除生效，使后续插入不会被唯一键冲突。
        4. ``db.expunge_all()`` 切断 ORM 身份映射，避免重建时把已删对象误以为存在。
        5. 按 :data:`_RESTORE_ORDER` 重建所有行。
        6. ``db.commit()`` 落盘。
        7. ``db.expire_all()`` 强制所有挂着的 ORM 实例下次访问时重新加载。
        """

        db.rollback()
        for model in _DELETE_ORDER:
            db.query(model).delete(synchronize_session=False)
        db.flush()
        db.expunge_all()
        for model in _RESTORE_ORDER:
            db.add_all(model(**values) for values in self.rows.get(model, []))
            db.flush()
        db.commit()
        db.expire_all()
