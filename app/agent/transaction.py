# -*- coding: UTF-8 -*-
"""Agent 批量确认的补偿式事务快照。

为什么不是 SAVEPOINT：
确认流里每一步都会调到 service 层，而 ``app/services/*`` 大量自己
``db.commit()``（``delete_event``、``delete_task`` 等）。``commit()`` 会隐式
释放所有 SAVEPOINT，撤销点在第一个工具跑完时就失效了。要改用 SAVEPOINT，
得先把 20 多处 service 提交上移到请求层——那是另一个量级的改动。

所以这里做的是**补偿**：执行前记下六张关键表的内容，失败时把状态修回去。

为什么不是"全表删除再灌回"：
旧实现 ``DELETE`` 整张表再按快照重灌。写放大只是表象，真正的危害是并发下
会删掉**别的会话刚提交的行**——请求 A 回滚时执行 ``DELETE FROM tasks``，
请求 B 在同一时刻新建的任务就没了，且不报错、不可恢复。

现在的关键区别：只删"本次确认流写进去的行"。写集用 SQLAlchemy 的
``after_flush`` 事件累积。service 内部 commit 之前必然先 flush，所以哪怕它
自己提交了，写集也能抓到。快照之外、写集之外的行一律不动。

⚠️ 约束（``app/tools/*.py`` 与相关 service 的作者必读）：
    不要在工具里新开 Session。同一个请求必须共用同一个 Session，否则写集
    抓不到，回滚会漏。

已知局限：
    如果别的会话在本次确认流期间**修改了同一行**，restore 会把快照值写回去，
    覆盖对方的修改。冲突粒度是行级、最后写入者生效——这与"同一份数据被两个人
    同时编辑"的语义一致，不属于本类要解决的问题。

可观察性：
    ``restore`` 完成后 ``db.expire_all()``，所有挂着的 ORM 对象下次访问都会
    重新查库；调用方不应跨 restore 持有引用。
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Set, Tuple

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.plan_version import PlanVersion
from app.models.preference import UserPreference
from app.models.schedule import Schedule
from app.models.task import Task, TaskDependency

logger = logging.getLogger(__name__)


#: 参与快照的表。按"创建在前、依赖在后"排列（恢复顺序）。
_MODELS = (Task, Event, TaskDependency, Schedule, UserPreference, PlanVersion)

#: 恢复顺序：父表先于子表，否则外键会挡住插入。
_RESTORE_ORDER = _MODELS


class ToolCommitNotAllowedError(RuntimeError):
    """标记"工具不应自行提交"这条约束。

    当前不主动抛出：service 层确实在提交，这是既有事实而非可以就地禁止的行为。
    保留类型是为了让 ``app/tools`` 的作者在阅读时能看到这条约束存在。
    """


def _primary_key(obj) -> Tuple:
    """取主键元组。用元组而不是单值，兼容复合主键。"""

    mapper = obj.__mapper__
    return tuple(getattr(obj, column.key) for column in mapper.primary_key)


def _row_dict(obj) -> Dict:
    return {
        column.name: getattr(obj, column.name) for column in obj.__table__.columns
    }


@dataclass
class AgentDatabaseSnapshot:
    """整个 Agent 批量确认流的撤销点。

    - ``rows``：执行前六张表的全部内容，按主键索引。
    - ``inserted`` / ``deleted``：本次确认流写过的行（按模型分组的写集）。
      这是"只撤销自己的改动"的依据。
    """

    rows: Dict[type, Dict[Tuple, Dict]] = field(default_factory=dict)
    inserted: Dict[type, Set[Tuple]] = field(default_factory=dict)
    deleted: Dict[type, Set[Tuple]] = field(default_factory=dict)
    _listener: object = None

    @classmethod
    def capture(cls, db: Session) -> "AgentDatabaseSnapshot":
        snapshot = cls(
            rows={
                model: {_primary_key(item): _row_dict(item) for item in db.query(model).all()}
                for model in _MODELS
            },
            inserted={model: set() for model in _MODELS},
            deleted={model: set() for model in _MODELS},
        )

        def _track(session, _flush_context, _instances=None) -> None:
            # after_flush 里 session.new / session.deleted 还没被清空，
            # 能拿到本次 flush 涉及的行的主键。service 内部 commit 之前必定
            # 先 flush，所以这里抓得到它提交的内容。
            for obj in session.new:
                model = type(obj)
                if model in snapshot.inserted:
                    snapshot.inserted[model].add(_primary_key(obj))
            for obj in session.deleted:
                model = type(obj)
                if model in snapshot.deleted:
                    snapshot.deleted[model].add(_primary_key(obj))

        snapshot._listener = _track
        sa_event.listen(db, "after_flush", _track)
        return snapshot

    def _detach_listener(self, db: Session) -> None:
        if self._listener is not None:
            sa_event.remove(db, "after_flush", self._listener)
            self._listener = None

    def restore(self, db: Session) -> None:
        """把数据库修回快照时刻，且只动本次确认流碰过的行。

        步骤：
        1. 撤掉监听；``db.rollback()`` 丢弃尚未提交的改动。
        2. 删掉本次新增的行（写集里的 inserted）。
        3. 按 :data:`_RESTORE_ORDER` 逐表把快照里的行恢复：还在就改回原值，
           被删了就重新插入。
        4. ``commit()`` 落盘，``expire_all()`` 让上层下次访问重新查库。

        快照里没有、写集里也没有的行（别的会话新增的）不会被碰到。
        """

        self._detach_listener(db)
        db.rollback()

        for model in _RESTORE_ORDER:
            self._restore_model(db, model)

        db.commit()
        db.expire_all()

    def _restore_model(self, db: Session, model: type) -> None:
        original = self.rows.get(model, {})
        dirty_inserts = self.inserted.get(model, set())

        current = {_primary_key(item): item for item in db.query(model).all()}

        # 1. 本次新插入的：删掉。别的会话新增的行不在 dirty_inserts 里，不受影响。
        for key in dirty_inserts:
            obj = current.get(key)
            if obj is not None:
                db.delete(obj)
        db.flush()

        # 2. 快照里有的：存在则改回原值，不存在则补回去。
        for key, values in original.items():
            obj = current.get(key)
            if obj is None:
                db.add(model(**values))
                continue
            for name, value in values.items():
                if getattr(obj, name) != value:
                    setattr(obj, name, value)
        db.flush()

    def release(self, db: Session) -> None:
        """成功路径：撤掉监听，改动保留在外层事务里。

        service 层会自己提交，因此这里通常无事可做；保留方法是为了让
        "这次确认已定稿、不再需要撤销点"在调用处显式可见。
        """

        self._detach_listener(db)
