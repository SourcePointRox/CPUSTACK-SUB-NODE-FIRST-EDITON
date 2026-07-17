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
    """查找可执行文件路径。

    搜索顺序：
    1. shutil.which (PATH 环境变量)
    2. Windows WinGet 包目录（llama.cpp 常见安装位置）
    3. 常见安装目录
    """
    # 1. PATH 环境变量
    found = shutil.which(name)
    if found:
        return found

    # 2. Windows 专用搜索路径
    import os
    import sys
    from pathlib import Path

    if sys.platform == "win32":
        # WinGet 包目录（llama.cpp 通过 winget install ggml.llamacpp 安装）
        winget_dirs = [
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft"
            / "WinGet"
            / "Packages",
        ]
        # 用户可能手动放置的目录
        extra_dirs = [
            Path("C:\\llama.cpp"),
            Path("C:\\llamacpp"),
            Path("C:\\Program Files\\llama.cpp"),
        ]

        # Windows 可执行文件后缀
        suffixes = [".exe", ""] if not name.endswith(".exe") else [""]

        for base_dir in winget_dirs + extra_dirs:
            if not base_dir.exists():
                continue
            # WinGet 目录下有子目录（如 ggml.llamacpp_Microsoft.Winget.Source_*）
            for sub in [base_dir] + list(base_dir.iterdir()):
                if not sub.is_dir():
                    continue
                for suf in suffixes:
                    candidate = sub / f"{name}{suf}"
                    if candidate.exists():
                        logger.info("find_binary: 在 %s 找到 %s", sub, candidate.name)
                        return str(candidate)

    return None


# junction 缓存：源目录 -> junction 路径，避免重复创建
_JUNCTION_CACHE: dict[str, str] = {}


def ensure_ascii_path(path: str) -> str:
    """确保路径只包含 ASCII 字符（Windows 上 llama.cpp 不支持非 ASCII 路径）。

    Windows 上 llama.cpp (llama-server) 无法打开包含非 ASCII 字符（如中文）
    的文件路径。此函数通过创建 NTFS junction 链接到纯 ASCII 路径来规避该限制。

    junction 路径基于源目录路径哈希，缓存在内存中避免重复创建。

    Args:
        path: 原始文件或目录路径

    Returns:
        ASCII 路径（原路径已是 ASCII 时直接返回原路径）
    """
    import sys
    from pathlib import Path

    if sys.platform != "win32":
        return path

    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass

    source = Path(path).resolve()
    if not source.exists():
        logger.warning("ensure_ascii_path: 路径不存在: %s", path)
        return path

    if source.is_file():
        source_dir = source.parent
        filename = source.name
    else:
        source_dir = source
        filename = ""

    cache_key = str(source_dir).lower()
    if cache_key in _JUNCTION_CACHE:
        junction_dir = Path(_JUNCTION_CACHE[cache_key])
        result = str(junction_dir / filename) if filename else str(junction_dir)
        if Path(result).exists():
            return result

    import hashlib
    import subprocess
    dir_hash = hashlib.md5(str(source_dir).encode("utf-8")).hexdigest()[:8]

    # 候选 junction 路径（优先使用固定路径兼容手动创建）
    candidates = [
        Path(r"C:\cpustack_cache_link"),
        Path(rf"C:\cpustack_link_{dir_hash}"),
    ]

    # 检查已存在的 junction 是否指向正确目标
    for junction_dir in candidates:
        if not junction_dir.exists():
            continue
        try:
            resolved = junction_dir.resolve()
            if str(resolved).lower() == str(source_dir).lower():
                _JUNCTION_CACHE[cache_key] = str(junction_dir)
                result = str(junction_dir / filename) if filename else str(junction_dir)
                logger.info("复用 junction: %s -> %s", junction_dir, source_dir)
                return result
        except Exception:
            pass

    # 创建新 junction（用第一个可用的候选路径）
    for junction_dir in candidates:
        if junction_dir.exists():
            continue
        try:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_dir), str(source_dir)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                _JUNCTION_CACHE[cache_key] = str(junction_dir)
                logger.info("创建 junction: %s -> %s", junction_dir, source_dir)
                result = str(junction_dir / filename) if filename else str(junction_dir)
                return result
            logger.debug("mklink 失败 (路径 %s): %s", junction_dir, result.stderr)
        except Exception as e:
            logger.debug("创建 junction 异常 (路径 %s): %s", junction_dir, e)

    logger.error("创建 junction 失败，返回原路径: %s", path)
    return path
