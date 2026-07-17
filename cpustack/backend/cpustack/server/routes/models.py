"""模型管理路由：CRUD + 部署 + 实例管理。"""

from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from cpustack.db import get_session
from cpustack.schemas.models import Model, ModelBackend, ModelInstance, ModelInstanceState
from cpustack.schemas.users import User
from cpustack.server.auth import get_current_user

router = APIRouter()


class ModelCreate(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    source_repo: str = "huggingface"
    source_model_id: str
    source_filename: str = ""
    backend: ModelBackend = ModelBackend.LLAMA_CPP_STANDALONE
    replicas: int = 1
    estimated_memory: int = 0  # MB
    required_instruction_sets: list[str] = []
    backend_parameters: dict = {}


class ModelResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: str
    source_repo: str
    source_model_id: str
    backend: str
    replicas: int
    estimated_memory: int
    required_instruction_sets: list[str]
    ready_replicas: int = 0


class InstanceResponse(BaseModel):
    id: int
    name: str
    model_id: int
    model_name: str = ""
    worker_id: int | None
    worker_name: str = ""
    state: str
    allocated_cpu_cores: int
    allocated_memory: int
    service_port: int | None
    download_progress: float
    error_message: str


@router.get("", response_model=list[ModelResponse])
async def list_models(
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出所有模型。"""
    stmt = select(Model)
    models = (await session.execute(stmt)).scalars().all()
    result = []
    for m in models:
        # 统计 ready 副本数
        inst_stmt = select(ModelInstance).where(
            ModelInstance.model_id == m.id,
            ModelInstance.state == ModelInstanceState.RUNNING,
        )
        running = (await session.execute(inst_stmt)).scalars().all()
        try:
            req_is = json.loads(m.required_instruction_sets)
        except (json.JSONDecodeError, TypeError):
            req_is = []
        result.append(
            ModelResponse(
                id=m.id,
                name=m.name,
                display_name=m.display_name,
                description=m.description,
                source_repo=m.source_repo,
                source_model_id=m.source_model_id,
                backend=m.backend.value,
                replicas=m.replicas,
                estimated_memory=m.estimated_memory,
                required_instruction_sets=req_is,
                ready_replicas=len(running),
            )
        )
    return result


@router.post("", response_model=ModelResponse)
async def create_model(
    req: ModelCreate,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """创建并部署模型。"""
    model = Model(
        name=req.name,
        display_name=req.display_name or req.name,
        description=req.description,
        source_repo=req.source_repo,
        source_model_id=req.source_model_id,
        source_filename=req.source_filename,
        backend=req.backend,
        replicas=req.replicas,
        estimated_memory=req.estimated_memory,
        required_instruction_sets=json.dumps(req.required_instruction_sets),
        user_id=user.id,
        backend_parameters=json.dumps(req.backend_parameters),
    )
    session.add(model)
    await session.commit()
    await session.refresh(model)

    # 创建期望数量的实例（PENDING 状态，等待调度器调度）
    for _ in range(req.replicas):
        instance = ModelInstance(
            name=f"{model.name}-{secrets.token_hex(3)}",
            model_id=model.id,
            state=ModelInstanceState.PENDING,
        )
        session.add(instance)

    await session.commit()

    # 发布事件触发调度（逐个发布 ModelInstance.CREATED）
    for inst in (await session.execute(
        select(ModelInstance).where(ModelInstance.model_id == model.id)
    )).scalars().all():
        await ModelInstance.publish_created(inst.id, {"name": inst.name})

    try:
        req_is = json.loads(model.required_instruction_sets)
    except (json.JSONDecodeError, TypeError):
        req_is = []
    return ModelResponse(
        id=model.id,
        name=model.name,
        display_name=model.display_name,
        description=model.description,
        source_repo=model.source_repo,
        source_model_id=model.source_model_id,
        backend=model.backend.value,
        replicas=model.replicas,
        estimated_memory=model.estimated_memory,
        required_instruction_sets=req_is,
        ready_replicas=0,
    )


@router.delete("/{model_id}")
async def delete_model(
    model_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """删除模型及其所有实例。"""
    model = await session.get(Model, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    # 删除所有实例
    inst_stmt = select(ModelInstance).where(ModelInstance.model_id == model_id)
    instances = (await session.execute(inst_stmt)).scalars().all()
    for inst in instances:
        await session.delete(inst)

    await session.delete(model)
    await session.commit()
    await Model.publish_deleted(model_id, {"name": model.name})
    return {"message": "模型已删除"}


@router.get("/instances", response_model=list[InstanceResponse])
async def list_instances(
    model_id: int | None = Query(None),
    state: str | None = Query(None),
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出模型实例。"""
    stmt = select(ModelInstance, Model).where(ModelInstance.model_id == Model.id)
    if model_id:
        stmt = stmt.where(ModelInstance.model_id == model_id)
    if state:
        stmt = stmt.where(ModelInstance.state == state)

    rows = (await session.execute(stmt)).all()
    result = []
    for inst, model in rows:
        # 获取 worker 名
        worker_name = ""
        if inst.worker_id:
            from cpustack.schemas.workers import Worker

            w = await session.get(Worker, inst.worker_id)
            if w:
                worker_name = w.name
        result.append(
            InstanceResponse(
                id=inst.id,
                name=inst.name,
                model_id=inst.model_id,
                model_name=model.name,
                worker_id=inst.worker_id,
                worker_name=worker_name,
                state=inst.state.value,
                allocated_cpu_cores=inst.allocated_cpu_cores,
                allocated_memory=inst.allocated_memory,
                service_port=inst.service_port,
                download_progress=inst.download_progress,
                error_message=inst.error_message,
            )
        )
    return result


@router.post("/instances/{instance_id}/restart")
async def restart_instance(
    instance_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """重启模型实例（重置为 PENDING 重新调度，清除自动重试计数）。"""
    from cpustack.server.controllers.instance_controller import reset_retry_count

    inst = await session.get(ModelInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    inst.state = ModelInstanceState.PENDING
    # 清除 [retry:N] 前缀，重置退避计数
    inst.error_message = reset_retry_count(inst.error_message)
    inst.worker_id = None
    inst.service_port = None
    inst.download_progress = 0.0
    inst.distributed_config = "{}"
    inst.rpc_worker_ids = "[]"
    session.add(inst)
    await session.commit()
    await ModelInstance.publish_updated(inst.id, {"state": "pending"})
    return {"message": "实例已重置，等待重新调度"}


@router.post("/instances/{instance_id}/stop")
async def stop_instance(
    instance_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """停止模型实例。"""
    inst = await session.get(ModelInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    inst.state = ModelInstanceState.ERROR
    inst.error_message = "用户手动停止"
    session.add(inst)
    await session.commit()
    await ModelInstance.publish_updated(inst.id, {"state": "error"})
    return {"message": "实例已停止"}


@router.get("/instances/{instance_id}/logs")
async def get_instance_logs(
    instance_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """获取实例日志。

    日志来源：
    1. 本节点 ServeManager 的内存日志缓冲（both 模式下可直接访问）
    2. 数据库中的 error_message 和状态信息
    """
    inst = await session.get(ModelInstance, instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")

    logs: list[str] = []

    # 1. 尝试从 app.state.worker.serve_manager 获取内存日志
    from fastapi import Request
    # 通过模块级 app 引用获取
    try:
        from cpustack.server.app import app
        worker = getattr(app.state, "worker", None)
        if worker and hasattr(worker, "serve_manager"):
            serve_logs = worker.serve_manager.get_instance_logs(instance_id)
            logs.extend(serve_logs)
    except Exception:
        pass

    # 2. 追加数据库中的状态信息
    if not logs:
        logs.append(f"实例 {inst.name} (ID: {inst.id})")
        logs.append(f"当前状态: {inst.state}")
        if inst.error_message:
            logs.append(f"错误信息: {inst.error_message}")
        if inst.download_progress > 0:
            logs.append(f"下载进度: {inst.download_progress * 100:.1f}%")
        if inst.service_port:
            logs.append(f"服务端口: {inst.service_port}")
        if not inst.error_message and inst.state == ModelInstanceState.SCHEDULED:
            logs.append("提示: 实例处于 SCHEDULED 状态，等待分配的 Worker 拉取处理。")
            logs.append("可能原因: Worker 不在线、网络不通、或 Worker 代码版本过旧。")
        elif not logs:
            logs.append("暂无日志")

    return {"logs": "\n".join(logs)}
