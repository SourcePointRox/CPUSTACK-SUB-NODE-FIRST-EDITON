"""推理后端抽象基类：可插拔架构的核心。

所有后端继承 InferenceServer，由 ServeManager 通过工厂模式映射。
借鉴 GPUStack 的 InferenceServer 抽象。
"""

from __future__ import annotations

import abc
import asyncio
import logging
import shutil
from typing import Any

from cpustack.schemas.models import ModelBackend, ModelInstance

logger = logging.getLogger(__name__)


class InferenceServer(abc.ABC):
    """推理后端抽象基类。"""

    def __init__(self, instance: ModelInstance):
        self.instance = instance

    @abc.abstractmethod
    async def start(self, model_file_path: str, port: int) -> asyncio.subprocess.Process | None:
        """启动推理后端进程。

        Args:
            model_file_path: 模型文件本地路径
            port: 服务端口

        Returns:
            进程对象，失败返回 None
        """
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止推理后端。"""
        ...

    @abc.abstractmethod
    def get_health_url(self, port: int) -> str:
        """获取健康检查 URL。"""
        ...


def get_backend(instance: ModelInstance) -> InferenceServer:
    """工厂方法：根据实例的后端类型返回对应实现。"""
    from cpustack.worker.backends.data_parallel import DataParallelServer
    from cpustack.worker.backends.llama_cpp_rpc import LlamaCppRPCServer
    from cpustack.worker.backends.llama_cpp_standalone import LlamaCppStandaloneServer
    from cpustack.worker.backends.prima_cpp import PrimaCppServer

    _SERVER_CLASS_MAPPING: dict[ModelBackend, type[InferenceServer]] = {
        ModelBackend.LLAMA_CPP_STANDALONE: LlamaCppStandaloneServer,
        ModelBackend.LLAMA_CPP_RPC: LlamaCppRPCServer,
        ModelBackend.PRIMA_CPP: PrimaCppServer,
        ModelBackend.DATA_PARALLEL: DataParallelServer,
    }

    cls = _SERVER_CLASS_MAPPING.get(instance.backend if hasattr(instance, 'backend') else None)
    if not cls:
        # 默认使用单机后端
        cls = LlamaCppStandaloneServer

    return cls(instance)


def find_binary(name: str) -> str | None:
    """查找可执行文件路径。"""
    return shutil.which(name)
