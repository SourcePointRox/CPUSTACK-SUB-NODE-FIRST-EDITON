"""llama.cpp RPC 分布式推理后端。

RPC 模式通过内存池化实现跨节点部署：
- Master 节点：运行 llama-server，加载模型，通过 --rpc 连接 Slave
- Slave 节点：运行 rpc-server，贡献本机内存供 Master 远程使用

命令示例：
  # Slave 节点：
  rpc-server --host 0.0.0.0 --port 50000

  # Master 节点：
  llama-server --model model.gguf --port 40000 --host 0.0.0.0 \
    --rpc 192.168.1.101:50000,192.168.1.102:50000
"""

from __future__ import annotations

import asyncio
import logging

from cpustack.worker.backends.base import InferenceServer, ensure_ascii_path, find_binary
from cpustack.worker.backends.params import build_common_args, parse_backend_parameters

logger = logging.getLogger(__name__)


async def start_rpc_server(port: int) -> asyncio.subprocess.Process | None:
    """在 Slave 节点启动 rpc-server 进程。

    Args:
        port: rpc-server 监听端口

    Returns:
        进程对象，失败返回 None
    """
    binary = find_binary("rpc-server") or find_binary("llama-rpc-server")
    if not binary:
        logger.error("未找到 rpc-server 二进制，请确保已安装 llama.cpp（含 RPC 支持）")
        return None

    cmd = [
        binary,
        "--host", "0.0.0.0",
        "--port", str(port),
    ]

    logger.info("启动 rpc-server: %s", " ".join(cmd))

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info("rpc-server 已启动 (PID %d, 端口 %d)", process.pid, port)
        return process
    except Exception:
        logger.exception("启动 rpc-server 失败")
        return None


class LlamaCppRPCServer(InferenceServer):
    """llama.cpp RPC 分布式推理后端（Master 端）。

    在 Master 节点上运行 llama-server，通过 --rpc 参数连接所有 Slave 节点。
    Slave 节点的 rpc-server 进程由 ServeManager 直接管理。
    """

    async def start(
        self,
        model_file_path: str,
        port: int,
        rpc_peers: list[str] | None = None,
        backend_parameters: dict | None = None,
    ) -> asyncio.subprocess.Process | None:
        """启动 llama-server 进程（Master 模式）。

        Args:
            model_file_path: 模型文件本地路径
            port: llama-server 服务端口
            rpc_peers: Slave 节点地址列表 ["ip:port", ...]
            backend_parameters: 算法优化参数（可选，缺省从 instance.backend_parameters 读取）
        """
        binary = find_binary("llama-server") or find_binary("llama-server.exe")
        if not binary:
            logger.error("未找到 llama-server 二进制")
            return None

        # Windows 上 llama.cpp 不支持非 ASCII 路径，自动创建 junction 规避
        model_file_path = ensure_ascii_path(model_file_path)

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

        # RPC 模式：添加 --rpc 参数连接 Slave 节点
        if rpc_peers:
            cmd.extend(["--rpc", ",".join(rpc_peers)])
            logger.info(
                "RPC Master 模式: %d 个 Slave 节点: %s",
                len(rpc_peers),
                rpc_peers,
            )

        logger.info("启动 llama-server (RPC): %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("llama-server (RPC) 已启动 (PID %d)", process.pid)
            return process
        except Exception:
            logger.exception("启动 llama-server (RPC) 失败")
            return None

    async def stop(self) -> None:
        pass

    def get_health_url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}/health"
