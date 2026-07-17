"""CLI 入口：cpustack serve / cpustack worker。"""

from __future__ import annotations

import logging

import click

from cpustack.config import settings


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@click.group()
def main() -> None:
    """CPUSTACK - CPU 分布式 AI 模型部署与推理平台。"""
    pass


@main.command()
@click.option("--host", default=None, help="监听地址")
@click.option("--port", default=None, type=int, help="监听端口")
@click.option("--debug", is_flag=True, help="调试模式")
def serve(host: str | None, port: int | None, debug: bool) -> None:
    """启动 Server（控制平面）。"""
    _setup_logging(debug)

    if host:
        settings.host = host
    if port:
        settings.port = port

    import uvicorn

    uvicorn.run(
        "cpustack.server.app:app",
        host=settings.host,
        port=settings.port,
        log_level="debug" if debug else "info",
    )


@main.command()
@click.option("--name", default=None, help="Worker 名称")
@click.option("--server-url", default=None, help="Server 地址")
@click.option("--token", default=None, help="注册 Token")
@click.option("--debug", is_flag=True, help="调试模式")
def worker(name: str | None, server_url: str | None, token: str | None, debug: bool) -> None:
    """启动 Worker（数据平面）。"""
    _setup_logging(debug)

    if name:
        settings.worker_name = name
    if server_url:
        settings.server_url = server_url
    if token:
        settings.worker_token = token

    import asyncio

    from cpustack.worker.worker import Worker

    w = Worker()
    asyncio.run(w.start())


@main.command()
@click.option("--host", default=None, help="监听地址")
@click.option("--port", default=None, type=int, help="监听端口")
@click.option("--debug", is_flag=True, help="调试模式")
def both(host: str | None, port: int | None, debug: bool) -> None:
    """同时启动 Server + Worker（单机模式）。"""
    _setup_logging(debug)

    if host:
        settings.host = host
    if port:
        settings.port = port

    import asyncio
    import uvicorn

    from cpustack.worker.worker import Worker

    config = uvicorn.Config(
        "cpustack.server.app:app",
        host=settings.host,
        port=settings.port,
        log_level="debug" if debug else "info",
    )
    server = uvicorn.Server(config)

    async def run_both():
        # 启动 Server
        server_task = asyncio.create_task(server.serve())
        # 等待 Server 就绪后启动 Worker
        await asyncio.sleep(2)
        settings.server_url = f"http://127.0.0.1:{settings.port}"
        worker = Worker()
        # 将 Worker 实例存储到 app.state，供 Server 路由访问 ServeManager（如日志端点）
        from cpustack.server.app import app
        app.state.worker = worker
        worker_task = asyncio.create_task(worker.start())

        await asyncio.gather(server_task, worker_task)

    asyncio.run(run_both())


if __name__ == "__main__":
    main()
