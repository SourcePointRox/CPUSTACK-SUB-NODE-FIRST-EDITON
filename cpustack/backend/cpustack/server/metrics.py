"""Prometheus 指标定义与采集。

指标分类：
- 集群资源：Worker 数量、CPU/内存
- 模型实例：实例数量按状态、副本数
- 推理请求：请求计数、耗时直方图

暴露端点：/metrics（由 app.py 注册）
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logger = logging.getLogger(__name__)

# ============ 集群资源指标 ============
WORKERS_TOTAL = Gauge(
    "cpustack_workers_total",
    "Worker 节点总数按状态",
    labelnames=["state"],
)

WORKER_CPU_CORES = Gauge(
    "cpustack_worker_cpu_cores",
    "Worker CPU 核心数",
    labelnames=["worker_name"],
)

WORKER_MEMORY_AVAILABLE_BYTES = Gauge(
    "cpustack_worker_memory_available_bytes",
    "Worker 可用内存（字节）",
    labelnames=["worker_name"],
)

WORKER_CPU_UTILIZATION = Gauge(
    "cpustack_worker_cpu_utilization",
    "Worker CPU 使用率",
    labelnames=["worker_name"],
)

# ============ 模型实例指标 ============
INSTANCES_TOTAL = Gauge(
    "cpustack_instances_total",
    "模型实例总数按状态",
    labelnames=["state"],
)

MODEL_REPLICAS_DESIRED = Gauge(
    "cpustack_model_replicas_desired",
    "模型期望副本数",
    labelnames=["model_name"],
)

MODEL_REPLICAS_READY = Gauge(
    "cpustack_model_replicas_ready",
    "模型就绪副本数（RUNNING）",
    labelnames=["model_name"],
)

# ============ 推理请求指标 ============
INFERENCE_REQUESTS_TOTAL = Counter(
    "cpustack_inference_requests_total",
    "推理请求总数",
    labelnames=["model", "status"],
)

INFERENCE_REQUEST_DURATION_SECONDS = Histogram(
    "cpustack_inference_request_duration_seconds",
    "推理请求耗时（秒）",
    labelnames=["model"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)


async def collect_metrics() -> None:
    """从 DB 采集集群指标，更新 Gauge。

    由周期任务（scheduler 或 controller）调用。
    """
    from sqlmodel import select

    from cpustack.db import session_scope
    from cpustack.schemas.models import (
        Model,
        ModelInstance,
        ModelInstanceState,
    )
    from cpustack.schemas.workers import Worker, WorkerState, WorkerStatus

    try:
        async with session_scope() as session:
            # Worker 指标
            w_stmt = select(Worker)
            workers = (await session.execute(w_stmt)).scalars().all()

            # 按状态统计
            state_counts: dict[str, int] = {}
            for w in workers:
                state_counts[w.state.value] = state_counts.get(w.state.value, 0) + 1
            for state, count in state_counts.items():
                WORKERS_TOTAL.labels(state=state).set(count)
            # 清零未出现的状态
            for state in ("ready", "not_ready", "unreachable"):
                if state not in state_counts:
                    WORKERS_TOTAL.labels(state=state).set(0)

            # Worker 资源详情
            for w in workers:
                ws_stmt = select(WorkerStatus).where(
                    WorkerStatus.worker_id == w.id
                )
                ws = (await session.execute(ws_stmt)).scalar_one_or_none()
                if ws:
                    WORKER_CPU_CORES.labels(worker_name=w.name).set(ws.cpu_cores)
                    WORKER_MEMORY_AVAILABLE_BYTES.labels(worker_name=w.name).set(
                        ws.memory_available * 1024 * 1024  # MB → bytes
                    )
                    WORKER_CPU_UTILIZATION.labels(worker_name=w.name).set(
                        ws.cpu_utilization
                    )

            # 实例指标
            i_stmt = select(ModelInstance)
            instances = (await session.execute(i_stmt)).scalars().all()
            inst_state_counts: dict[str, int] = {}
            for inst in instances:
                inst_state_counts[inst.state.value] = (
                    inst_state_counts.get(inst.state.value, 0) + 1
                )
            for state, count in inst_state_counts.items():
                INSTANCES_TOTAL.labels(state=state).set(count)
            # 清零所有 9 态中未出现的
            for state in (
                "pending", "analyzing", "scheduled", "initializing",
                "downloading", "starting", "running", "error", "unreachable",
            ):
                if state not in inst_state_counts:
                    INSTANCES_TOTAL.labels(state=state).set(0)

            # 模型副本指标
            m_stmt = select(Model)
            models = (await session.execute(m_stmt)).scalars().all()
            for m in models:
                MODEL_REPLICAS_DESIRED.labels(model_name=m.name).set(m.replicas)
                # 统计 RUNNING 实例数
                running_stmt = select(ModelInstance).where(
                    ModelInstance.model_id == m.id,
                    ModelInstance.state == ModelInstanceState.RUNNING,
                )
                running = (await session.execute(running_stmt)).scalars().all()
                MODEL_REPLICAS_READY.labels(model_name=m.name).set(len(running))

    except Exception:
        logger.exception("采集 Prometheus 指标异常")


def render_metrics() -> tuple[bytes, str]:
    """渲染指标为 Prometheus 文本格式（供 /metrics 端点使用）。"""
    return generate_latest(), CONTENT_TYPE_LATEST
