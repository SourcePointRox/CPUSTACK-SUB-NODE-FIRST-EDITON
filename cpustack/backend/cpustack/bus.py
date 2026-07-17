"""事件总线：进程内异步事件驱动，挂钩 DB 生命周期自动发布事件。

借鉴 GPUStack 的 EventBus 设计：
- 基于 asyncio.Queue 的单例事件总线
- 支持事件订阅/发布
- 更新合并(Update Squashing)：同实体ID的多次 UPDATED 仅保留最新
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"


@dataclass
class Event:
    """事件对象。"""

    entity_type: str  # 实体类型，如 "model", "model_instance", "worker"
    event_type: EventType
    entity_id: int | str
    data: dict = field(default_factory=dict)


class EventBus:
    """进程内事件总线单例。

    订阅者注册回调函数，发布者推送事件。
    更新合并：同一实体ID的连续 UPDATED 事件仅保留最新一条。
    """

    _instance: EventBus | None = None

    def __new__(cls) -> EventBus:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._subscribers: dict[str, list[Callable[[Event], Coroutine[Any, Any, None]]]] = (
            defaultdict(list)
        )
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False
        # 待合并的更新事件：{(entity_type, entity_id): Event}
        self._pending_updates: dict[tuple[str, int | str], Event] = {}

    def subscribe(
        self,
        entity_type: str,
        callback: Callable[[Event], Coroutine[Any, Any, None]],
    ) -> None:
        """订阅特定实体类型的事件。"""
        self._subscribers[entity_type].append(callback)
        logger.debug("订阅事件: %s -> %s", entity_type, callback.__qualname__)

    async def publish(self, event: Event) -> None:
        """发布事件。

        对于 UPDATED 事件，执行更新合并：同实体ID的未处理更新仅保留最新。
        """
        if event.event_type == EventType.UPDATED:
            key = (event.entity_type, event.entity_id)
            self._pending_updates[key] = event
        else:
            # CREATED/DELETED 立即入队
            await self._queue.put(event)

    async def start(self) -> None:
        """启动事件分发循环。"""
        if self._running:
            return
        self._running = True
        asyncio.create_task(self._dispatch_loop())
        logger.info("事件总线已启动")

    async def stop(self) -> None:
        """停止事件分发。"""
        self._running = False

    async def _dispatch_loop(self) -> None:
        """事件分发主循环：定期刷新合并更新 + 处理事件队列。"""
        while self._running:
            # 刷新待合并的更新事件到队列
            if self._pending_updates:
                for key, event in list(self._pending_updates.items()):
                    await self._queue.put(event)
                    del self._pending_updates[key]

            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            subscribers = self._subscribers.get(event.entity_type, [])
            for callback in subscribers:
                try:
                    await callback(event)
                except Exception:
                    logger.exception(
                        "事件处理异常: %s %s #%s",
                        event.entity_type,
                        event.event_type,
                        event.entity_id,
                    )


def get_event_bus() -> EventBus:
    """获取事件总线单例。"""
    return EventBus()
