"""局域网发现路由：扫描子节点 + 一键添加（接管注册）。"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from cpustack.config import settings
from cpustack.db import get_session, session_scope
from cpustack.schemas.users import User
from cpustack.schemas.workers import Worker
from cpustack.server.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_lan_ip() -> str:
    """获取本机局域网 IP（用于构造子节点可回连的地址）。

    通过 UDP 连接探测默认路由出口 IP，无需真正发送数据。失败回退 127.0.0.1。
    """
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _external_server_url() -> str:
    """计算主节点外部可达 URL。

    settings.server_url 可能被配置为 loopback（127.0.0.1/localhost）或端口与实际
    监听端口不符（如 CLI 用 --port 覆盖了配置端口），这些地址对子节点不可达。
    检测到不可达配置时，用本机局域网 IP + 实际监听端口(settings.port)构造正确地址。
    """
    from urllib.parse import urlparse

    configured = settings.server_url
    try:
        parsed = urlparse(configured)
        cfg_host = (parsed.hostname or "").lower()
        cfg_port = parsed.port
    except Exception:
        cfg_host, cfg_port = "", None

    # host 不能是 loopback/通配，端口须与实际监听端口一致，否则对外不可达
    unreachable_host = cfg_host in ("", "localhost", "127.0.0.1", "0.0.0.0", "::1", "::")
    port_mismatch = cfg_port is not None and cfg_port != settings.port

    if not unreachable_host and not port_mismatch:
        return configured

    lan_ip = _get_lan_ip()
    return f"http://{lan_ip}:{settings.port}"


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
    """为已发现的 Worker 生成注册引导命令（向后兼容）。

    推荐使用 POST /v2/discovery/adopt 实现一键添加，无需在子节点手动执行命令。
    """
    name = req.name or f"worker-{req.ip.replace('.', '-')}"
    external_url = _external_server_url()

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


class AdoptRequest(BaseModel):
    """一键添加已发现 Worker 的请求。"""

    ip: str
    port: int = 30080
    name: str | None = None  # 可选自定义名称


class AdoptResponse(BaseModel):
    """一键添加结果。"""

    ok: bool
    ip: str
    port: int
    name: str
    worker_id: int | None = None
    worker_uuid: str | None = None
    message: str = ""


@router.post("/adopt", response_model=AdoptResponse)
async def adopt_discovered(
    req: AdoptRequest,
    user: User = Depends(get_current_user),
):
    """一键添加已发现的子节点：主节点主动推送注册指令，子节点自动注册并入算力池。

    流程：
    1. 主节点调用子节点 POST http://{ip}:{port}/internal/register
    2. 子节点用推送的 server_url + token 重新注册到主节点
    3. 注册成功后子节点自动心跳上报，并入算力池

    无需在子节点手动执行任何命令。
    """
    name = req.name or f"worker-{req.ip.replace('.', '-')}"
    external_url = _external_server_url()
    target = f"http://{req.ip}:{req.port}/internal/register"

    logger.info("一键添加子节点 %s:%d -> %s", req.ip, req.port, target)

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.post(
                target,
                json={
                    "server_url": external_url,
                    "worker_token": settings.worker_token,
                    "name": name,
                },
            )
    except httpx.ConnectError:
        return AdoptResponse(
            ok=False, ip=req.ip, port=req.port, name=name,
            message=f"无法连接子节点 {req.ip}:{req.port}，请确认子节点 Worker 进程已启动",
        )
    except httpx.TimeoutException:
        return AdoptResponse(
            ok=False, ip=req.ip, port=req.port, name=name,
            message=f"连接子节点超时，请检查网络或防火墙（端口 {req.port}）",
        )

    if resp.status_code != 200:
        return AdoptResponse(
            ok=False, ip=req.ip, port=req.port, name=name,
            message=f"子节点响应异常: HTTP {resp.status_code}",
        )

    try:
        data = resp.json()
    except Exception:
        return AdoptResponse(
            ok=False, ip=req.ip, port=req.port, name=name,
            message="子节点返回非 JSON 响应",
        )

    sub_ok = data.get("ok")
    worker_id = data.get("worker_id")
    worker_uuid = data.get("worker_uuid")

    # 无论子节点返回 ok=True/False，都轮询数据库确认（最多 8 秒）
    # 子节点可能因凭证持久化失败误报 ok=False，但实际注册已成功（内存凭证已赋值，
    # 心跳正常上报）。以数据库实际状态为准，避免误报添加失败。
    confirmed = False
    for _ in range(8):
        await asyncio.sleep(1)
        try:
            async with session_scope() as session:
                if worker_uuid:
                    stmt = select(Worker).where(Worker.uuid == worker_uuid)
                else:
                    stmt = select(Worker).where(Worker.ip == req.ip)
                w = (await session.execute(stmt)).scalar_one_or_none()
                if w and w.heartbeat_at is not None:
                    confirmed = True
                    worker_id = w.id
                    worker_uuid = w.uuid
                    break
        except Exception:
            logger.debug("确认节点注册状态时查询失败", exc_info=True)

    if confirmed:
        return AdoptResponse(
            ok=True,
            ip=req.ip,
            port=req.port,
            name=name,
            worker_id=worker_id,
            worker_uuid=worker_uuid,
            message="已并入算力池",
        )

    # 数据库未确认，按子节点原始返回值响应
    if sub_ok:
        return AdoptResponse(
            ok=True,
            ip=req.ip,
            port=req.port,
            name=name,
            worker_id=worker_id,
            worker_uuid=worker_uuid,
            message="注册成功，等待心跳上报中",
        )
    return AdoptResponse(
        ok=False,
        ip=req.ip,
        port=req.port,
        name=name,
        message=data.get("message", "子节点注册失败"),
    )
