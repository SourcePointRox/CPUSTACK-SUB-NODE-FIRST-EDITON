"""ModelController：模型副本扩缩容调谐。

借鉴 K8s Deployment 的副本调谐逻辑：
- 期望副本数（Model.replicas）vs 实际"有效"实例数
- 有效实例 = 非 ERROR 且非 UNREACHABLE 的实例（含 PENDING/SCHEDULED/RUNNING 等）
- 扩容：有效 < 期望 → 创建新 PENDING 实例
- 缩容：有效 > 期望 → 优先删除 ERROR/UNREACHABLE 实例，再删最旧的 PENDING

不处理的情形：
- 手动停止的实例（error_message 含"用户手动停止"）不纳入有效计数，也不删除
- 流水线/数据并行的 replicas 语义：
  - 流水线并行：replicas=节点数，实例本身是1个但调度到多节点（由 scheduler 处理）
  - 数据并行：replicas=副本数，每个副本是独立实例
  本控制器统一按"实例数"调谐，对流水线并行 replicas=2 时只创建1个实例（由 scheduler 分配多节点）
"""

from __future__ import annotations

import logging
import secrets

from sqlmodel import select

from cpustack.bus import Event, EventType
from cpustack.db import session_scope
from cpustack.schemas.models import (
    Model,
    ModelBackend,
    ModelInstance,
    ModelInstanceState,
)
from cpustack.server.controllers.base import Reconciler

logger = logging.getLogger(__name__)

_PINNED_MARK = "用户手动停止"

# 流水线并行：replicas 表示节点数，但只创建1个实例（调度器分配多节点）
# 数据并行 / 单机 / RPC：replicas 表示实例副本数
_MULTI_NODE_BACKENDS = {ModelBackend.PRIMA_CPP}


class ModelController(Reconciler):
    """模型副本控制器：期望副本数 vs 实际实例数收敛。"""

    entity_type = "model"
    scan_interval_seconds = 180
    name = "model-controller"

    async def _on_event(self, event: Event) -> None:
        """Model 事件增量调谐：UPDATED（如 replicas 变更）触发该模型调谐。"""
        if event.event_type == EventType.DELETED:
            return
        await self._reconcile_model(event.entity_id)

    async def reconcile_all(self) -> None:
        """全量扫描：调谐所有模型。"""
        async with session_scope() as session:
            stmt = select(Model)
            models = (await session.execute(stmt)).scalars().all()
            model_ids = [m.id for m in models]

        for mid in model_ids:
            await self._reconcile_model(mid)

    async def _reconcile_model(self, model_id: int) -> None:
        """单个模型的副本调谐。"""
        async with session_scope() as session:
            model = await session.get(Model, model_id)
            if not model:
                return

            # 期望实例数：流水线并行只创建1个实例（多节点由调度器分配）
            desired = 1 if model.backend in _MULTI_NODE_BACKENDS else model.replicas

            # 查询该模型所有实例
            inst_stmt = select(ModelInstance).where(
                ModelInstance.model_id == model_id
            )
            instances = (await session.execute(inst_stmt)).scalars().all()

            # 分类
            active_states = {
                ModelInstanceState.PENDING,
                ModelInstanceState.ANALYZING,
                ModelInstanceState.SCHEDULED,
                ModelInstanceState.INITIALIZING,
                ModelInstanceState.DOWNLOADING,
                ModelInstanceState.STARTING,
                ModelInstanceState.RUNNING,
            }
            active = [i for i in instances if i.state in active_states]
            failed = [
                i for i in instances
                if i.state in (ModelInstanceState.ERROR, ModelInstanceState.UNREACHABLE)
                and _PINNED_MARK not in i.error_message
            ]
            pinned = [
                i for i in instances
                if _PINNED_MARK in i.error_message
            ]

            # 有效实例数 = 活跃实例 + 手动停止实例（手动停止的不主动清理）
            # 故障实例（ERROR/UNREACHABLE）不计入有效，缩容时优先删除
            effective = len(active) + len(pinned)
            # 总非 pinned 实例数（含故障），用于判断是否需要缩容清理
            total_non_pinned = len(active) + len(failed)

            # 扩容
            if effective < desired:
                to_create = desired - effective
                for _ in range(to_create):
                    inst = ModelInstance(
                        name=f"{model.name}-{secrets.token_hex(3)}",
                        model_id=model.id,
                        state=ModelInstanceState.PENDING,
                    )
                    session.add(inst)
                    logger.info(
                        "ModelController: 模型 %s 扩容，创建实例（期望 %d, 有效 %d）",
                        model.name, desired, effective,
                    )
                await session.commit()
                # 发布新建实例事件触发调度
                new_stmt = select(ModelInstance).where(
                    ModelInstance.model_id == model_id,
                    ModelInstance.state == ModelInstanceState.PENDING,
                )
                new_instances = (await session.execute(new_stmt)).scalars().all()
                for inst in new_instances:
                    await ModelInstance.publish_created(inst.id, {"name": inst.name})
                return

            # 缩容：优先删故障实例（ERROR/UNREACHABLE），再删最旧的 active
            if total_non_pinned > desired:
                to_delete = total_non_pinned - desired
                # 先删 failed（ERROR/UNREACHABLE），再删最旧的 active
                candidates = sorted(failed, key=lambda i: i.created_at)
                if len(candidates) < to_delete:
                    # 故障实例不够，补充最旧的 active
                    candidates += sorted(active, key=lambda i: i.created_at)[
                        : to_delete - len(candidates)
                    ]
                else:
                    candidates = candidates[:to_delete]

                for inst in candidates:
                    await session.delete(inst)
                    logger.info(
                        "ModelController: 模型 %s 缩容，删除实例 %s（状态 %s）",
                        model.name, inst.name, inst.state.value,
                    )
                await session.commit()
                return
