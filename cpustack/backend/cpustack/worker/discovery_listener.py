"""Worker 端局域网发现监听器。

监听 UDP 端口（默认 30090），收到 Server 广播的探测包后回包，
告知 Server 本节点的名称、IP、端口等信息。

被动响应，无需主动注册，实现"开机即被发现"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

from cpustack.config import settings

logger = logging.getLogger(__name__)


class DiscoveryListener:
    """Worker 端 UDP 发现监听器。"""

    def __init__(self, worker_manager) -> None:
        self._wm = worker_manager
        self._task: asyncio.Task | None = None
        self._running = False
        self._sock: socket.socket | None = None

    async def start(self) -> None:
        """启动 UDP 监听。"""
        if self._running:
            return
        self._running = True

        port = settings.discovery_port
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(False)
            # 绑定到所有接口，监听广播
            sock.bind(("0.0.0.0", port))
            self._sock = sock
            self._task = asyncio.create_task(self._listen_loop())
            logger.info(
                "局域网发现监听已启动 (UDP 端口 %d)，等待 Server 扫描", port
            )
        except OSError as e:
            # 端口占用不致命，仅记录
            logger.warning("启动发现监听失败（端口 %d 可能被占用）: %s", port, e)
            self._running = False

    async def stop(self) -> None:
        """停止监听。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._sock:
            self._sock.close()
            self._sock = None

    async def _listen_loop(self) -> None:
        """监听循环：收到探测包后回包。"""
        loop = asyncio.get_event_loop()
        sock = self._sock
        if sock is None:
            return

        while self._running:
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096), timeout=2.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("发现监听接收异常", exc_info=True)
                continue

            try:
                msg = json.loads(data.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue

            if msg.get("type") != "cpustack_discovery":
                continue

            # 校验 token（若 Server 配置了 token）
            expected_token = settings.worker_token
            if expected_token and msg.get("token") != expected_token:
                logger.debug(
                    "发现包 token 不匹配，来源 %s，忽略", addr[0]
                )
                continue

            # 构造响应
            response = self._build_response()
            try:
                await loop.sock_sendto(
                    sock,
                    json.dumps(response).encode("utf-8"),
                    addr,
                )
                logger.debug("已回复发现探测到 %s", addr[0])
            except Exception:
                logger.debug("回复发现探测失败", exc_info=True)

    def _build_response(self) -> dict:
        """构造发现响应包。"""
        import socket as _socket

        hostname = _socket.gethostname()
        # 优先用 WorkerManager 已注册的 IP，回退到本机 IP
        ip = getattr(self._wm, "_registered_ip", None) or self._wm._get_local_ip()
        name = settings.worker_name or hostname
        port = settings.worker_port

        # 附加资源信息（轻量采集，避免阻塞）
        cpu_cores = 0
        memory_total_mb = 0
        try:
            import psutil

            cpu_cores = psutil.cpu_count(logical=True) or 0
            memory_total_mb = int(psutil.virtual_memory().total / (1024 * 1024))
        except Exception:
            pass

        return {
            "type": "cpustack_discovery_response",
            "name": name,
            "ip": ip,
            "port": port,
            "worker_port": port,
            "hostname": hostname,
            "cpu_cores": cpu_cores,
            "memory_total_mb": memory_total_mb,
        }
