"""Worker 启动编排。

启动序列：
1. 硬件检测（本地 IP、网络接口）
2. 注册到 Server（Token 握手）
3. 启动心跳循环（状态上报）
4. 启动实例监听（ServeManager）
5. 启动局域网发现监听（被动响应 Server 扫描）
"""

from __future__ import annotations

import asyncio
import logging
import sys

from cpustack.config import settings
from cpustack.worker.discovery_listener import DiscoveryListener
from cpustack.worker.serve_manager import ServeManager
from cpustack.worker.worker_http import start_worker_http
from cpustack.worker.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


def _ensure_firewall_rules(worker_id: int) -> None:
    """Windows 上自动添加防火墙规则，允许 RPC 端口和内部 HTTP 端口入站。

    RPC 端口约定：50000 + worker_id
    内部 HTTP 端口：settings.worker_port (默认 30080)

    需要管理员权限；非管理员时静默跳过（记录警告）。
    """
    if sys.platform != "win32":
        return

    import subprocess

    ports = [
        (50000 + worker_id, f"CPUSTACK RPC Worker {worker_id}"),
        (settings.worker_port, "CPUSTACK Worker HTTP"),
    ]

    for port, rule_name in ports:
        try:
            # 检查规则是否已存在
            check = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
                capture_output=True, text=True, timeout=10,
            )
            if check.returncode == 0 and rule_name in check.stdout:
                logger.info("防火墙规则已存在: %s (端口 %d)", rule_name, port)
                continue

            # 添加入站规则
            result = subprocess.run(
                [
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={rule_name}",
                    "dir=in", "action=allow", "protocol=TCP",
                    f"localport={port}",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                logger.info("已添加防火墙规则: %s (端口 %d)", rule_name, port)
            else:
                logger.warning(
                    "添加防火墙规则失败: %s (端口 %d) — %s。"
                    "请以管理员身份运行或手动添加规则。",
                    rule_name, port, result.stderr.strip(),
                )
        except Exception as e:
            logger.warning("配置防火墙异常: %s (端口 %d) — %s", rule_name, port, e)


class Worker:
    """Worker 进程编排。"""

    def __init__(self):
        self.worker_manager = WorkerManager()
        self.serve_manager = ServeManager(self.worker_manager)
        self.discovery_listener = DiscoveryListener(self.worker_manager)
        self._running = False
        self._http_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动 Worker。"""
        logger.info("CPUSTACK Worker 启动中...")

        # 0. 启动内部 HTTP 服务（支持主节点一键接管注册）
        #    即使初始注册失败，主节点扫描发现后也能通过 /internal/register 触发注册
        self._http_task = await start_worker_http(self.worker_manager)

        # 1. 注册（优先加载已保存凭证）
        if not self.worker_manager._load_credentials():
            registered = await self.worker_manager.register()
            if not registered:
                logger.warning(
                    "Worker 初始注册失败，但内部 HTTP 服务已启动，"
                    "可等待主节点通过「一键添加」接管注册"
                )
            # 注册失败不退出：保留进程，等待主节点一键接管
        else:
            logger.info("使用已保存的凭证: uuid=%s", self.worker_manager.worker_uuid)
            # 凭证存在但 Server 可能已重启丢失记录，立即尝试同步一次
            ok = await self.worker_manager.sync_status()
            if not ok:
                logger.warning("使用旧凭证同步失败，尝试重新注册...")
                await self.worker_manager.register()

        # 2. 立即上报一次状态
        await self.worker_manager.sync_status()

        # 2.5 配置防火墙规则（Windows 上允许 RPC 端口入站）
        if self.worker_manager.worker_id is not None:
            _ensure_firewall_rules(self.worker_manager.worker_id)

        # 3. 启动心跳
        await self.worker_manager.start_heartbeat(interval=15)

        # 4. 启动实例监听
        self._watch_task = asyncio.create_task(self.serve_manager.watch_instances())

        # 5. 启动局域网发现监听（被动响应 Server 扫描）
        await self.discovery_listener.start()

        self._running = True
        logger.info("Worker 已就绪，等待任务分配...")

        # 保持运行
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        """停止 Worker。"""
        logger.info("Worker 停止中...")
        self._running = False
        # 停止发现监听
        await self.discovery_listener.stop()
        # 停止内部 HTTP 服务
        if self._http_task and not self._http_task.done():
            self._http_task.cancel()
            try:
                await self._http_task
            except asyncio.CancelledError:
                pass
        # 停止实例监听
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
        # 停止所有推理进程
        for instance_id in list(self.serve_manager._processes.keys()):
            await self.serve_manager.stop_instance(instance_id)
