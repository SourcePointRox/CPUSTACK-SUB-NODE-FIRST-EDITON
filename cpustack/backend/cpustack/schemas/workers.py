"""Worker 节点模型与状态。

CPU 特有字段：指令集支持、CPU 核心、内存、NUMA 拓扑。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlmodel import Field

from cpustack.schemas.common import ActiveRecordMixin, TimestampMixin


class WorkerState(str, Enum):
    NOT_READY = "not_ready"
    READY = "ready"
    UNREACHABLE = "unreachable"


class Worker(TimestampMixin, ActiveRecordMixin, table=True):
    __tablename__ = "workers"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True, nullable=False, max_length=128)
    uuid: str = Field(unique=True, index=True, nullable=False, max_length=64)
    # 注册凭证
    api_key: str = Field(nullable=False, max_length=128)
    # 网络地址
    ip: str = Field(nullable=False, max_length=64)
    port: int = Field(default=30080, nullable=False)
    # 标签（JSON 字符串，用于调度）
    labels: str | None = Field(default=None, max_length=2048)
    state: WorkerState = Field(default=WorkerState.NOT_READY, nullable=False, index=True)
    # 心跳时间
    heartbeat_at: datetime | None = Field(default=None, nullable=True)


class WorkerStatus(TimestampMixin, table=True):
    """Worker 资源状态（每次心跳更新）。

    CPU 特有：指令集支持是调度的关键依据。
    """

    __tablename__ = "worker_statuses"

    id: int | None = Field(default=None, primary_key=True)
    worker_id: int = Field(foreign_key="workers.id", nullable=False, index=True, unique=True)

    # CPU 信息
    cpu_arch: str = Field(default="x86_64", nullable=False, max_length=32)
    cpu_model: str = Field(default="", nullable=False, max_length=256)
    cpu_cores: int = Field(default=0, nullable=False)
    cpu_allocated: int = Field(default=0, nullable=False)
    cpu_utilization: float = Field(default=0.0, nullable=False)

    # 指令集支持（JSON 数组字符串，如 '["AVX2","AVX-512","AMX"]'）
    instruction_sets: str = Field(default="[]", nullable=False, max_length=256)

    # NUMA
    numa_nodes: int = Field(default=1, nullable=False)

    # 内存信息（MB）
    memory_total: int = Field(default=0, nullable=False)
    memory_allocated: int = Field(default=0, nullable=False)
    memory_available: int = Field(default=0, nullable=False)

    # Swap（MB）
    swap_total: int = Field(default=0, nullable=False)
    swap_used: int = Field(default=0, nullable=False)

    # 磁盘（GB）
    disk_total: int = Field(default=0, nullable=False)
    disk_available: int = Field(default=0, nullable=False)

    # 网络（Mbps）
    network_bandwidth: int = Field(default=1000, nullable=False)

    # OS 信息
    os: str = Field(default="", nullable=False, max_length=128)
    kernel: str = Field(default="", nullable=False, max_length=128)


def get_instruction_sets(status: WorkerStatus) -> list[str]:
    """解析指令集 JSON 字符串。"""
    import json

    try:
        return json.loads(status.instruction_sets)
    except (json.JSONDecodeError, TypeError):
        return []
