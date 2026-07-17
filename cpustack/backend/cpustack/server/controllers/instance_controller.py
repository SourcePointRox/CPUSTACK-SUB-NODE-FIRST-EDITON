"""InstanceController：失败实例自动重启调谐。

职责：
- 监听 ModelInstance UPDATED 事件，当实例进入 ERROR 状态时触发重启评估
- 周期扫描所有 ERROR 实例，带退避地重置为 PENDING 触发重新调度

退避策略：
- 在 error_message 前缀记录重试次数："[retry:N] 原始错误"
- 超过 MAX_RETRIES（默认 3）次不再自动重启，需人工介入（restart API 重置计数）
- 每次重试需满足冷却时间（COOLDOWN_SECONDS），避免崩溃循环

不重启的情形：
- 手动停止的实例（error_message 含"用户手动停止"）
- 已达最大重试次数的实例
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from cpustack.bus import Event, EventType
from cpustack.db import session_scope
from cpustack.schemas.models import ModelInstance, ModelInstanceState
from cpustack.server.controllers.base import Reconciler

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_COOLDOWN_SECONDS = 60
_PINNED_MARK = "用户手动停止"
# 匹配 error_message 开头的 [retry:N] 标记
_RETRY_PATTERN = re.compile(r"^\[retry:(\d+)\]\s*")


class InstanceController(Reconciler):
    """失败实例自动重启控制器。"""

    entity_type = "model_instance"
    scan_interval_seconds = 60
    name = "instance-controller"

    async def _on_event(self, event: Event) -> None:
        """实例事件增量调谐：仅 UPDATED 且当前为 ERROR 时触发。"""
        if event.event_type != EventType.UPDATED:
            return
        # 事件 data 里可能携带 state 信息，但保险起见全量查 DB
        await self._restart_failed_instances()

    async def reconcile_all(self) -> None:
        """全量扫描：重启所有符合条件的 ERROR 实例。"""
        await self._restart_failed_instances()

    async def _restart_failed_instances(self) -> None:
        """扫描 ERROR 实例，满足退避条件则重置为 PENDING。"""
        now = datetime.now(timezone.utc)
        restarted = 0

        async with session_scope() as session:
            stmt = select(ModelInstance).where(
                ModelInstance.state == ModelInstanceState.ERROR
            )
            failed = (await session.execute(stmt)).scalars().all()

            for inst in failed:
                # 跳过手动停止的
                if _PINNED_MARK in inst.error_message:
                    continue

                # 解析当前重试次数
                retry_count = _parse_retry_count(inst.error_message)

                # 超过最大重试次数，放弃自动重启
                if retry_count >= _MAX_RETRIES:
                    logger.warning(
                        "实例 %s 已达最大重试次数 %d，不再自动重启: %s",
                        inst.name, _MAX_RETRIES, inst.error_message,
                    )
                    continue

                # 冷却检查：updated_at 距今不足 COOLDOWN_SECONDS 则跳过本轮
                # updated_at 可能是 naive datetime（DB 返回），统一处理
                updated = inst.updated_at
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                age = (now - updated).total_seconds()
                if age < _COOLDOWN_SECONDS:
                    continue

                # 重置为 PENDING，保留原错误信息到 error_message 以便追溯
                inst.state = ModelInstanceState.PENDING
                inst.worker_id = None
                inst.service_port = None
                inst.download_progress = 0.0
                new_msg = _bump_retry(inst.error_message, retry_count)
                inst.error_message = new_msg
                inst.distributed_config = "{}"
                inst.rpc_worker_ids = "[]"
                session.add(inst)
                await ModelInstance.publish_updated(
                    inst.id, {"state": "pending", "retry": retry_count + 1}
                )
                restarted += 1
                logger.info(
                    "实例 %s 自动重启（第 %d 次重试）",
                    inst.name, retry_count + 1,
                )

            if restarted:
                await session.commit()
                logger.info(
                    "InstanceController: 本轮重启 %d 个失败实例", restarted
                )


def _parse_retry_count(error_message: str) -> int:
    """从 error_message 解析已重试次数，无标记则返回 0。"""
    m = _RETRY_PATTERN.match(error_message)
    return int(m.group(1)) if m else 0


def _bump_retry(error_message: str, current_count: int) -> str:
    """递增重试计数标记，剥离旧标记后重新加前缀。"""
    # 剥离旧的 [retry:N] 前缀
    stripped = _RETRY_PATTERN.sub("", error_message)
    return f"[retry:{current_count + 1}] {stripped}".strip()


def reset_retry_count(error_message: str) -> str:
    """清除重试计数标记（手动 restart 调用时使用）。"""
    return _RETRY_PATTERN.sub("", error_message).strip()
