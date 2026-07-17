"""调度过滤器链：CPU 指令集 + 内存适配过滤。

CPUSTACK 特有过滤器：
- InstructionSetFilter：AVX-512 模型只能调度到支持的节点
- MemoryFitFilter：模型必须能装入节点/集群内存
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

from cpustack.schemas.models import Model, ModelInstance, ModelBackend
from cpustack.schemas.workers import Worker, WorkerState, WorkerStatus, get_instruction_sets

logger = logging.getLogger(__name__)


class WorkerFilter(ABC):
    """调度过滤器抽象基类。"""

    @abstractmethod
    async def filter(
        self, model: Model, instance: ModelInstance, workers: list[tuple[Worker, WorkerStatus]]
    ) -> list[tuple[Worker, WorkerStatus]]:
        """过滤候选 Worker 列表。"""
        ...


class StatusFilter(WorkerFilter):
    """过滤：仅保留 READY 状态的 Worker。"""

    async def filter(self, model, instance, workers):
        return [(w, s) for w, s in workers if w.state == WorkerState.READY]


class InstructionSetFilter(WorkerFilter):
    """CPU 特有：指令集匹配过滤。

    AVX-512 优化的模型不能调度到仅支持 AVX2 的节点。
    """

    async def filter(self, model, instance, workers):
        try:
            required = json.loads(model.required_instruction_sets)
        except (json.JSONDecodeError, TypeError):
            required = []

        if not required:
            return workers

        result = []
        for w, s in workers:
            supported = set(get_instruction_sets(s))
            if all(req in supported for req in required):
                result.append((w, s))
        return result


class MemoryFitFilter(WorkerFilter):
    """CPU 特有：内存适配过滤。

    单机模式：节点可用内存 >= 模型需求
    RPC 模式：主节点+工作节点内存总和 >= 模型需求（此过滤器仅做单机预筛）
    """

    async def filter(self, model, instance, workers):
        if model.estimated_memory <= 0:
            return workers

        # 系统预留内存（512MB）
        reserved = 512

        if model.backend == ModelBackend.LLAMA_CPP_RPC:
            # RPC 模式：只要有内存的节点都可候选（后续聚合判断）
            return [(w, s) for w, s in workers if s.memory_available > reserved]
        else:
            # 单机/流水线/数据并行：节点内存需满足需求
            return [
                (w, s)
                for w, s in workers
                if s.memory_available >= model.estimated_memory + reserved
            ]


class LabelMatchingFilter(WorkerFilter):
    """标签匹配过滤。"""

    async def filter(self, model, instance, workers):
        # 初期不实现标签匹配，返回全部
        return workers


class WorkerFilterChain:
    """过滤器链：按顺序应用所有过滤器。"""

    def __init__(self):
        self._filters: list[WorkerFilter] = [
            StatusFilter(),
            InstructionSetFilter(),
            MemoryFitFilter(),
            LabelMatchingFilter(),
        ]

    async def apply(
        self, model: Model, instance: ModelInstance, workers: list[tuple[Worker, WorkerStatus]]
    ) -> list[tuple[Worker, WorkerStatus]]:
        result = workers
        for f in self._filters:
            result = await f.filter(model, instance, result)
            if not result:
                break
        return result
