"""WorkerController：节点故障检测 + 实例迁移重调度。

触发条件：
- Worker 心跳超时被标记 UNREACHABLE（由 scheduler._check_worker_heartbeats 完成）
- 本控制器负责后续动作：将该 Worker 上的 UNREACHABLE 实例重置为 PENDING，
  清除 worker_id 绑定，使其重新进入调度队列被迁移到健康节点。

不处理的情形：
- 手动停止的实例（error_message 含"用户手动停止"）不迁移，保留原地
- 已删除的模型对应的实例跳过
"""

from __future__ import annotations

import logging

from sqlmodel import select

from cpustack.bus import Event, EventType
from cpustack.db import session_scope
from cpustack.schemas.models import ModelInstance, ModelInstanceState
from cpustack.schemas.workers import Worker, WorkerState
from cpustack.server.controllers.base import Reconciler

logger = logging.getLogger(__name__)

# 不参与自动迁移的错误标记（用户主动操作）
_PINNED_MARK = "用户手动停止"


class WorkerController(Reconciler):
    """Worker 故障控制器：UNREACHABLE 节点上的实例迁移重调度。"""

    entity_type = "worker"
    scan_interval_seconds = 120
    name = "worker-controller"

    async def _on_event(self, event: Event) -> None:
        """Worker 事件增量调谐：仅在 UPDATED 且状态变 UNREACHABLE 时触发。"""
        if event.event_type != EventType.UPDATED:
            return
        await self._migrate_instances_for_unreachable()

    async def reconcile_all(self) -> None:
        """全量扫描：处理所有 UNREACHABLE Worker 上的实例。"""
        await self._migrate_instances_for_unreachable()

    async def _migrate_instances_for_unreachable(self) -> None:
        """将 UNREACHABLE Worker 上的 UNREACHABLE 实例重置为 PENDING 触发重调度。"""
        async with session_scope() as session:
            # 查找所有 UNREACHABLE Worker
            w_stmt = select(Worker).where(Worker.state == WorkerState.UNREACHABLE)
            unreachable_workers = (await session.execute(w_stmt)).scalars().all()
            if not unreachable_workers:
                return

            migrated = 0
            for w in unreachable_workers:
                # 查找该 Worker 上需迁移的实例：
                # - 状态为 UNREACHABLE（心跳超时级联标记的）
                # - 排除手动停止的（error_message 含 _PINNED_MARK 的实例状态是 ERROR，不会被命中）
                inst_stmt = select(ModelInstance).where(
                    ModelInstance.worker_id == w.id,
                    ModelInstance.state == ModelInstanceState.UNREACHABLE,
                )
                instances = (await session.execute(inst_stmt)).scalars().all()
                for inst in instances:
                    if _PINNED_MARK in inst.error_message:
                        continue
                    inst.state = ModelInstanceState.PENDING
                    inst.worker_id = None
                    inst.service_port = None
                    inst.error_message = (
                        f"迁移自故障节点 {w.name}: {inst.error_message}".strip()
                    )
                    inst.distributed_config = "{}"
                    inst.rpc_worker_ids = "[]"
                    session.add(inst)
                    await ModelInstance.publish_updated(
                        inst.id, {"state": "pending", "migrated_from": w.name}
                    )
                    migrated += 1
                    logger.info(
                        "实例 %s 从故障节点 %s 迁移，重新进入调度队列",
                        inst.name, w.name,
                    )

            if migrated:
                await session.commit()
                logger.info("WorkerController: 本次迁移 %d 个实例", migrated)
