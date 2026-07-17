"""FastAPI 应用创建与生命周期管理。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from cpustack.bus import get_event_bus
from cpustack.config import settings
from cpustack.db import dispose_engine
from cpustack.server.routes import (
    auth,
    discovery,
    knowledge,
    model_pull,
    models,
    openai,
    system,
    tokens,
    worker_api,
    workers,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动事件总线 + 调度器 + 控制器，关闭时逆序清理。"""
    # 启动
    bus = get_event_bus()
    await bus.start()
    logger.info("CPUSTACK Server 启动中...")

    # 初始化默认数据
    from cpustack.server.init_data import init_default_data

    await init_default_data()

    # 启动调度器
    from cpustack.server.scheduler.scheduler import start_scheduler, stop_scheduler

    await start_scheduler()

    # 启动控制器（调谐循环）
    from cpustack.server.controllers import (
        InstanceController,
        ModelController,
        WorkerController,
    )

    controllers = [
        WorkerController(),
        ModelController(),
        InstanceController(),
    ]
    for c in controllers:
        await c.start()
    app.state.controllers = controllers
    app.state.scheduler_started = True

    yield

    # 关闭（逆序：控制器 → 调度器 → 事件总线 → DB）
    logger.info("CPUSTACK Server 关闭中...")
    for c in reversed(controllers):
        await c.stop()
    await stop_scheduler()
    await bus.stop()
    await dispose_engine()


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(
        title="CPUSTACK",
        description="CPU 分布式 AI 模型部署与推理平台",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS（可通过配置收紧，生产环境应设为具体域名）
    cors_origins = settings.cors_origins if settings.cors_origins else ["*"]
    # allow_credentials=True 时不能使用通配符 "*", 需特殊处理
    allow_credentials = "*" not in cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(auth.router, prefix="/v2/auth", tags=["认证"])
    app.include_router(workers.router, prefix="/v2/workers", tags=["节点管理"])
    app.include_router(models.router, prefix="/v2/models", tags=["模型管理"])
    app.include_router(model_pull.router, prefix="/v2/models", tags=["模型目录与拉取"])
    app.include_router(system.router, prefix="/v2", tags=["系统"])
    app.include_router(worker_api.router, prefix="/v2/worker", tags=["Worker API"])
    app.include_router(discovery.router, prefix="/v2/discovery", tags=["局域网发现"])
    app.include_router(tokens.router, prefix="/v2/tokens", tags=["Token 用量"])
    app.include_router(knowledge.router, prefix="/v2/knowledge-bases", tags=["本地知识库"])
    app.include_router(openai.router, prefix="/v1", tags=["OpenAI 兼容 API"])

    # 健康检查
    @app.get("/healthz", tags=["健康检查"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["健康检查"])
    async def readyz():
        """就绪检查：验证事件总线、调度器、控制器均已启动。"""
        from cpustack.bus import get_event_bus

        bus = get_event_bus()
        checks = {
            "event_bus": bus._running,
            "scheduler": getattr(app.state, "scheduler_started", False),
            "controllers": all(
                c._running for c in getattr(app.state, "controllers", [])
            ),
        }
        ready = all(checks.values())
        return {"status": "ready" if ready else "not_ready", "checks": checks}

    # Prometheus 指标端点（可通过配置关闭）
    if settings.metrics_enabled:
        from fastapi import Response

        @app.get("/metrics", tags=["监控"])
        async def metrics():
            """Prometheus 指标导出。"""
            from cpustack.server.metrics import render_metrics

            # 采集最新指标
            from cpustack.server.metrics import collect_metrics
            await collect_metrics()
            body, content_type = render_metrics()
            return Response(content=body, media_type=content_type)

    # 挂载前端静态文件（生产环境）
    frontend_dist = settings.data_path / "ui"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()
