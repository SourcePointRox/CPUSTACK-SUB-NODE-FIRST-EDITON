"""数据并行推理后端。

数据并行（Data Parallelism）：
- 每节点加载完整模型副本，独立处理请求
- 请求由网关层负载均衡器分发到各副本
- 线性扩展并发吞吐（N 副本 ≈ N 倍 QPS）
- 代价：不加速单请求延迟，内存占用 ×N

与流水线并行/RPC 的区别：
- 流水线/RPC：单实例跨多节点，加速单请求
- 数据并行：多实例各占一节点，提升并发吞吐

实现说明：
- 数据并行的每个副本本质是一个独立的 llama-server 实例
- 后端进程复用 LlamaCppStandaloneServer（完整模型，无分布式参数）
- DataParallelServer 作为标记类，调度器据此创建 N 个副本实例
- 负载均衡在网关层（load_balancer.py）完成，非后端进程内

调度模型：
  模型 replicas=3 + backend=data_parallel
    → 调度器创建 3 个 ModelInstance（各自独立的 worker_id）
    → 每个 Worker 运行一个 LlamaCppStandaloneServer
    → 网关 _select_instance 在 3 个 RUNNING 实例间负载均衡
"""

from __future__ import annotations

import logging

from cpustack.worker.backends.base import InferenceServer
from cpustack.worker.backends.llama_cpp_standalone import LlamaCppStandaloneServer

logger = logging.getLogger(__name__)


class DataParallelServer(InferenceServer):
    """数据并行后端（标记类，实际进程复用 LlamaCppStandaloneServer）。

    数据并行模式下，调度器会为模型创建 N 个独立实例（N = model.replicas），
    每个实例在各自 Worker 上运行一个完整的 llama-server。
    因此本类的 start/stop 直接委托给 LlamaCppStandaloneServer。

    负载均衡逻辑不在后端进程内，而在网关层 load_balancer.py 实现。
    """

    def __init__(self, instance):
        super().__init__(instance)
        # 委托给单机后端（每个副本就是一个独立 llama-server）
        self._delegate = LlamaCppStandaloneServer(instance)

    async def start(
        self,
        model_file_path: str,
        port: int,
        backend_parameters: dict | None = None,
    ):
        """启动一个数据并行副本（等价于单机 llama-server）。"""
        logger.info(
            "启动数据并行副本: 实例 %s (端口 %d)",
            getattr(self.instance, "name", "?"), port,
        )
        return await self._delegate.start(model_file_path, port, backend_parameters)

    async def stop(self) -> None:
        await self._delegate.stop()

    def get_health_url(self, port: int) -> str:
        return self._delegate.get_health_url(port)
