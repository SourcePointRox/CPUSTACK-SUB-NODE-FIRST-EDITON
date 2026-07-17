"""模型目录浏览 + 一键拉取路由。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from cpustack.catalog.service import list_catalog, pull_from_catalog
from cpustack.schemas.users import User
from cpustack.server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class CatalogEntry(BaseModel):
    name: str
    display_name: str
    description: str
    parameters: str = ""
    source_repo: str
    source_model_id: str
    source_filename: str
    quantization_size_gb: float = 0
    estimated_memory_mb: int = 0
    required_instruction_sets: list[str] = []
    recommended_backend: str = "llama_cpp_standalone"
    test_purpose: str = ""
    category: str = ""
    size_tier: str = "unknown"


class CatalogResponse(BaseModel):
    total: int
    categories: list[str]
    entries: list[CatalogEntry]


class PullRequest(BaseModel):
    """一键拉取请求。"""

    catalog_name: str  # 目录中的模型 name
    replicas: int = 1
    backend_override: str | None = None  # 可选覆盖后端
    custom_model_name: str | None = None  # 可选自定义部署名


class PullResponse(BaseModel):
    model_id: int
    model_name: str
    instances: int
    message: str


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(
    category: str | None = Query(None, description="按分类过滤"),
    search: str | None = Query(None, description="关键词搜索"),
    user: User = Depends(get_current_user),
):
    """浏览预置模型目录（支持分类过滤与关键词搜索）。"""
    entries = list_catalog()

    if search:
        kw = search.lower()
        entries = [
            e
            for e in entries
            if kw in e["name"].lower()
            or kw in e["display_name"].lower()
            or kw in e["description"].lower()
            or kw in e["source_model_id"].lower()
        ]

    if category:
        entries = [e for e in entries if e["category"] == category]

    categories = sorted({e["category"] for e in list_catalog() if e["category"]})
    return CatalogResponse(
        total=len(entries),
        categories=categories,
        entries=[CatalogEntry(**e) for e in entries],
    )


@router.post("/pull", response_model=PullResponse)
async def pull_model(
    req: PullRequest,
    user: User = Depends(get_current_user),
):
    """一键拉取并部署目录中的模型。

    流程：查目录 → 创建 Model → 创建 N 个 PENDING 实例 → 调度器接管下载与启动。
    """
    try:
        result = await pull_from_catalog(
            name=req.catalog_name,
            user=user,
            replicas=req.replicas,
            backend_override=req.backend_override,
            custom_model_name=req.custom_model_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("拉取模型失败: %s", req.catalog_name)
        raise HTTPException(status_code=500, detail="拉取模型失败")
    return PullResponse(**result)
