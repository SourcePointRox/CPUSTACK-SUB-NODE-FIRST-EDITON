"""节点管理路由。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from cpustack.db import get_session
from cpustack.schemas.users import User
from cpustack.schemas.workers import Worker, WorkerState, WorkerStatus, get_instruction_sets
from cpustack.server.auth import get_current_user

router = APIRouter()


class WorkerResponse(BaseModel):
    id: int
    name: str
    ip: str
    port: int
    state: str
    labels: dict[str, str] | None = None
    heartbeat_at: str | None = None
    # 资源信息
    cpu_model: str = ""
    cpu_cores: int = 0
    cpu_utilization: float = 0.0
    instruction_sets: list[str] = []
    memory_total: int = 0
    memory_available: int = 0
    memory_allocated: int = 0
    disk_total: int = 0
    disk_available: int = 0
    os: str = ""
    numa_nodes: int = 1


def _to_response(worker: Worker, status: WorkerStatus | None) -> WorkerResponse:
    labels = None
    if worker.labels:
        try:
            labels = json.loads(worker.labels)
        except (json.JSONDecodeError, TypeError):
            labels = None

    return WorkerResponse(
        id=worker.id,
        name=worker.name,
        ip=worker.ip,
        port=worker.port,
        state=worker.state.value,
        labels=labels,
        heartbeat_at=worker.heartbeat_at.isoformat() if worker.heartbeat_at else None,
        cpu_model=status.cpu_model if status else "",
        cpu_cores=status.cpu_cores if status else 0,
        cpu_utilization=status.cpu_utilization if status else 0.0,
        instruction_sets=get_instruction_sets(status) if status else [],
        memory_total=status.memory_total if status else 0,
        memory_available=status.memory_available if status else 0,
        memory_allocated=status.memory_allocated if status else 0,
        disk_total=status.disk_total if status else 0,
        disk_available=status.disk_available if status else 0,
        os=status.os if status else "",
        numa_nodes=status.numa_nodes if status else 1,
    )


@router.get("", response_model=list[WorkerResponse])
async def list_workers(
    state: str | None = Query(None),
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出所有节点。"""
    stmt = select(Worker)
    if state:
        stmt = stmt.where(Worker.state == state)
    workers = (await session.execute(stmt)).scalars().all()

    result = []
    for w in workers:
        status_stmt = select(WorkerStatus).where(WorkerStatus.worker_id == w.id)
        status = (await session.execute(status_stmt)).scalar_one_or_none()
        result.append(_to_response(w, status))
    return result


@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker(
    worker_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """获取节点详情。"""
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="节点不存在")
    status_stmt = select(WorkerStatus).where(WorkerStatus.worker_id == worker.id)
    status = (await session.execute(status_stmt)).scalar_one_or_none()
    return _to_response(worker, status)


@router.put("/{worker_id}/labels")
async def update_worker_labels(
    worker_id: int,
    labels: dict[str, str],
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """更新节点标签。"""
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="节点不存在")
    worker.labels = json.dumps(labels)
    session.add(worker)
    await session.commit()
    return {"message": "标签已更新"}


@router.delete("/{worker_id}")
async def remove_worker(
    worker_id: int,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """移除节点。"""
    worker = await session.get(Worker, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="节点不存在")
    # 删除关联状态
    status_stmt = select(WorkerStatus).where(WorkerStatus.worker_id == worker.id)
    status = (await session.execute(status_stmt)).scalar_one_or_none()
    if status:
        await session.delete(status)
    await session.delete(worker)
    await session.commit()
    return {"message": "节点已移除"}
