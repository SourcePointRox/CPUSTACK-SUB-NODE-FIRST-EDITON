"""llama.cpp 单机推理后端。

使用 llama-server 二进制提供 OpenAI 兼容 API。
这是最基础的后端，用于单节点小模型部署。

命令示例：
  llama-server --model /path/to/model.gguf --port 40000 --host 0.0.0.0

阶段5 算法优化（通过 Model.backend_parameters 传参，见 backends/params.py）：
  - 连续批处理（cont_batching）：吞吐 2-5×
  - 投机解码（draft_model）：延迟降 2-3×
  - Flash Attention（flash_attn）：降低内存、加速长序列
  - 前缀缓存复用（cache_reuse）：降 TTFT
"""

from __future__ import annotations

import asyncio
import logging

from cpustack.worker.backends.base import InferenceServer, ensure_ascii_path, find_binary
from cpustack.worker.backends.params import build_common_args, parse_backend_parameters

logger = logging.getLogger(__name__)


class LlamaCppStandaloneServer(InferenceServer):
    """llama.cpp 单机推理后端。"""

    async def start(
        self,
        model_file_path: str,
        port: int,
        backend_parameters: dict | None = None,
    ) -> asyncio.subprocess.Process | None:
        """启动 llama-server 进程。

        Args:
            model_file_path: 模型文件本地路径
            port: 服务端口
            backend_parameters: 算法优化参数（可选，缺省从 instance.backend_parameters 读取）
        """
        binary = find_binary("llama-server") or find_binary("llama-server.exe")
        if not binary:
            logger.error("未找到 llama-server 二进制，请确保已安装 llama.cpp")
            return None

        # Windows 上 llama.cpp 不支持非 ASCII 路径，自动创建 junction 规避
        model_file_path = ensure_ascii_path(model_file_path)

        # 优先使用显式传入的参数，回退到 instance 上的属性
        params = backend_parameters
        if params is None:
            params = parse_backend_parameters(
                getattr(self.instance, "backend_parameters", None)
            )

        cmd = [
            binary,
            "--model", model_file_path,
            "--port", str(port),
            "--host", "0.0.0.0",
        ]
        cmd.extend(build_common_args(params, self.instance))

        logger.info("启动 llama-server: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("llama-server 已启动 (PID %d)", process.pid)
            return process
        except Exception:
            logger.exception("启动 llama-server 失败")
            return None

    async def stop(self) -> None:
        """停止由 ServeManager 管理，此处无需实现。"""
        pass

    def get_health_url(self, port: int) -> str:
        """llama-server 健康检查端点。"""
        return f"http://127.0.0.1:{port}/health"
