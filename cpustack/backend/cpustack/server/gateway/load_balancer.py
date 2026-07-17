"""负载均衡器：在多个 RUNNING 实例间分发请求。

两种策略：
- RoundRobin（轮询）：按顺序循环选择，简单公平
- LeastConnections（最少连接）：选当前活跃连接数最少的实例，适合请求耗时差异大的场景

数据并行模式下，同一模型的多个副本实例在此层完成负载均衡。
流水线/RPC 模式只有单实例，负载均衡退化为直接选择。

使用方式：
    balancer = get_load_balancer()
    instance = await balancer.select(instances)
    try:
        balancer.acquire(instance.id)
        # 转发请求到 instance
    finally:
        balancer.release(instance.id)
"""

from __future__ import annotations

import abc
import logging
from collections import defaultdict

from cpustack.schemas.models import ModelInstance

logger = logging.getLogger(__name__)


class LoadBalancer(abc.ABC):
    """负载均衡器抽象基类。"""

    def __init__(self):
        # instance_id -> 活跃连接数
        self._active_connections: dict[int, int] = defaultdict(int)

    @abc.abstractmethod
    def select(self, instances: list[ModelInstance]) -> ModelInstance | None:
        """从候选实例中选择一个。"""
        ...

    def acquire(self, instance_id: int) -> None:
        """记录一个请求开始（增加活跃连接计数）。"""
        self._active_connections[instance_id] += 1

    def release(self, instance_id: int) -> None:
        """记录一个请求结束（减少活跃连接计数）。"""
        if self._active_connections[instance_id] > 0:
            self._active_connections[instance_id] -= 1

    def active_count(self, instance_id: int) -> int:
        """查询某实例当前活跃连接数。"""
        return self._active_connections[instance_id]


class RoundRobinBalancer(LoadBalancer):
    """轮询负载均衡器。

    维护一个全局游标，按顺序循环选择实例。
    适合各副本性能相近、请求耗时均匀的场景。
    """

    def __init__(self):
        super().__init__()
        self._cursor = 0

    def select(self, instances: list[ModelInstance]) -> ModelInstance | None:
        if not instances:
            return None
        if len(instances) == 1:
            return instances[0]

        idx = self._cursor % len(instances)
        self._cursor = (self._cursor + 1) % len(instances)
        chosen = instances[idx]
        logger.debug(
            "轮询选择: 实例 %s (idx=%d/%d)",
            chosen.name, idx, len(instances),
        )
        return chosen


class LeastConnectionsBalancer(LoadBalancer):
    """最少连接负载均衡器。

    选择当前活跃连接数最少的实例。
    适合请求耗时差异大、需动态均衡负载的场景。
    并列时取第一个（轮询效果）。
    """

    def select(self, instances: list[ModelInstance]) -> ModelInstance | None:
        if not instances:
            return None
        if len(instances) == 1:
            return instances[0]

        # 按活跃连接数升序，并列时取第一个
        chosen = min(instances, key=lambda i: self._active_connections[i.id])
        logger.debug(
            "最少连接选择: 实例 %s (活跃连接=%d)",
            chosen.name, self._active_connections[chosen.id],
        )
        return chosen


# 默认负载均衡器（轮询策略）
_default_balancer: LoadBalancer | None = None


def get_load_balancer() -> LoadBalancer:
    """获取全局负载均衡器实例。

    默认使用轮询策略；可通过 set_load_balancer 替换。
    """
    global _default_balancer
    if _default_balancer is None:
        _default_balancer = RoundRobinBalancer()
    return _default_balancer


def set_load_balancer(balancer: LoadBalancer) -> None:
    """替换全局负载均衡器（用于切换策略或测试）。"""
    global _default_balancer
    _default_balancer = balancer
