"""调度器主模块：事件驱动 + 周期巡检双机制。

订阅 ModelInstance.CREATED 事件 + 每 N 秒扫描 PENDING 实例。
调度三阶段：资源评估 → 候选选择(Filter Chain) → 放置打分(SPREAD/BINPACK)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from cpustack.bus import Event, EventBus, EventType, get_event_bus
from cpustack.config import settings
from cpustack.db import session_scope
from cpustack.schemas.models import Model, ModelInstance, ModelInstanceState, ModelBackend
from cpustack.schemas.workers import Worker, WorkerState, WorkerStatus
from cpustack.server.scheduler.filters import WorkerFilterChain
from cpustack.server.scheduler.placement import (
    score_placement,
    select_pipeline_nodes,
    select_rpc_master,
)

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
_filter_chain = WorkerFilterChain()


async def _score_placement(
    candidates: list[tuple[Worker, WorkerStatus]],
    strategy: str = "spread",
    model: Model | None = None,
    backend_parameters: dict | None = None,
) -> tuple[Worker, WorkerStatus] | None:
    """放置打分（阶段5优化：接入 placement 模块）。

    综合维度：指令集优先级 > 网络带宽（流水线）> SPREAD/BINPACK 内存策略。
    策略可通过 backend_parameters.placement_strategy 覆盖。
    """
    return score_placement(
        candidates,
        strategy=strategy,
        model=model,
        backend_parameters=backend_parameters,
    )


def _parse_instruction_sets(raw: str | None) -> set[str]:
    """解析指令集 JSON 字符串为集合。"""
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return set()


async def _schedule_rpc_instance(
    instance: ModelInstance,
    model: Model,
    workers: list[tuple[Worker, WorkerStatus]],
    session,
) -> bool:
    """RPC 模式调度：跨节点内存池化。

    策略：
    1. 筛选满足指令集要求的 READY 节点
    2. 按可用内存降序排序
    3. 若单节点内存足够 → 单节点部署（无需 RPC）
    4. 否则 → Master(内存最大) + Slaves 聚合内存
    """
    required_sets = _parse_instruction_sets(model.required_instruction_sets)

    # 筛选满足指令集要求的 READY 节点
    eligible = [
        (w, s)
        for w, s in workers
        if w.state == WorkerState.READY
        and required_sets.issubset(_parse_instruction_sets(s.instruction_sets))
    ]

    if not eligible:
        instance.state = ModelInstanceState.PENDING
        instance.error_message = "无满足指令集要求的节点"
        session.add(instance)
        await session.commit()
        return False

    # 按可用内存降序
    eligible.sort(key=lambda x: x[1].memory_available, reverse=True)

    # 内存预估：大型模型 KV cache + 激活开销更大，按 20% 预留（最少 512MB）
    memory_needed = model.estimated_memory + max(
        512, int(model.estimated_memory * 0.20)
    )

    # 检查单节点是否足够
    if eligible[0][1].memory_available >= memory_needed:
        master_w, master_s = eligible[0]
        instance.worker_id = master_w.id
        instance.rpc_worker_ids = "[]"
        instance.state = ModelInstanceState.SCHEDULED
        instance.allocated_memory = model.estimated_memory
        instance.allocated_cpu_cores = min(master_s.cpu_cores, 4)
        instance.error_message = ""
        master_s.memory_allocated += model.estimated_memory
        master_s.cpu_allocated += instance.allocated_cpu_cores
        session.add(instance)
        session.add(master_s)
        await session.commit()
        logger.info(
            "RPC 实例 %s 单节点部署到 %s (内存 %dMB 足够)",
            instance.name, master_w.name, master_s.memory_available,
        )
        await ModelInstance.publish_updated(
            instance.id, {"state": "scheduled", "worker_id": master_w.id}
        )
        return True

    # 多节点聚合：Master + Slaves
    # Slave 数量上限 8（过多节点会导致 RPC 通信开销过大，反而降低吞吐）
    MAX_RPC_SLAVES = 8
    master_w, master_s = eligible[0]
    slaves: list[tuple[Worker, WorkerStatus]] = []
    total_memory = master_s.memory_available

    for w, s in eligible[1:]:
        if total_memory >= memory_needed:
            break
        if len(slaves) >= MAX_RPC_SLAVES:
            logger.warning(
                "RPC 实例 %s 已达 Slave 上限 %d，停止聚合",
                instance.name, MAX_RPC_SLAVES,
            )
            break
        slaves.append((w, s))
        total_memory += s.memory_available

    if total_memory < memory_needed:
        instance.state = ModelInstanceState.PENDING
        instance.error_message = (
            f"集群总内存不足: 需要 {memory_needed}MB, "
            f"可用 {total_memory}MB (共 {1 + len(slaves)} 节点)"
        )
        session.add(instance)
        await session.commit()
        logger.warning(
            "RPC 实例 %s 内存不足: 需 %dMB, 可用 %dMB",
            instance.name, memory_needed, total_memory,
        )
        return False

    # 分配资源
    slave_ids = [w.id for w, _ in slaves]
    instance.worker_id = master_w.id
    instance.rpc_worker_ids = json.dumps(slave_ids)
    instance.state = ModelInstanceState.SCHEDULED
    instance.allocated_memory = model.estimated_memory
    instance.allocated_cpu_cores = min(master_s.cpu_cores, 4)
    instance.error_message = ""

    # 更新各节点内存分配
    master_contribution = min(master_s.memory_available, model.estimated_memory)
    master_s.memory_allocated += master_contribution
    master_s.cpu_allocated += instance.allocated_cpu_cores
    session.add(master_s)

    remaining = model.estimated_memory - master_contribution
    for w, s in slaves:
        contrib = min(s.memory_available, remaining)
        s.memory_allocated += contrib
        remaining -= contrib
        session.add(s)

    session.add(instance)
    await session.commit()

    logger.info(
        "RPC 实例 %s 多节点部署: Master=%s + %d Slaves=%s (总内存 %dMB)",
        instance.name, master_w.name, len(slaves),
        [w.name for w, _ in slaves], total_memory,
    )
    await ModelInstance.publish_updated(
        instance.id, {"state": "scheduled", "worker_id": master_w.id}
    )
    return True


async def _schedule_prima_instance(
    instance: ModelInstance,
    model: Model,
    workers: list[tuple[Worker, WorkerStatus]],
    session,
) -> bool:
    """流水线并行调度：按层切片分配到多节点。

    策略：
    1. 筛选满足指令集 + 内存的 READY 节点（每节点需装载完整模型）
    2. 按网络延迟/CPU 核心数选择 N 个节点（N = model.replicas，至少2）
    3. 用 compute_layer_split 按核心比例分配层段
    4. Master(第一节点) + Slaves(后续节点)，层分配存入 distributed_config
    """
    from cpustack.worker.backends.prima_cpp import compute_layer_split

    required_sets = _parse_instruction_sets(model.required_instruction_sets)
    memory_needed = model.estimated_memory + 512  # 512MB 预留

    # 筛选满足指令集 + 内存（每节点需装载完整模型）的 READY 节点
    eligible = [
        (w, s)
        for w, s in workers
        if w.state == WorkerState.READY
        and required_sets.issubset(_parse_instruction_sets(s.instruction_sets))
        and s.memory_available >= memory_needed
    ]

    if not eligible:
        instance.state = ModelInstanceState.PENDING
        instance.error_message = "无满足指令集/内存要求的节点（流水线每节点需装载完整模型）"
        session.add(instance)
        await session.commit()
        return False

    # 流水线节点数：model.replicas（至少2），最多 eligible 数量
    pipeline_nodes = max(2, model.replicas)
    if pipeline_nodes > len(eligible):
        pipeline_nodes = len(eligible)

    # 阶段5优化：节点选择综合 指令集 + 网络带宽 + CPU 核心
    # 流水线并行对网络延迟敏感，优先选择高带宽、强指令集节点
    selected = select_pipeline_nodes(eligible, pipeline_nodes, model=model)

    # 单节点情况：退化为单机（无流水线加速）
    if len(selected) == 1:
        master_w, master_s = selected[0]
        instance.worker_id = master_w.id
        instance.rpc_worker_ids = "[]"
        instance.state = ModelInstanceState.SCHEDULED
        instance.allocated_memory = model.estimated_memory
        instance.allocated_cpu_cores = min(master_s.cpu_cores, 4)
        instance.error_message = ""
        instance.distributed_config = json.dumps({"pipeline": "single"})
        master_s.memory_allocated += model.estimated_memory
        master_s.cpu_allocated += instance.allocated_cpu_cores
        session.add(instance)
        session.add(master_s)
        await session.commit()
        logger.info("流水线实例 %s 退化为单节点部署到 %s", instance.name, master_w.name)
        await ModelInstance.publish_updated(
            instance.id, {"state": "scheduled", "worker_id": master_w.id}
        )
        return True

    # 多节点流水线：计算层切片分配
    # 模型总层数从 backend_parameters 获取，默认 32（常见 7B 模型）
    params = _parse_json(model.backend_parameters, {})
    total_layers = params.get("total_layers", 32)

    node_caps = [(w.id, s.cpu_cores) for w, s in selected]
    layer_split = compute_layer_split(total_layers, node_caps)

    # Master = 第一个节点，Slaves = 其余
    master_w, master_s = selected[0]
    slave_ids = [w.id for w, _ in selected[1:]]

    # 构建 distributed_config（含层分配和节点网络信息）
    pipeline_config = []
    for i, (w, s) in enumerate(selected):
        split = layer_split[i]
        pipeline_config.append({
            "worker_id": w.id,
            "worker_name": w.name,
            "ip": w.ip,
            "port": 50000 + w.id,  # 复用 RPC 端口约定
            "layer_start": split["layer_start"],
            "layer_end": split["layer_end"],
            "rank": split["rank"],
            "cpu_cores": s.cpu_cores,
        })

    instance.worker_id = master_w.id
    instance.rpc_worker_ids = json.dumps(slave_ids)
    instance.state = ModelInstanceState.SCHEDULED
    instance.allocated_memory = model.estimated_memory
    instance.allocated_cpu_cores = min(master_s.cpu_cores, 4)
    instance.error_message = ""
    instance.distributed_config = json.dumps({"pipeline": pipeline_config})

    # 更新各节点内存分配（每节点装载完整模型）
    for w, s in selected:
        s.memory_allocated += model.estimated_memory
        if w.id == master_w.id:
            s.cpu_allocated += instance.allocated_cpu_cores
        session.add(s)

    session.add(instance)
    await session.commit()

    logger.info(
        "流水线实例 %s 多节点部署: Master=%s + %d Workers, 总层数 %d, 分配=%s",
        instance.name, master_w.name, len(slave_ids), total_layers,
        [(c["worker_name"], f"{c['layer_start']}-{c['layer_end']}") for c in pipeline_config],
    )
    await ModelInstance.publish_updated(
        instance.id, {"state": "scheduled", "worker_id": master_w.id}
    )
    return True


async def _try_upgrade_to_rpc(
    instance: ModelInstance,
    model: Model,
    workers: list[tuple[Worker, WorkerStatus]],
    session,
) -> bool:
    """大模型自动降级：单机/数据并行内存不足时，升级到 RPC 内存池化。

    触发条件：
    1. 模型后端为 standalone 或 data_parallel
    2. 存在 READY 节点满足指令集要求（说明失败原因是内存而非指令集）
    3. 集群 READY 节点总可用内存 >= 模型内存 + 预留（RPC 聚合可装下）

    降级操作：将 model.backend 改为 llama_cpp_rpc 并重新调度。
    """
    # 仅对单机/数据并行后端尝试降级
    if model.backend not in (ModelBackend.LLAMA_CPP_STANDALONE, ModelBackend.DATA_PARALLEL):
        return False

    required_sets = _parse_instruction_sets(model.required_instruction_sets)

    # 筛选满足指令集要求的 READY 节点
    eligible = [
        (w, s)
        for w, s in workers
        if w.state == WorkerState.READY
        and required_sets.issubset(_parse_instruction_sets(s.instruction_sets))
    ]

    if not eligible:
        # 指令集都不满足，无法降级
        return False

    # 计算集群总可用内存
    total_available = sum(s.memory_available for _, s in eligible)
    memory_needed = model.estimated_memory + max(
        512, int(model.estimated_memory * 0.20)
    )

    if total_available < memory_needed:
        # 即使聚合也装不下，无法降级
        instance.state = ModelInstanceState.PENDING
        instance.error_message = (
            f"集群总内存不足: 模型需 {memory_needed}MB，"
            f"集群可用 {total_available}MB（共 {len(eligible)} 节点）。"
            f"建议增加节点或使用更小量化的模型。"
        )
        session.add(instance)
        await session.commit()
        logger.warning(
            "实例 %s 集群内存不足: 需 %dMB, 可用 %dMB",
            instance.name, memory_needed, total_available,
        )
        return True  # 已处理（设置错误信息），返回 True 停止后续调度

    # 满足降级条件：升级到 RPC
    logger.info(
        "实例 %s 自动降级: %s -> llama_cpp_rpc (单机内存不足，集群可用 %dMB)",
        instance.name, model.backend.value, total_available,
    )
    model.backend = ModelBackend.LLAMA_CPP_RPC
    session.add(model)
    await session.commit()

    # 用 RPC 调度
    await _schedule_rpc_instance(instance, model, workers, session)
    return True


def _parse_json(raw: str | None, default):
    """解析 JSON 字符串，失败返回默认值。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def _schedule_instance(instance_id: int) -> None:
    """调度单个 PENDING 实例。"""
    async with session_scope() as session:
        # 获取实例
        instance = await session.get(ModelInstance, instance_id)
        if not instance or instance.state != ModelInstanceState.PENDING:
            return

        # 获取模型
        model = await session.get(Model, instance.model_id)
        if not model:
            instance.state = ModelInstanceState.ERROR
            instance.error_message = "模型不存在"
            session.add(instance)
            await session.commit()
            return

        # 设置分析状态
        instance.state = ModelInstanceState.ANALYZING
        session.add(instance)
        await session.commit()

        # 获取所有 Worker + 状态
        workers_stmt = select(Worker, WorkerStatus).where(
            Worker.id == WorkerStatus.worker_id
        )
        workers = (await session.execute(workers_stmt)).all()
        worker_list = [(w, s) for w, s in workers]

        # RPC 模式：多节点内存池化调度
        if model.backend == ModelBackend.LLAMA_CPP_RPC:
            await _schedule_rpc_instance(instance, model, worker_list, session)
            return

        # 流水线并行：按层切片分配多节点
        if model.backend == ModelBackend.PRIMA_CPP:
            await _schedule_prima_instance(instance, model, worker_list, session)
            return

        # 数据并行 & 单机模式：Filter Chain 过滤
        # 数据并行下每个实例独立调度（模型创建时已按 replicas 生成 N 个实例），
        # SPREAD 策略使副本分散到不同节点，网关层在 RUNNING 实例间负载均衡。
        candidates = await _filter_chain.apply(model, instance, worker_list)

        if not candidates:
            # 大模型自动降级：单机/数据并行内存不足时，尝试升级到 RPC 池化
            if await _try_upgrade_to_rpc(instance, model, worker_list, session):
                return
            instance.state = ModelInstanceState.PENDING
            instance.error_message = "无满足条件的节点（指令集或内存不匹配）"
            session.add(instance)
            await session.commit()
            logger.warning(
                "实例 %s 无候选节点: 模型 %s 需 %dMB 内存",
                instance.name,
                model.name,
                model.estimated_memory,
            )
            return

        # 放置打分（阶段5优化：传入 model + backend_parameters，支持指令集优先级与策略覆盖）
        params = _parse_json(model.backend_parameters, {})
        chosen = await _score_placement(
            candidates,
            strategy="spread",
            model=model,
            backend_parameters=params,
        )
        if not chosen:
            instance.state = ModelInstanceState.PENDING
            session.add(instance)
            await session.commit()
            return

        worker, status = chosen

        # 分配资源
        instance.worker_id = worker.id
        instance.state = ModelInstanceState.SCHEDULED
        instance.allocated_memory = model.estimated_memory
        instance.allocated_cpu_cores = min(status.cpu_cores, 4)  # 默认分配 4 核
        instance.error_message = ""

        # 更新 Worker 已分配资源
        status.memory_allocated += model.estimated_memory
        status.cpu_allocated += instance.allocated_cpu_cores

        session.add(instance)
        session.add(status)
        await session.commit()

        logger.info(
            "实例 %s 已调度到节点 %s (内存 %dMB, CPU %d 核)",
            instance.name,
            worker.name,
            instance.allocated_memory,
            instance.allocated_cpu_cores,
        )

        # 发布事件，触发 Worker 侧启动
        await ModelInstance.publish_updated(
            instance.id, {"state": "scheduled", "worker_id": worker.id}
        )


async def _on_instance_created(event: Event) -> None:
    """事件回调：ModelInstance.CREATED → 触发调度。"""
    await _schedule_instance(event.entity_id)


async def _scan_pending() -> None:
    """周期巡检：调度所有 PENDING 实例。"""
    try:
        async with session_scope() as session:
            stmt = select(ModelInstance).where(
                ModelInstance.state == ModelInstanceState.PENDING
            )
            pending = (await session.execute(stmt)).scalars().all()

        for inst in pending:
            await _schedule_instance(inst.id)
    except Exception:
        logger.exception("周期巡检调度异常")


async def _check_worker_heartbeats() -> None:
    """检查 Worker 心跳：过期的标记为 UNREACHABLE。"""
    try:
        async with session_scope() as session:
            stmt = select(Worker).where(Worker.state == WorkerState.READY)
            workers = (await session.execute(stmt)).scalars().all()

            now = datetime.now(timezone.utc)
            timeout = settings.worker_heartbeat_timeout_seconds

            for w in workers:
                if w.heartbeat_at:
                    # SQLite 不保留时区信息，heartbeat_at 可能是 naive；
                    # 统一补成 aware(UTC) 再与 now(UTC) 相减，避免 naive/aware 混用报错
                    hb = w.heartbeat_at
                    if hb.tzinfo is None:
                        hb = hb.replace(tzinfo=timezone.utc)
                    age = (now - hb).total_seconds()
                    if age > timeout:
                        w.state = WorkerState.UNREACHABLE
                        session.add(w)
                        logger.warning("Worker %s 心跳超时(%ds)，标记为 UNREACHABLE", w.name, int(age))

                        # 标记该 Worker 上的实例为 UNREACHABLE
                        inst_stmt = select(ModelInstance).where(
                            ModelInstance.worker_id == w.id,
                            ModelInstance.state.in_([
                                ModelInstanceState.RUNNING,
                                ModelInstanceState.STARTING,
                                ModelInstanceState.DOWNLOADING,
                            ]),
                        )
                        instances = (await session.execute(inst_stmt)).scalars().all()
                        for inst in instances:
                            inst.state = ModelInstanceState.UNREACHABLE
                            inst.error_message = f"Worker {w.name} 心跳超时"
                            session.add(inst)
                            await ModelInstance.publish_updated(inst.id, {"state": "unreachable"})
            await session.commit()
    except Exception:
        logger.exception("Worker 心跳检查异常")


async def start_scheduler() -> None:
    """启动调度器：订阅事件 + 周期任务。"""
    global _scheduler

    # 订阅 ModelInstance.CREATED 事件
    bus = get_event_bus()
    bus.subscribe("model_instance", _on_instance_created)

    # 启动周期调度器
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _scan_pending,
        IntervalTrigger(seconds=settings.scheduler_interval_seconds),
        id="scan_pending",
        replace_existing=True,
    )
    _scheduler.add_job(
        _check_worker_heartbeats,
        IntervalTrigger(seconds=60),
        id="check_heartbeats",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("调度器已启动 (巡检间隔 %ds)", settings.scheduler_interval_seconds)


async def stop_scheduler() -> None:
    """停止调度器。"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
