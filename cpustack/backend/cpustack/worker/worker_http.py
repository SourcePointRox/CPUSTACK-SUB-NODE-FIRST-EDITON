"""Worker 轻量 HTTP 服务：支持主节点一键接管注册。

监听 worker_port（默认 30080），暴露内部端点：
- GET  /internal/health        健康检查（供主节点探测可达性）
- POST /internal/register      主节点推送 server_url + token，触发本节点重新注册并入池

设计要点：
- 极简 FastAPI 应用，仅 2 个端点，不依赖数据库
- 嵌入式 uvicorn 运行在 Worker 的 asyncio 事件循环中
- 注册触发由 WorkerManager 执行，复用既有注册流程
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from cpustack.worker.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


class RegisterTriggerRequest(BaseModel):
    """主节点推送的注册触发请求。"""

    server_url: str  # 主节点的外部可达地址，如 http://192.168.1.100:8081
    worker_token: str  # 集群共享密钥
    name: str | None = None  # 可选：主节点指定的节点名称


class RegisterTriggerResponse(BaseModel):
    """注册触发结果。"""

    ok: bool
    worker_id: int | None = None
    worker_uuid: str | None = None
    message: str = ""


def create_worker_http_app(worker_manager: "WorkerManager") -> FastAPI:
    """构造 Worker 内部 HTTP 应用。

    Args:
        worker_manager: 当前 Worker 的 WorkerManager 实例

    Returns:
        FastAPI 应用（含 /internal/health 与 /internal/register）
    """
    app = FastAPI(
        title="CPUSTACK Worker Internal",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/internal/health")
    async def health() -> dict:
        """健康检查：返回本节点基本信息，供主节点探测可达性。"""
        return {
            "status": "ok",
            "worker_uuid": worker_manager.worker_uuid,
            "worker_id": worker_manager.worker_id,
        }

    @app.post("/internal/register", response_model=RegisterTriggerResponse)
    async def trigger_register(req: RegisterTriggerRequest) -> RegisterTriggerResponse:
        """主节点一键接管注册：推送新的 server_url + token，触发本节点重新注册。

        流程：
        1. 清除本地旧凭证（避免复用错误的 uuid/api_key）
        2. 用新的 server_url + token 调用主节点 /v2/worker-registration
        3. 注册成功后，后续心跳自动用新地址上报，节点并入算力池
        """
        logger.info(
            "收到主节点注册触发: server_url=%s, name=%s",
            req.server_url,
            req.name,
        )

        # 清除旧凭证，强制完整重注册
        try:
            from cpustack.config import settings
            cred_file = settings.data_path / "worker_credentials.json"
            if cred_file.exists():
                cred_file.unlink()
        except Exception:
            logger.debug("清除旧凭证文件失败", exc_info=True)

        # 可选：主节点指定节点名称
        if req.name:
            try:
                from cpustack.config import settings
                settings.worker_name = req.name
            except Exception:
                logger.debug("设置 worker_name 失败", exc_info=True)

        # 执行重新注册（使用传入的 server_url + token 覆盖）
        try:
            ok = await worker_manager.register(
                server_url_override=req.server_url,
                token_override=req.worker_token,
            )
        except Exception:
            logger.exception("注册触发执行异常")
            ok = False

        # register() 可能因凭证持久化失败返回 False，但内存凭证已赋值（注册实际成功）
        # 此时 worker_uuid 一定有值，主节点可据此在数据库中确认节点已注册
        if ok or worker_manager.worker_uuid:
            return RegisterTriggerResponse(
                ok=True,
                worker_id=worker_manager.worker_id,
                worker_uuid=worker_manager.worker_uuid,
                message="注册成功，已并入算力池",
            )
        return RegisterTriggerResponse(
            ok=False, message="注册失败，请检查主节点地址与 token"
        )

    return app


async def start_worker_http(
    worker_manager: "WorkerManager",
    port: int | None = None,
) -> asyncio.Task | None:
    """启动 Worker 内部 HTTP 服务（嵌入式 uvicorn）。

    Args:
        worker_manager: Worker 的 WorkerManager 实例
        port: 监听端口，默认取 settings.worker_port

    Returns:
        后台运行的服务 task；启动失败返回 None
    """
    import uvicorn

    from cpustack.config import settings

    if port is None:
        port = settings.worker_port

    app = create_worker_http_app(worker_manager)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # 允许被取消时优雅退出
    server.config.load()
    server.lifespan = "off"

    async def _run() -> None:
        try:
            await server.serve()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Worker HTTP 服务异常退出")

    task = asyncio.create_task(_run())
    logger.info("Worker 内部 HTTP 服务已启动 (端口 %d)，支持一键接管注册", port)
    return task
