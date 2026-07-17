"""Token 用量统计服务：记录 + 查询。

记录策略：按 (model_name, user_id, api_key_id, usage_date) upsert。
SQLite 使用 INSERT ON CONFLICT；其他 DB 使用 SELECT + UPDATE 兜底。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import select

from cpustack.db import session_scope
from cpustack.schemas.tokens import TokenUsage, ModelUsageSummary

logger = logging.getLogger(__name__)


async def record_token_usage(
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    user_id: int | None = None,
    api_key_id: int | None = None,
    total_tokens: int | None = None,
) -> None:
    """记录一次推理请求的 token 用量。

    按 (model_name, user_id, api_key_id, today) 聚合累加。
    失败仅记录日志，不影响推理主流程。
    """
    if not model_name:
        return
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    if total_tokens <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        return

    today = date.today()

    try:
        async with session_scope() as session:
            # 查找现有记录
            stmt = select(TokenUsage).where(
                TokenUsage.model_name == model_name,
                TokenUsage.user_id == user_id,
                TokenUsage.api_key_id == api_key_id,
                TokenUsage.usage_date == today,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()

            if existing:
                existing.prompt_tokens += prompt_tokens
                existing.completion_tokens += completion_tokens
                existing.total_tokens += total_tokens
                existing.request_count += 1
                session.add(existing)
            else:
                usage = TokenUsage(
                    model_name=model_name,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    usage_date=today,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    request_count=1,
                )
                session.add(usage)
            await session.commit()
    except Exception:
        logger.debug("记录 token 用量失败", exc_info=True)


async def get_usage_summary(
    model_name: str | None = None,
    days: int = 7,
) -> list[dict[str, Any]]:
    """查询近 N 天每模型的用量汇总。

    Args:
        model_name: 指定模型名，None 表示全部
        days: 查询天数（默认 7 天）

    Returns:
        每模型一行：{model_name, prompt_tokens, completion_tokens, total_tokens, request_count, daily: [...]}
    """
    from datetime import timedelta

    end = date.today()
    start = end - timedelta(days=days - 1)

    try:
        async with session_scope() as session:
            stmt = select(TokenUsage).where(TokenUsage.usage_date >= start)
            if model_name:
                stmt = stmt.where(TokenUsage.model_name == model_name)
            rows = (await session.execute(stmt)).scalars().all()

            # 按模型聚合
            by_model: dict[str, dict[str, Any]] = {}
            for r in rows:
                m = by_model.setdefault(
                    r.model_name,
                    {
                        "model_name": r.model_name,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "request_count": 0,
                        "daily": {},
                    },
                )
                m["prompt_tokens"] += r.prompt_tokens
                m["completion_tokens"] += r.completion_tokens
                m["total_tokens"] += r.total_tokens
                m["request_count"] += r.request_count
                m["daily"][r.usage_date.isoformat()] = {
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.total_tokens,
                    "request_count": r.request_count,
                }

            # 补全缺失日期（确保图表数据连续）
            result = []
            cur = start
            while cur <= end:
                cur_iso = cur.isoformat()
                for m in by_model.values():
                    if cur_iso not in m["daily"]:
                        m["daily"][cur_iso] = {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": 0,
                            "request_count": 0,
                        }
                cur += timedelta(days=1)

            for m in by_model.values():
                m["daily"] = [
                    {"date": k, **v} for k, v in sorted(m["daily"].items())
                ]
                result.append(m)

            result.sort(key=lambda x: x["total_tokens"], reverse=True)
            return result
    except Exception:
        logger.exception("查询用量汇总失败")
        return []


async def get_total_usage() -> dict[str, Any]:
    """查询全集群累计用量。"""
    try:
        async with session_scope() as session:
            rows = (await session.execute(select(TokenUsage))).scalars().all()
            return {
                "prompt_tokens": sum(r.prompt_tokens for r in rows),
                "completion_tokens": sum(r.completion_tokens for r in rows),
                "total_tokens": sum(r.total_tokens for r in rows),
                "request_count": sum(r.request_count for r in rows),
                "model_count": len({r.model_name for r in rows}),
            }
    except Exception:
        logger.exception("查询累计用量失败")
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
            "model_count": 0,
        }
