"""prima.cpp 流水线并行推理后端。

流水线并行（Pipeline Parallelism）：
- 模型按层切分为 N 段，每节点负责一段 layers
- 请求按流水线顺序在节点间传递激活值，真正实现 CPU 算力横向扩展
- 代价：对网络延迟敏感（流水线气泡大小由延迟决定）

架构：
  Client → Master(rank0, layers 0..L1) → Worker1(rank1, L1..L2) → ... → WorkerN(rankN, LN..end)
        ↓ 响应沿原路返回

命令示例（prima.cpp 基于 llama.cpp fork，使用 --split / --rank 参数）：
  # Master（rank 0）：监听服务端口，处理第一段层
  prima-server --model model.gguf --port 40000 --host 0.0.0.0 \
    --layers 0-15 --rank 0

  # Worker（rank 1+）：处理后续层段，连接到 Master
  prima-server --model model.gguf --port 50001 --host 0.0.0.0 \
    --layers 16-31 --rank 1 --master 10.0.0.1:40000
"""

from __future__ import annotations

import asyncio
import logging

from cpustack.worker.backends.base import InferenceServer, find_binary
from cpustack.worker.backends.params import build_common_args, parse_backend_parameters

logger = logging.getLogger(__name__)


async def start_prima_worker(
    port: int,
    layer_start: int,
    layer_end: int,
    rank: int,
    master_addr: str,
    model_file_path: str | None = None,
) -> asyncio.subprocess.Process | None:
    """在 Worker 节点启动 prima-server 从进程（流水线后续段）。

    Args:
        port: 本节点监听端口（用于与 Master 通信）
        layer_start: 负责的起始层
        layer_end: 负责的结束层
        rank: 流水线中的序号（Master=0，Worker 从 1 开始）
        master_addr: Master 地址 host:port
        model_file_path: 模型文件路径（Worker 也需加载模型元数据，部分实现可省略）

    Returns:
        进程对象，失败返回 None
    """
    binary = find_binary("prima-server") or find_binary("prima-server.exe")
    if not binary:
        logger.error("未找到 prima-server 二进制，请确保已安装 prima.cpp")
        return None

    cmd = [
        binary,
        "--host", "0.0.0.0",
        "--port", str(port),
        "--layers", f"{layer_start}-{layer_end}",
        "--rank", str(rank),
        "--master", master_addr,
    ]
    if model_file_path:
        cmd.extend(["--model", model_file_path])

    logger.info(
        "启动 prima-server Worker: rank=%d layers=%d-%d master=%s",
        rank, layer_start, layer_end, master_addr,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(
            "prima-server Worker 已启动 (PID %d, rank %d, 端口 %d)",
            process.pid, rank, port,
        )
        return process
    except Exception:
        logger.exception("启动 prima-server Worker 失败")
        return None


class PrimaCppServer(InferenceServer):
    """prima.cpp 流水线并行推理后端（Master 端，rank 0）。

    Master 节点：
    - 监听对外服务端口（OpenAI 兼容 API）
    - 处理模型的第一段层（layer 0 .. L1）
    - 将激活值按流水线顺序传递给后续 Worker 节点
    - 接收最终输出并返回给客户端
    """

    async def start(
        self,
        model_file_path: str,
        port: int,
        pipeline_workers: list[dict] | None = None,
        backend_parameters: dict | None = None,
    ) -> asyncio.subprocess.Process | None:
        """启动 prima-server Master 进程（rank 0，第一段层）。

        Args:
            model_file_path: 模型文件本地路径
            port: 对外服务端口（OpenAI 兼容 API）
            pipeline_workers: 流水线后续 Worker 节点信息列表
                [{"worker_id":2, "layer_start":16, "layer_end":31,
                  "rank":1, "ip":"10.0.0.2", "port":50002}, ...]
            backend_parameters: 算法优化参数（可选，缺省从 instance.backend_parameters 读取）

        Returns:
            进程对象，失败返回 None
        """
        binary = find_binary("prima-server") or find_binary("prima-server.exe")
        if not binary:
            logger.error("未找到 prima-server 二进制")
            return None

        # 计算本 Master 节点负责的层段（第一段）
        # 层分配由调度器计算并传入，Master 总是 rank 0 处理第一段
        master_layer_end = 0
        if pipeline_workers:
            # 第一个 Worker 的 layer_start 即 Master 的 layer_end+1 的边界
            master_layer_end = pipeline_workers[0].get("layer_start", 0) - 1
            if master_layer_end < 0:
                master_layer_end = 0

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
            "--layers", f"0-{master_layer_end}",
            "--rank", "0",
        ]
        cmd.extend(build_common_args(params, self.instance))

        # 添加流水线后续节点信息（prima.cpp 通过 --peers 或逐个 --worker 指定）
        if pipeline_workers:
            for w in pipeline_workers:
                cmd.extend([
                    "--worker",
                    f"{w['ip']}:{w['port']}",
                    "--worker-layers",
                    f"{w['layer_start']}-{w['layer_end']}",
                    "--worker-rank",
                    str(w["rank"]),
                ])
            logger.info(
                "流水线并行 Master: 处理层 0-%d, %d 个 Worker 节点",
                master_layer_end, len(pipeline_workers),
            )

        logger.info("启动 prima-server Master: %s", " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("prima-server Master 已启动 (PID %d, 端口 %d)", process.pid, port)
            return process
        except Exception:
            logger.exception("启动 prima-server Master 失败")
            return None

    async def stop(self) -> None:
        pass

    def get_health_url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}/health"


def compute_layer_split(
    total_layers: int,
    node_capacities: list[tuple[int, int]],
) -> list[dict]:
    """层切片分配策略：按节点 CPU 核心数比例分配层数。

    Args:
        total_layers: 模型总层数
        node_capacities: [(worker_id, cpu_cores), ...] 按 pipeline 顺序

    Returns:
        [{"worker_id":id, "layer_start":s, "layer_end":e, "rank":r}, ...]
        长度与 node_capacities 相同，层段连续且覆盖 [0, total_layers)
    """
    if not node_capacities or total_layers <= 0:
        return []

    total_cores = sum(c for _, c in node_capacities)
    if total_cores <= 0:
        # 核心数未知，均分
        per_node = total_layers // len(node_capacities)
        result = []
        for rank, (wid, _) in enumerate(node_capacities):
            start = rank * per_node
            end = (start + per_node - 1) if rank < len(node_capacities) - 1 else (total_layers - 1)
            result.append({
                "worker_id": wid,
                "layer_start": start,
                "layer_end": end,
                "rank": rank,
            })
        return result

    result = []
    allocated = 0
    for rank, (wid, cores) in enumerate(node_capacities):
        if rank == len(node_capacities) - 1:
            # 最后一个节点负责剩余所有层
            start = allocated
            end = total_layers - 1
        else:
            # 按核心比例分配（至少 1 层）
            share = max(1, round(total_layers * cores / total_cores))
            start = allocated
            end = min(start + share - 1, total_layers - 1)
            allocated = end + 1
        result.append({
            "worker_id": wid,
            "layer_start": start,
            "layer_end": end,
            "rank": rank,
        })
    return result
