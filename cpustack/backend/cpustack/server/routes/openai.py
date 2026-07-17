"""OpenAI 兼容 API：/v1/chat/completions 等。

请求流（7 阶段）：
认证 → 模型名提取 → 访问控制 → 模型解析 → 实例选择(负载均衡)
  → 代理转发(到 Worker /proxy) → 流式响应处理

Token 计量：
- 非流式：直接读取 response.usage
- 流式：从末尾 chunk 提取 usage（若含），否则按字符估算
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import select

from cpustack.config import settings
from cpustack.db import get_session
from cpustack.schemas.models import Model, ModelInstance, ModelInstanceState
from cpustack.schemas.users import User
from cpustack.schemas.workers import Worker
from cpustack.server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

# 推理代理默认超时（秒）：CPU 推理单次请求可能较长
_PROXY_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# 共享 httpx 客户端：复用连接池，避免每次推理请求重建 TCP 连接
_shared_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """获取全局共享 httpx 客户端（惰性创建，连接池跨请求复用）。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=_PROXY_TIMEOUT)
    return _shared_client


async def close_http_client() -> None:
    """关闭共享 httpx 客户端（应用关闭时调用）。"""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文 1 字 ≈ 1.5 token，英文 4 字 ≈ 1 token。"""
    if not text:
        return 0
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - chinese
    return max(1, int(chinese * 1.5 + other / 4))


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """估算请求消息的 token 数。"""
    total = 0
    for m in messages:
        total += 4
        content = m.get("content", "")
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += _estimate_tokens(str(part.get("text", "")))
    return total + 2


async def _select_instance(model_name: str, session, user=None) -> ModelInstance:
    """负载均衡：选择一个 RUNNING 状态的实例。

    数据并行模式下多个副本在此层完成负载均衡（轮询/最少连接）。
    流水线/RPC 模式只有单实例，负载均衡退化为直接选择。

    若 user 关联了 API Key 且设置了 allowed_model_names 白名单，则校验模型访问权限。
    """
    from cpustack.server.gateway.load_balancer import get_load_balancer

    # API Key 模型白名单校验
    if user is not None:
        api_key = getattr(user, "api_key", None)
        if api_key and api_key.allowed_model_names:
            import json
            try:
                allowed = json.loads(api_key.allowed_model_names)
            except (json.JSONDecodeError, TypeError):
                allowed = []
            if allowed and model_name not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"API Key 无权访问模型 '{model_name}'",
                )

    # 查找模型
    model_stmt = select(Model).where(Model.name == model_name)
    model = (await session.execute(model_stmt)).scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 不存在")

    # 查找 RUNNING 实例
    inst_stmt = select(ModelInstance).where(
        ModelInstance.model_id == model.id,
        ModelInstance.state == ModelInstanceState.RUNNING,
    )
    instances = (await session.execute(inst_stmt)).scalars().all()
    if not instances:
        raise HTTPException(status_code=503, detail=f"模型 '{model_name}' 无可用实例")

    # 负载均衡器选择（默认轮询，可切换为最少连接）
    balancer = get_load_balancer()
    chosen = balancer.select(list(instances))
    if not chosen:
        raise HTTPException(status_code=503, detail=f"模型 '{model_name}' 负载均衡选择失败")
    return chosen


async def _proxy_to_worker(
    instance: ModelInstance,
    request_body: dict,
    stream: bool,
    model_name: str = "",
    user: User | None = None,
) -> Any:
    """代理请求到 Worker 上的推理后端（含 Token 计量）。"""
    from cpustack.db import session_scope

    async with session_scope() as session:
        worker = await session.get(Worker, instance.worker_id)
        if not worker:
            raise HTTPException(status_code=503, detail="实例所在节点不可用")
        target_url = f"http://{worker.ip}:{instance.service_port}/v1/chat/completions"

    user_id = user.id if user else None
    api_key_id = None
    if user is not None:
        api_key = getattr(user, "api_key", None)
        api_key_id = api_key.id if api_key else None

    if stream:
        prompt_tokens = _estimate_prompt_tokens(request_body.get("messages", []))
        return StreamingResponse(
            _stream_proxy_with_count(
                target_url,
                request_body,
                model_name,
                prompt_tokens,
                user_id,
                api_key_id,
            ),
            media_type="text/event-stream",
        )
    else:
        client = get_http_client()
        resp = await client.post(target_url, json=request_body)
        data = resp.json()

        # 非流式：提取 usage 记录
        try:
            usage = data.get("usage") or {}
            prompt_t = int(usage.get("prompt_tokens", 0))
            completion_t = int(usage.get("completion_tokens", 0))
            total_t = int(usage.get("total_tokens", prompt_t + completion_t))
            if total_t <= 0:
                # 下游未返回 usage，估算
                prompt_t = _estimate_prompt_tokens(request_body.get("messages", []))
                completion_t = _estimate_tokens(
                    _extract_completion_text(data)
                )
                total_t = prompt_t + completion_t
            if model_name:
                from cpustack.server.token_service import record_token_usage
                await record_token_usage(
                    model_name=model_name,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    total_tokens=total_t,
                )
        except Exception:
            logger.debug("记录非流式 token 用量失败", exc_info=True)
        return data


def _extract_completion_text(data: dict) -> str:
    """从非流式响应中提取补全文本。"""
    try:
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            return msg.get("content", "") or ""
    except Exception:
        pass
    return ""


async def _stream_proxy_with_count(
    url: str,
    body: dict,
    model_name: str,
    prompt_tokens: int,
    user_id: int | None,
    api_key_id: int | None,
):
    """流式代理 + Token 计量：累计 completion 文本，末尾记录用量。

    优先使用末尾 chunk 的 usage（llama.cpp 在 stream_options.include_usage 时返回）。
    """
    completion_text = ""
    usage_from_server: dict | None = None

    from cpustack.server.token_service import record_token_usage

    try:
        client = get_http_client()
        async with client.stream("POST", url, json=body) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                yield f"{line}\n\n"

                trimmed = line.strip()
                if not trimmed.startswith("data:"):
                    continue
                payload = trimmed[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                # 累计 completion 内容
                try:
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        content = delta.get("content")
                        if content:
                            completion_text += content
                except Exception:
                    pass

                # 捕获 usage（末尾 chunk）
                if chunk.get("usage"):
                    usage_from_server = chunk["usage"]
    finally:
        # 流结束，记录用量
        try:
            if usage_from_server:
                prompt_t = int(usage_from_server.get("prompt_tokens", prompt_tokens))
                completion_t = int(usage_from_server.get("completion_tokens", 0))
                total_t = int(
                    usage_from_server.get("total_tokens", prompt_t + completion_t)
                )
            else:
                # 估算
                completion_t = _estimate_tokens(completion_text)
                total_t = prompt_tokens + completion_t
            if model_name:
                await record_token_usage(
                    model_name=model_name,
                    prompt_tokens=prompt_t,
                    completion_tokens=completion_t,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    total_tokens=total_t,
                )
        except Exception:
            logger.debug("记录流式 token 用量失败", exc_info=True)


@router.get("/models")
async def list_openai_models(
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """列出可用模型（OpenAI 格式）。"""
    stmt = select(Model)
    models = (await session.execute(stmt)).scalars().all()
    return {
        "object": "list",
        "data": [
            {
                "id": m.name,
                "object": "model",
                "created": int(m.created_at.timestamp()),
                "owned_by": "cpustack",
            }
            for m in models
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """对话补全（OpenAI 兼容）。"""
    import time

    from cpustack.server.metrics import (
        INFERENCE_REQUEST_DURATION_SECONDS,
        INFERENCE_REQUESTS_TOTAL,
    )

    start = time.perf_counter()
    body = await request.json()
    model_name = body.get("model", "")
    stream = body.get("stream", False)

    try:
        instance = await _select_instance(model_name, session, user=user)
        result = await _proxy_to_worker(
            instance, body, stream, model_name=model_name, user=user
        )
        INFERENCE_REQUESTS_TOTAL.labels(model=model_name, status="success").inc()
        return result
    except HTTPException:
        INFERENCE_REQUESTS_TOTAL.labels(model=model_name, status="error").inc()
        raise
    except Exception:
        INFERENCE_REQUESTS_TOTAL.labels(model=model_name, status="error").inc()
        raise
    finally:
        # 流式响应无法精确计量下游耗时，这里记录网关侧处理时间
        elapsed = time.perf_counter() - start
        INFERENCE_REQUEST_DURATION_SECONDS.labels(model=model_name).observe(elapsed)


@router.post("/completions")
async def completions(
    request: Request,
    user: User = Depends(get_current_user),
    session=Depends(get_session),
):
    """文本补全（OpenAI 兼容，含 Token 计量）。"""
    body = await request.json()
    model_name = body.get("model", "")
    stream = body.get("stream", False)

    instance = await _select_instance(model_name, session, user=user)

    # 代理到 Worker 的 /v1/completions
    from cpustack.db import session_scope

    async with session_scope() as sess:
        worker = await sess.get(Worker, instance.worker_id)
        if not worker:
            raise HTTPException(status_code=503, detail="实例所在节点不可用")
        target_url = f"http://{worker.ip}:{instance.service_port}/v1/completions"

    user_id = user.id if user else None
    api_key_id = None
    if user is not None:
        api_key = getattr(user, "api_key", None)
        api_key_id = api_key.id if api_key else None

    if stream:
        prompt_tokens = _estimate_tokens(str(body.get("prompt", "")))
        return StreamingResponse(
            _stream_proxy_with_count(
                target_url, body, model_name, prompt_tokens, user_id, api_key_id
            ),
            media_type="text/event-stream",
        )

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(target_url, json=body)
        data = resp.json()

    # 非流式：记录用量
    try:
        usage = data.get("usage") or {}
        prompt_t = int(usage.get("prompt_tokens", 0))
        completion_t = int(usage.get("completion_tokens", 0))
        total_t = int(usage.get("total_tokens", prompt_t + completion_t))
        if total_t <= 0:
            prompt_t = _estimate_tokens(str(body.get("prompt", "")))
            completion_t = _estimate_tokens(_extract_completion_text(data))
            total_t = prompt_t + completion_t
        if model_name:
            from cpustack.server.token_service import record_token_usage
            await record_token_usage(
                model_name=model_name,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                user_id=user_id,
                api_key_id=api_key_id,
                total_tokens=total_t,
            )
    except Exception:
        logger.debug("记录 completions token 用量失败", exc_info=True)
    return data
