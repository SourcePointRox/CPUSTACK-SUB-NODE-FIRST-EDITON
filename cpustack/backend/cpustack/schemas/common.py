"""SQLModel 基类与 ActiveRecordMixin：挂钩 DB 生命周期自动发布事件。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

from cpustack.bus import EventBus, EventType, get_event_bus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin(SQLModel):
    """创建/更新时间戳混入。"""

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=_utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": _utcnow},
    )


class ActiveRecordMixin:
    """ActiveRecord 模式混入：提供便捷的 CRUD 方法与事件发布。

    事件发布通过显式调用 publish_event 完成（在 service 层 commit 后触发），
    避免 before_flush/after_commit 钩子在异步上下文中的复杂性。
    """

    @classmethod
    def _entity_type(cls) -> str:
        """实体类型名，用于事件总线路由。"""
        # 将类名转为 snake_case
        name = cls.__name__
        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append("_")
            result.append(char.lower())
        return "".join(result)

    @classmethod
    async def publish_created(cls, entity_id: int | str, data: dict | None = None) -> None:
        bus = get_event_bus()
        await bus.publish(
            EventBus._make_event(cls._entity_type(), EventType.CREATED, entity_id, data or {})
        )

    @classmethod
    async def publish_updated(cls, entity_id: int | str, data: dict | None = None) -> None:
        bus = get_event_bus()
        await bus.publish(
            EventBus._make_event(cls._entity_type(), EventType.UPDATED, entity_id, data or {})
        )

    @classmethod
    async def publish_deleted(cls, entity_id: int | str, data: dict | None = None) -> None:
        bus = get_event_bus()
        await bus.publish(
            EventBus._make_event(cls._entity_type(), EventType.DELETED, entity_id, data or {})
        )


# 为 EventBus 添加工厂方法
def _make_event(
    entity_type: str, event_type: EventType, entity_id: int | str, data: dict
):
    from cpustack.bus import Event

    return Event(
        entity_type=entity_type,
        event_type=event_type,
        entity_id=entity_id,
        data=data,
    )


EventBus._make_event = staticmethod(_make_event)
