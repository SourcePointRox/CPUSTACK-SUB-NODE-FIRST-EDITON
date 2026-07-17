"""Token 用量查询路由。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from cpustack.schemas.users import User
from cpustack.server.auth import get_current_user
from cpustack.server.token_service import get_usage_summary, get_total_usage

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/summary")
async def usage_summary(
    model_name: str | None = Query(None, description="过滤模型名"),
    days: int = Query(7, ge=1, le=90, description="查询天数"),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """查询每模型 token 用量（按天聚合）。"""
    return await get_usage_summary(model_name=model_name, days=days)


@router.get("/total")
async def total_usage(
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """查询全集群累计用量。"""
    return await get_total_usage()
