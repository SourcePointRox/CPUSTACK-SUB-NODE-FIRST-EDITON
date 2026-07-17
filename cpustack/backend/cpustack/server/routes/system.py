"""系统路由：概览统计、模型目录、Worker 注册端点。"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from cpustack.config import settings
from cpustack.db import get_session, session_scope
from cpustack.schemas.models import Model, ModelInstance, ModelInstanceState
from cpustack.schemas.users import User
from cpustack.schemas.workers import Worker, WorkerState, WorkerStatus
from cpustack.server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class WorkerRegisterRequest(BaseModel):
    """Worker 注册请求。"""

    name: str
    token: str
    ip: str
    port: int = 30080


class WorkerRegisterResponse(BaseModel):
    worker_id: int
    worker_uuid: str
    api_key: str


class WorkerSyncRequest(BaseModel):
    """Worker 心跳同步请求。"""

    worker_uuid: str
    api_key: str
    status: dict


class DashboardStats(BaseModel):
    total_workers: int
    ready_workers: int
    total_cpu_cores: int
    total_memory_mb: int
    available_memory_mb: int
    total_models: int
    running_instances: int


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard(
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """概览统计。"""
    workers = (await session.execute(select(Worker))).scalars().all()
    ready = [w for w in workers if w.state == WorkerState.READY]

    total_cpu = 0
    total_mem = 0
    avail_mem = 0
    for w in workers:
        s = (
            await session.execute(
                select(WorkerStatus).where(WorkerStatus.worker_id == w.id)
            )
        ).scalar_one_or_none()
        if s:
            total_cpu += s.cpu_cores
            total_mem += s.memory_total
            avail_mem += s.memory_available

    total_models = len((await session.execute(select(Model))).scalars().all())
    running = (
        await session.execute(
            select(ModelInstance).where(ModelInstance.state == ModelInstanceState.RUNNING)
        )
    ).scalars().all()

    return DashboardStats(
        total_workers=len(workers),
        ready_workers=len(ready),
        total_cpu_cores=total_cpu,
        total_memory_mb=total_mem,
        available_memory_mb=avail_mem,
        total_models=total_models,
        running_instances=len(running),
    )


@router.post("/worker-registration", response_model=WorkerRegisterResponse)
async def register_worker(req: WorkerRegisterRequest, session=Depends(get_session)):
    """Worker 注册端点（Token 握手）。

    验证集群 Token，生成 worker_uuid 和 Worker 专属 API Key。
    """
    # 验证 Token（初期使用固定 token，后续可从系统设置读取）
    if req.token != settings.worker_token and settings.worker_token:
        raise HTTPException(status_code=403, detail="无效的注册 Token")

    # 检查是否已注册
    stmt = select(Worker).where(Worker.name == req.name)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        # 更新信息
        existing.ip = req.ip
        existing.port = req.port
        existing.state = WorkerState.NOT_READY
        session.add(existing)
        await session.commit()
        return WorkerRegisterResponse(
            worker_id=existing.id,
            worker_uuid=existing.uuid,
            api_key=existing.api_key,
        )

    # 创建新 Worker
    worker_uuid = secrets.token_hex(16)
    api_key = "wk-" + secrets.token_hex(24)
    worker = Worker(
        name=req.name,
        uuid=worker_uuid,
        api_key=api_key,
        ip=req.ip,
        port=req.port,
        state=WorkerState.NOT_READY,
    )
    session.add(worker)
    await session.commit()
    await session.refresh(worker)

    logger.info("Worker 注册成功: %s (%s:%d)", worker.name, worker.ip, worker.port)
    return WorkerRegisterResponse(
        worker_id=worker.id, worker_uuid=worker_uuid, api_key=api_key
    )


@router.post("/worker-sync")
async def sync_worker_status(req: WorkerSyncRequest, session=Depends(get_session)):
    """Worker 心跳同步：上报资源状态。"""
    stmt = select(Worker).where(Worker.uuid == req.worker_uuid)
    worker = (await session.execute(stmt)).scalar_one_or_none()
    if not worker or worker.api_key != req.api_key:
        raise HTTPException(status_code=403, detail="无效的 Worker 凭证")

    # 更新心跳与状态
    worker.heartbeat_at = datetime.now(timezone.utc)
    worker.state = WorkerState.READY
    session.add(worker)

    # 更新资源状态
    status_stmt = select(WorkerStatus).where(WorkerStatus.worker_id == worker.id)
    status = (await session.execute(status_stmt)).scalar_one_or_none()
    if not status:
        status = WorkerStatus(worker_id=worker.id)
        session.add(status)

    for key, value in req.status.items():
        if hasattr(status, key):
            setattr(status, key, value)

    session.add(status)
    await session.commit()
    return {"status": "ok"}


@router.get("/model-catalog")
async def model_catalog(user: User = Depends(get_current_user)):
    """预置模型目录。"""
    return {
        "model_sets": [
            {
                "name": "Llama 3.2",
                "description": "Meta 开源小模型，边缘优化",
                "specs": [
                    {
                        "name": "Llama-3.2-1B-Instruct",
                        "source_repo": "huggingface",
                        "source_model_id": "bartowski/Llama-3.2-1B-Instruct-GGUF",
                        "source_filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 1500,
                        "required_instruction_sets": ["AVX2"],
                    },
                    {
                        "name": "Llama-3.2-3B-Instruct",
                        "source_repo": "huggingface",
                        "source_model_id": "bartowski/Llama-3.2-3B-Instruct-GGUF",
                        "source_filename": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 3500,
                        "required_instruction_sets": ["AVX2"],
                    },
                ],
            },
            {
                "name": "Qwen 2.5",
                "description": "阿里通义千问，多语言强",
                "specs": [
                    {
                        "name": "Qwen2.5-0.5B-Instruct",
                        "source_repo": "huggingface",
                        "source_model_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                        "source_filename": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 800,
                        "required_instruction_sets": ["AVX2"],
                    },
                    {
                        "name": "Qwen2.5-3B-Instruct",
                        "source_repo": "huggingface",
                        "source_model_id": "Qwen/Qwen2.5-3B-Instruct-GGUF",
                        "source_filename": "qwen2.5-3b-instruct-q4_k_m.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 3000,
                        "required_instruction_sets": ["AVX2"],
                    },
                    {
                        "name": "Qwen2.5-7B-Instruct",
                        "source_repo": "huggingface",
                        "source_model_id": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                        "source_filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 5200,
                        "required_instruction_sets": ["AVX2"],
                    },
                ],
            },
            {
                "name": "Phi-4 Mini",
                "description": "微软小模型，推理/编程强",
                "specs": [
                    {
                        "name": "Phi-4-mini-instruct",
                        "source_repo": "huggingface",
                        "source_model_id": "microsoft/Phi-4-mini-instruct-gguf",
                        "source_filename": "Phi-4-mini-instruct-q4-k-m.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 3500,
                        "required_instruction_sets": ["AVX2"],
                    },
                ],
            },
            {
                "name": "Gemma 3",
                "description": "Google 开源，移动端优化",
                "specs": [
                    {
                        "name": "gemma-3-4b-it",
                        "source_repo": "huggingface",
                        "source_model_id": "bartowski/gemma-3-4b-it-GGUF",
                        "source_filename": "gemma-3-4b-it-Q4_K_M.gguf",
                        "quantization": "Q4_K_M",
                        "estimated_memory_mb": 4000,
                        "required_instruction_sets": ["AVX2"],
                    },
                ],
            },
        ]
    }
