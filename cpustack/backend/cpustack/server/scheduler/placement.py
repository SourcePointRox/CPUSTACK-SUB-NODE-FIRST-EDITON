"""放置打分器：SPREAD/BINPACK + 指令集优先级 + 网络感知。

支持阶段5资源调度优化（规划文档 §11.3）：
- SPREAD：跨 Worker 分散，最大化可用性
- BINPACK：打包到同 Worker，腾出大内存节点
- 指令集优先级：AVX-512/AMX 节点优先（性能提升 23%+）
- 网络感知：流水线并行优先高带宽节点（降低流水线气泡）

策略可通过 Model.backend_parameters 覆盖：
    {"placement_strategy": "spread"}   # 或 "binpack"
"""

from __future__ import annotations

import logging
import math
from typing import Any

from cpustack.schemas.models import Model, ModelBackend
from cpustack.schemas.workers import Worker, WorkerStatus, get_instruction_sets

logger = logging.getLogger(__name__)

# 指令集优先级权重（越高越优先）
# 规划文档 §2.3：AVX-512 较 AVX2 提升 23%+，AMX 有革命性提升
_INSTRUCTION_SET_WEIGHTS: dict[str, int] = {
    "AMX": 100,        # Intel 至强 SPR+，革命性提升
    "AVX-512": 50,     # 较 AVX2 提升 23%+
    "AVX-VNNI": 30,
    "AVX2": 10,        # 基准
}

# 流水线并行的网络带宽阈值（Mbps）
# 规划文档 §2.4：1Gbps 绝对不足，10GbE 是起步门槛
_PIPELINE_NETWORK_WARN = 10000


def _instruction_set_score(status: WorkerStatus) -> int:
    """指令集优先级打分：AVX-512/AMX 节点得分更高。"""
    sets = set(get_instruction_sets(status))
    if not sets:
        return 0
    return max(_INSTRUCTION_SET_WEIGHTS.get(s, 0) for s in sets)


def _network_score(status: WorkerStatus) -> int:
    """网络带宽打分：带宽越高得分越高（对数缩放）。

    100Mbps≈46, 1000Mbps≈69, 10000Mbps≈92
    """
    bw = status.network_bandwidth or 0
    if bw <= 0:
        return 0
    return int(math.log10(bw) * 23)


def score_placement(
    candidates: list[tuple[Worker, WorkerStatus]],
    strategy: str = "spread",
    model: Model | None = None,
    backend_parameters: dict | None = None,
) -> tuple[Worker, WorkerStatus] | None:
    """放置打分：综合 SPREAD/BINPACK + 指令集优先级 + 网络感知。

    排序优先级（高到低）：
        1. 指令集能力（AMX > AVX-512 > AVX2）—— 性能分水岭
        2. 网络带宽（仅流水线并行）—— 降低流水线气泡
        3. 内存分配策略（SPREAD/BINPACK）—— 负载均衡/打包

    Args:
        candidates: 候选节点列表
        strategy: 默认放置策略（spread/binpack）
        model: 模型对象（用于判断后端类型，应用网络感知）
        backend_parameters: 后端参数（可覆盖 strategy）

    Returns:
        最优节点，无候选返回 None
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    params = backend_parameters or {}
    effective_strategy = params.get("placement_strategy", strategy)
    if effective_strategy not in {"spread", "binpack"}:
        effective_strategy = strategy

    is_pipeline = model is not None and model.backend == ModelBackend.PRIMA_CPP

    def composite_score(item: tuple[Worker, WorkerStatus]) -> tuple[int, int, int]:
        _, s = item
        # 1. 指令集维度
        is_score = _instruction_set_score(s)
        # 2. 网络维度（仅流水线并行）
        net_score = _network_score(s) if is_pipeline else 0
        # 3. 内存维度
        if effective_strategy == "binpack":
            mem_score = s.memory_allocated  # 打包：已分配多的优先
        else:
            mem_score = -s.memory_allocated  # spread：已分配少的优先
        return (is_score, net_score, mem_score)

    sorted_candidates = sorted(candidates, key=composite_score, reverse=True)
    return sorted_candidates[0]


def select_pipeline_nodes(
    candidates: list[tuple[Worker, WorkerStatus]],
    node_count: int,
    model: Model | None = None,
) -> list[tuple[Worker, WorkerStatus]]:
    """流水线并行节点选择：按指令集 + 网络带宽 + CPU 核心综合打分。

    流水线并行对网络延迟敏感（规划文档 §2.4），需选择高带宽、高性能节点组合。
    排序优先级：指令集 > 网络带宽 > CPU 核心数。

    Args:
        candidates: 候选节点（已通过基础过滤）
        node_count: 需要的节点数
        model: 模型对象（用于网络带宽警告）

    Returns:
        选中的节点列表（按打分降序）
    """
    if not candidates or node_count <= 0:
        return []

    def pipeline_score(item: tuple[Worker, WorkerStatus]) -> tuple[int, int, int]:
        _, s = item
        is_score = _instruction_set_score(s)
        net_score = _network_score(s)
        cpu_score = s.cpu_cores  # 核心多的优先处理更多层
        return (is_score, net_score, cpu_score)

    sorted_candidates = sorted(candidates, key=pipeline_score, reverse=True)
    selected = sorted_candidates[:node_count]

    # 网络带宽警告
    if model is not None and model.backend == ModelBackend.PRIMA_CPP:
        for w, s in selected:
            if s.network_bandwidth < _PIPELINE_NETWORK_WARN:
                logger.warning(
                    "Worker %s 网络带宽 %dMbps 低于流水线并行推荐阈值 %dMbps，"
                    "可能出现性能崩塌",
                    w.name, s.network_bandwidth, _PIPELINE_NETWORK_WARN,
                )

    return selected


def select_rpc_master(
    candidates: list[tuple[Worker, WorkerStatus]],
) -> tuple[Worker, WorkerStatus] | None:
    """RPC Master 节点选择：优先指令集能力强 + 可用内存大。

    RPC 模式 Master 负责加载模型主体，需要较大的本地内存和强指令集。
    （Slave 节点的选择按可用内存降序即可，本函数仅用于 Master 选择）

    Args:
        candidates: 候选节点（已通过指令集过滤）

    Returns:
        最优 Master 节点，无候选返回 None
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def master_score(item: tuple[Worker, WorkerStatus]) -> tuple[int, int]:
        _, s = item
        is_score = _instruction_set_score(s)
        mem_score = s.memory_available  # 内存大的优先
        return (is_score, mem_score)

    sorted_candidates = sorted(candidates, key=master_score, reverse=True)
    return sorted_candidates[0]
