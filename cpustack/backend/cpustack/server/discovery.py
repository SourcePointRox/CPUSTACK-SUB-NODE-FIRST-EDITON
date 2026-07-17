"""局域网子节点发现：UDP 广播扫描。

协议：
- Server 广播 JSON 探测包到 UDP 广播地址（端口 30090）：
  {"type": "cpustack_discovery", "token": "<worker_token>"}
- Worker 监听 UDP 30090，校验 token 后回包：
  {"type": "cpustack_discovery_response", "name": "...", "ip": "...",
   "port": 30080, "hostname": "...", "worker_port": 30080}

设计要点：
- 单次扫描并发收包，超时回收（默认 5s）
- 同 IP 多端口去重
- 与已注册 Worker 比对，标识"未注册/已注册"
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any

from cpustack.config import settings

logger = logging.getLogger(__name__)

DISCOVERY_MAGIC = "cpustack_discovery"


def _get_broadcast_addresses() -> list[str]:
    """获取本机所有网段的广播地址。

    通过 UDP socket 不连接的方式获取本机 IP，再构造 x.x.x.255 广播。
    Windows 上 255.255.255.255 广播可能受限，按网段构造更可靠。
    """
    addrs: list[str] = []
    try:
        # 通过 UDP "假连接" 获取本机出口 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()

        if local_ip and not local_ip.startswith("127."):
            parts = local_ip.split(".")
            if len(parts) == 4:
                # C 类网段广播：x.x.x.255
                addrs.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
    except Exception:
        logger.debug("获取本机 IP 失败", exc_info=True)

    # 兜底：受限广播地址
    if "255.255.255.255" not in addrs:
        addrs.append("255.255.255.255")
    return addrs


async def scan_lan_workers(timeout: int | None = None) -> list[dict[str, Any]]:
    """扫描局域网内的 CPUSTACK Worker 节点。

    Args:
        timeout: 收包超时秒数，默认取 settings.discovery_scan_timeout

    Returns:
        已发现的 Worker 信息列表，每项含：
        - name, ip, port, hostname, worker_port, responded_at, registered
    """
    if timeout is None:
        timeout = settings.discovery_scan_timeout

    discovery_port = settings.discovery_port
    token = settings.worker_token
    payload = json.dumps(
        {"type": DISCOVERY_MAGIC, "token": token}
    ).encode("utf-8")

    broadcast_addrs = _get_broadcast_addresses()
    logger.info(
        "开始局域网扫描：广播地址 %s，UDP 端口 %d，超时 %ds",
        broadcast_addrs,
        discovery_port,
        timeout,
    )

    # 创建 UDP 套接字（允许广播）
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)

    discovered: dict[tuple[str, int], dict[str, Any]] = {}

    loop = asyncio.get_event_loop()

    async def _recv_loop(deadline: float) -> None:
        """循环收包直到超时。"""
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return
            except Exception:
                logger.debug("接收发现响应异常", exc_info=True)
                return

            try:
                msg = json.loads(data.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue

            if msg.get("type") != f"{DISCOVERY_MAGIC}_response":
                continue

            # 优先用 UDP 包实际源地址（一定可达），子节点自报 IP 可能因多网卡/代理 TUN 而错误
            src_ip = addr[0]
            reported_ip = msg.get("ip")
            if src_ip and not src_ip.startswith("127."):
                ip = src_ip
            else:
                ip = reported_ip or src_ip or ""
            port = int(msg.get("port") or msg.get("worker_port") or 30080)
            key = (ip, port)
            if key in discovered:
                continue

            discovered[key] = {
                "name": msg.get("name", ""),
                "ip": ip,
                "port": port,
                "worker_port": port,
                "hostname": msg.get("hostname", ""),
                "cpu_cores": msg.get("cpu_cores", 0),
                "memory_total_mb": msg.get("memory_total_mb", 0),
                "responded_at": datetime.now(timezone.utc).isoformat(),
                "registered": False,
                "registered_worker_id": None,
            }
            logger.info("发现 Worker: %s (%s:%d)", msg.get("name", ""), ip, port)

    try:
        # 发送广播
        for baddr in broadcast_addrs:
            try:
                await loop.sock_sendto(sock, payload, (baddr, discovery_port))
                logger.debug("已发送发现包到 %s:%d", baddr, discovery_port)
            except Exception:
                logger.debug("发送广播到 %s 失败", baddr, exc_info=True)

        # 收包
        deadline = loop.time() + timeout
        await _recv_loop(deadline)
    finally:
        sock.close()

    # 与已注册 Worker 比对
    try:
        from cpustack.db import session_scope
        from cpustack.schemas.workers import Worker
        from sqlmodel import select

        async with session_scope() as session:
            existing = (await session.execute(select(Worker))).scalars().all()
            ip_to_worker = {w.ip: w for w in existing}

        for info in discovered.values():
            w = ip_to_worker.get(info["ip"])
            if w:
                info["registered"] = True
                info["registered_worker_id"] = w.id
                info["registered_name"] = w.name
    except Exception:
        logger.debug("与已注册 Worker 比对失败", exc_info=True)

    result = list(discovered.values())
    result.sort(key=lambda x: (x["ip"], x["port"]))
    logger.info("局域网扫描完成：发现 %d 个 Worker", len(result))
    return result
