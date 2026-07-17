"""局域网发现路由：扫描子节点 + 一键注册。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cpustack.config import settings
from cpustack.db import get_session
from cpustack.schemas.users import User
from cpustack.server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


class DiscoveredWorker(BaseModel):
    """扫描发现的 Worker 信息。"""

    name: str
    ip: str
    port: int
    worker_port: int
    hostname: str = ""
    cpu_cores: int = 0
    memory_total_mb: int = 0
    responded_at: str
    registered: bool = False
    registered_worker_id: int | None = None
    registered_name: str | None = None


class ScanResponse(BaseModel):
    """扫描结果。"""

    total: int
    discovered: list[DiscoveredWorker]
    broadcast_addresses: list[str] = []


class RegisterDiscoveredRequest(BaseModel):
    """一键注册已发现 Worker 的请求。

    复用系统已有的 Worker 主动注册流程：
    Server 不直接创建 Worker 记录，而是返回注册引导信息，
    由调用方在目标 Worker 上执行 cpustack worker 启动即可自动注册。
    """

    ip: str
    port: int = 30080
    name: str | None = None  # 可选自定义名称


class RegisterDiscoveredResponse(BaseModel):
    """注册引导响应。"""

    ip: str
    port: int
    name: str
    server_url: str
    worker_token: str
    command: str  # 在目标节点执行的命令


@router.get("/scan", response_model=ScanResponse)
async def scan_workers(
    timeout: int | None = None,
    user: User = Depends(get_current_user),
):
    """扫描局域网内的 CPUSTACK Worker 节点。

    通过 UDP 广播探测，返回所有响应的 Worker。已注册的 Worker 会被标记。
    """
    from cpustack.server.discovery import scan_lan_workers, _get_broadcast_addresses

    if timeout is None:
        timeout = settings.discovery_scan_timeout
    # 限制最大超时，防止滥用
    timeout = max(1, min(timeout, 30))

    try:
        discovered = await scan_lan_workers(timeout=timeout)
    except Exception:
        logger.exception("局域网扫描异常")
        raise HTTPException(status_code=500, detail="局域网扫描失败，请查看日志")

    items = [DiscoveredWorker(**d) for d in discovered]
    return ScanResponse(
        total=len(items),
        discovered=items,
        broadcast_addresses=_get_broadcast_addresses(),
    )


@router.post("/register", response_model=RegisterDiscoveredResponse)
async def register_discovered(
    req: RegisterDiscoveredRequest,
    user: User = Depends(get_current_user),
):
    """为已发现的 Worker 生成注册引导命令。

    CPUSTACK 的 Worker 是主动注册模型（Worker 调用 Server 的
    /v2/worker-registration），因此此端点返回在目标节点上执行的命令，
    用户在目标节点运行该命令即可完成注册。
    """
    name = req.name or f"worker-{req.ip.replace('.', '-')}"
    # 修正 server_url：host 为 0.0.0.0 时不可被外部访问，回退到 server_url 配置
    if settings.host in ("0.0.0.0", "::"):
        external_url = settings.server_url
    else:
        external_url = f"http://{settings.host}:{settings.port}"

    command = (
        f"set CPUSTACK_SERVER_URL={external_url} && "
        f"set CPUSTACK_WORKER_TOKEN={settings.worker_token} && "
        f"set CPUSTACK_WORKER_NAME={name} && "
        f"set CPUSTACK_WORKER_PORT={req.port} && "
        f"py -m cpustack.cli worker"
    )

    return RegisterDiscoveredResponse(
        ip=req.ip,
        port=req.port,
        name=name,
        server_url=external_url,
        worker_token=settings.worker_token,
        command=command,
    )
