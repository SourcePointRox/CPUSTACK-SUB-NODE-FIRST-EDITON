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

from cpustack.config import settings
from cpustack.worker.discovery_listener import DiscoveryListener
from cpustack.worker.serve_manager import ServeManager
from cpustack.worker.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


class Worker:
    """Worker 进程编排。"""

    def __init__(self):
        self.worker_manager = WorkerManager()
        self.serve_manager = ServeManager(self.worker_manager)
        self.discovery_listener = DiscoveryListener(self.worker_manager)
        self._running = False

    async def start(self) -> None:
        """启动 Worker。"""
        logger.info("CPUSTACK Worker 启动中...")

        # 1. 注册（优先加载已保存凭证）
        if not self.worker_manager._load_credentials():
            registered = await self.worker_manager.register()
            if not registered:
                logger.error("Worker 注册失败，退出")
                return
        else:
            logger.info("使用已保存的凭证: uuid=%s", self.worker_manager.worker_uuid)
            # 凭证存在但 Server 可能已重启丢失记录，立即尝试同步一次
            ok = await self.worker_manager.sync_status()
            if not ok:
                logger.warning("使用旧凭证同步失败，尝试重新注册...")
                await self.worker_manager.register()

        # 2. 立即上报一次状态
        await self.worker_manager.sync_status()

        # 3. 启动心跳
        await self.worker_manager.start_heartbeat(interval=15)

        # 4. 启动实例监听
        watch_task = asyncio.create_task(self.serve_manager.watch_instances())

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
        # 停止所有推理进程
        for instance_id in list(self.serve_manager._processes.keys()):
            await self.serve_manager.stop_instance(instance_id)
