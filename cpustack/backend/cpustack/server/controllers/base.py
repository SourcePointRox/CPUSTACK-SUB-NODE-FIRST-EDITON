"""控制器基类：事件驱动 + 周期全量扫描的调谐循环抽象。

借鉴 Kubernetes Controller 模式：
- 事件驱动：订阅 EventBus 特定实体事件，触发增量调谐
- 周期扫描：定时全量 reconcile，兜底事件丢失
- 期望状态 vs 实际状态收敛

子类需实现：
- reconcile_all()：全量扫描调谐
- _on_event(event)：单事件增量调谐（可选）
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from cpustack.bus import Event, EventBus, get_event_bus

logger = logging.getLogger(__name__)


class Reconciler(ABC):
    """控制器基类：管理事件订阅与周期调谐任务。

    子类设置：
    - entity_type：订阅的实体类型（如 "worker", "model", "model_instance"）
    - scan_interval_seconds：周期全量扫描间隔
    - name：控制器名称（日志用）
    """

    entity_type: str = ""
    scan_interval_seconds: int = 300
    name: str = "reconciler"

    def __init__(self) -> None:
        self._bus: EventBus = get_event_bus()
        self._scan_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """启动控制器：订阅事件 + 启动周期扫描任务。"""
        if self._running:
            return
        self._running = True

        if self.entity_type:
            self._bus.subscribe(self.entity_type, self._on_event_wrapper)
            logger.info("[%s] 已订阅 %s 事件", self.name, self.entity_type)

        self._scan_task = asyncio.create_task(self._scan_loop())
        logger.info(
            "[%s] 已启动，扫描间隔 %ds", self.name, self.scan_interval_seconds
        )

    async def stop(self) -> None:
        """停止控制器。"""
        self._running = False
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        logger.info("[%s] 已停止", self.name)

    async def _on_event_wrapper(self, event: Event) -> None:
        """事件回调包装：捕获异常防止单次调谐失败影响总线。"""
        try:
            await self._on_event(event)
        except Exception:
            logger.exception(
                "[%s] 事件调谐异常 %s #%s", self.name, event.entity_type, event.entity_id
            )

    async def _on_event(self, event: Event) -> None:
        """增量事件调谐（子类覆写）。"""
        # 默认：事件触发即执行一次全量扫描（兜底）
        await self.reconcile_all()

    async def _scan_loop(self) -> None:
        """周期全量扫描循环。"""
        # 启动后先等一小段，避免与 lifespan 启动阶段抢资源
        await asyncio.sleep(2)
        while self._running:
            try:
                await self.reconcile_all()
            except Exception:
                logger.exception("[%s] 周期调谐异常", self.name)
            await asyncio.sleep(self.scan_interval_seconds)

    @abstractmethod
    async def reconcile_all(self) -> None:
        """全量扫描调谐：扫描所有相关实体，驱动向期望状态收敛。"""
        ...
