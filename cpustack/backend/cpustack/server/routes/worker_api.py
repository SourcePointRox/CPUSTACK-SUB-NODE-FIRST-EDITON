"""Worker 专用 API：查询已分配实例、上报实例状态。

Worker 通过 worker_uuid + api_key 认证（非 JWT）。
支持 RPC 模式：Master 和 Slave 查询各自的角色和协调信息。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from cpustack.db import get_session
from cpustack.schemas.models import Model, ModelInstance, ModelInstanceState
from cpustack.schemas.workers import Worker, WorkerStatus

logger = logging.getLogger(__name__)
router = APIRouter()


async def _verify_worker(
    x_worker_uuid: str = Header(..., alias="X-Worker-UUID"),
    x_worker_key: str = Header(..., alias="X-Worker-Key"),
    session=Depends(get_session),
) -> Worker:
    """验证 Worker 凭证。"""
    stmt = select(Worker).where(Worker.uuid == x_worker_uuid)
    worker = (await session.execute(stmt)).scalar_one_or_none()
    if not worker or worker.api_key != x_worker_key:
        raise HTTPException(status_code=403, detail="无效的 Worker 凭证")
    return worker


class RpcSlaveInfo(BaseModel):
    """RPC Slave 节点信息（供 Master 使用）。"""

    worker_id: int
    worker_name: str
    ip: str
    rpc_port: int | None = None  # Slave 上报的 rpc-server 端口
    ready: bool = False


class PipelineWorkerInfo(BaseModel):
    """流水线并行 Worker 节点信息（供 Master 使用）。"""

    worker_id: int
    worker_name: str
    ip: str
    port: int  # prima-server 监听端口
    layer_start: int
    layer_end: int
    rank: int
    cpu_cores: int = 0
    ready: bool = False


class AssignedInstance(BaseModel):
    """分配给 Worker 的实例信息（含模型下载信息和分布式角色）。"""

    id: int
    name: str
    model_id: int
    state: str
    allocated_cpu_cores: int
    allocated_memory: int

    # 模型下载信息
    source_repo: str
    source_model_id: str
    source_filename: str
    backend: str
    backend_parameters: dict

    # RPC 角色信息（llama_cpp_rpc 后端）
    rpc_role: str = ""  # "master" | "slave" | ""
    rpc_slaves: list[RpcSlaveInfo] = []  # Master: Slave 节点列表
    rpc_master_ip: str = ""  # Slave: Master 节点 IP

    # 流水线并行角色信息（prima_cpp 后端）
    pipeline_role: str = ""  # "master" | "worker" | ""
    pipeline_workers: list[PipelineWorkerInfo] = []  # Master: 后续 Worker 节点列表
    pipeline_master_ip: str = ""  # Worker: Master 节点 IP
    pipeline_master_port: int = 0  # Worker: Master 服务端口
    layer_start: int = -1  # Worker: 本节点负责的起始层
    layer_end: int = -1  # Worker: 本节点负责的结束层
    rank: int = -1  # Worker: 流水线序号


class InstanceStateUpdate(BaseModel):
    """Worker 上报的实例状态变更。"""

    state: ModelInstanceState
    error_message: str = ""
    service_port: int | None = None
    download_progress: float = 0.0


def _parse_json(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def _build_assigned_instance(
    inst: ModelInstance,
    model: Model,
    worker: Worker,
    session,
) -> AssignedInstance:
    """构造分配给 Worker 的实例信息（含 RPC 和流水线角色）。"""
    params = _parse_json(model.backend_parameters, {})
    rpc_ids = _parse_json(inst.rpc_worker_ids, [])
    dist_cfg = _parse_json(inst.distributed_config, {})

    rpc_role = ""
    rpc_slaves: list[RpcSlaveInfo] = []
    rpc_master_ip = ""

    pipeline_role = ""
    pipeline_workers: list[PipelineWorkerInfo] = []
    pipeline_master_ip = ""
    pipeline_master_port = 0
    layer_start = -1
    layer_end = -1
    rank = -1

    # 判断 RPC 角色（llama_cpp_rpc 后端）
    if model.backend == "llama_cpp_rpc":
        if inst.worker_id == worker.id and rpc_ids:
            # Master 角色：查询 Slave 节点信息
            rpc_role = "master"
            for slave_id in rpc_ids:
                slave_w = await session.get(Worker, slave_id)
                if slave_w:
                    # RPC 端口约定：50000 + worker_id
                    rpc_port = 50000 + slave_w.id
                    rpc_slaves.append(
                        RpcSlaveInfo(
                            worker_id=slave_w.id,
                            worker_name=slave_w.name,
                            ip=slave_w.ip,
                            rpc_port=rpc_port,
                            ready=True,
                        )
                    )
        elif worker.id in rpc_ids:
            # Slave 角色
            rpc_role = "slave"
            master_w = await session.get(Worker, inst.worker_id)
            if master_w:
                rpc_master_ip = master_w.ip

    # 判断流水线并行角色（prima_cpp 后端）
    elif model.backend == "prima_cpp":
        pipeline_cfg = dist_cfg.get("pipeline", [])
        is_single = pipeline_cfg == "single"

        if inst.worker_id == worker.id and (rpc_ids or is_single):
            # Master 角色：构造后续 Worker 节点列表
            pipeline_role = "master"
            if not is_single:
                for node in pipeline_cfg:
                    # 跳过 Master 自身（rank 0）
                    if node.get("rank", 0) == 0:
                        continue
                    pipeline_workers.append(
                        PipelineWorkerInfo(
                            worker_id=node["worker_id"],
                            worker_name=node["worker_name"],
                            ip=node["ip"],
                            port=node["port"],
                            layer_start=node["layer_start"],
                            layer_end=node["layer_end"],
                            rank=node["rank"],
                            cpu_cores=node.get("cpu_cores", 0),
                            ready=True,
                        )
                    )
        elif worker.id in rpc_ids:
            # Worker 角色：查找自己的层分配 + Master 地址
            pipeline_role = "worker"
            master_w = await session.get(Worker, inst.worker_id)
            if master_w:
                pipeline_master_ip = master_w.ip
                pipeline_master_port = inst.service_port or 0
            # 从 distributed_config 查找本节点的层段
            for node in pipeline_cfg:
                if node.get("worker_id") == worker.id:
                    layer_start = node.get("layer_start", -1)
                    layer_end = node.get("layer_end", -1)
                    rank = node.get("rank", -1)
                    break

    return AssignedInstance(
        id=inst.id,
        name=inst.name,
        model_id=inst.model_id,
        state=inst.state.value,
        allocated_cpu_cores=inst.allocated_cpu_cores,
        allocated_memory=inst.allocated_memory,
        source_repo=model.source_repo,
        source_model_id=model.source_model_id,
        source_filename=model.source_filename,
        backend=model.backend.value,
        backend_parameters=params,
        rpc_role=rpc_role,
        rpc_slaves=rpc_slaves,
        rpc_master_ip=rpc_master_ip,
        pipeline_role=pipeline_role,
        pipeline_workers=pipeline_workers,
        pipeline_master_ip=pipeline_master_ip,
        pipeline_master_port=pipeline_master_port,
        layer_start=layer_start,
        layer_end=layer_end,
        rank=rank,
    )


@router.get("/instances", response_model=list[AssignedInstance])
async def list_assigned_instances(
    worker: Worker = Depends(_verify_worker),
    session=Depends(get_session),
):
    """查询分配给本 Worker 的实例。

    返回：
    - Master 角色：worker_id == 本节点的实例（含 RPC Slave 信息）
    - Slave 角色：rpc_worker_ids 包含本节点 ID 的实例
    - 单机模式：worker_id == 本节点的实例
    """
    active_states = [
        ModelInstanceState.SCHEDULED,
        ModelInstanceState.INITIALIZING,
        ModelInstanceState.DOWNLOADING,
        ModelInstanceState.STARTING,
        ModelInstanceState.RUNNING,
    ]

    # 1. 查询本节点为 Master/单机的实例
    master_stmt = select(ModelInstance, Model).where(
        ModelInstance.worker_id == worker.id,
        ModelInstance.model_id == Model.id,
        ModelInstance.state.in_(active_states),
    )
    master_rows = (await session.execute(master_stmt)).all()

    result = []
    seen_ids = set()
    for inst, model in master_rows:
        result.append(await _build_assigned_instance(inst, model, worker, session))
        seen_ids.add(inst.id)

    # 2. 查询本节点为 RPC Slave 的实例
    # rpc_worker_ids 是 JSON 数组字符串，需要在 Python 层过滤
    slave_stmt = select(ModelInstance, Model).where(
        ModelInstance.model_id == Model.id,
        ModelInstance.state.in_(active_states),
        ModelInstance.worker_id != worker.id,  # 排除已查的 Master 实例
    )
    slave_rows = (await session.execute(slave_stmt)).all()

    for inst, model in slave_rows:
        if inst.id in seen_ids:
            continue
        rpc_ids = _parse_json(inst.rpc_worker_ids, [])
        if worker.id in rpc_ids:
            result.append(await _build_assigned_instance(inst, model, worker, session))
            seen_ids.add(inst.id)

    return result


@router.post("/instances/{instance_id}/state")
async def update_instance_state(
    instance_id: int,
    update: InstanceStateUpdate,
    worker: Worker = Depends(_verify_worker),
    session=Depends(get_session),
):
    """Worker 上报实例状态变更。

    Master 和 Slave 都通过此端点上报状态。
    Slave 上报时 service_port 为 rpc-server 端口。
    """
    inst = await session.get(ModelInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 验证权限：Master 或 Slave 都可上报
    rpc_ids = _parse_json(inst.rpc_worker_ids, [])
    is_master = inst.worker_id == worker.id
    is_slave = worker.id in rpc_ids
    if not is_master and not is_slave:
        raise HTTPException(status_code=403, detail="实例不属于本 Worker")

    # Slave 只能上报 ERROR 状态（rpc-server 启动失败等）。
    # Slave 的 INITIALIZING/RUNNING 等状态不应覆盖 Master 的生命周期，
    # 否则 Slave 启动 rpc-server 后报告 RUNNING 会掩盖 Master 仍在下载模型的事实。
    if is_slave and update.state != ModelInstanceState.ERROR:
        logger.info(
            "实例 %s Slave(%s) 上报 %s 状态，忽略（不覆盖 Master 生命周期）",
            inst.name, worker.name, update.state.value,
        )
        return {"status": "ok", "ignored": True}

    old_state = inst.state
    inst.state = update.state
    if update.error_message:
        inst.error_message = update.error_message
    if update.service_port is not None:
        inst.service_port = update.service_port
    if update.download_progress > 0:
        inst.download_progress = update.download_progress

    session.add(inst)
    await session.commit()

    logger.info(
        "实例 %s 状态变更: %s -> %s (worker=%s, role=%s)",
        inst.name,
        old_state.value,
        update.state.value,
        worker.name,
        "master" if is_master else "slave",
    )
    return {"status": "ok"}
