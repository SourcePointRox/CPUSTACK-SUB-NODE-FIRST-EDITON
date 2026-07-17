"""推理后端参数构建器：从 backend_parameters JSON 构建命令行参数。

支持阶段5算法优化参数（通过 Model.backend_parameters JSON 传递）：

基本参数:
    ctx_size: int           上下文大小（默认 4096）
    n_batch: int            批处理大小（--batch-size）
    n_ubatch: int           微批处理大小（--ubatch-size）
    threads: int            线程数（默认取 instance.allocated_cpu_cores）

性能优化（规划文档 §11.3）:
    flash_attn: bool        Flash Attention（降低内存、加速长序列）
    mlock: bool             锁定内存（防止 swap 导致性能抖动）
    cont_batching: bool     连续批处理（吞吐 2-5×，规划文档预期收益）
    parallel: int           并行序列数（配合 cont_batching 使用）
    cache_reuse: int        前缀缓存复用槽位（降 TTFT）
    n_gpu_layers: int       GPU 层数（CPU 平台默认 0）

投机解码（降延迟 2-3×）:
    draft_model: str        草稿模型路径（--model-draft）
    draft_max_tokens: int   单次投机最大 token（--draft-max，默认 16）
    draft_min_p: float      草稿接受最小概率（--draft-min-p）

扩展:
    extra_args: list[str]   额外透传参数（高级用法）

设计原则：
- 参数全部可选，缺省时保持阶段4行为（向后兼容）
- 草稿模型路径若不存在则跳过投机解码（避免启动失败）
- extra_args 仅追加，不做合法性校验（运维责任）
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def build_common_args(params: dict, instance) -> list[str]:
    """构建 llama-server / prima-server 通用参数。

    Args:
        params: backend_parameters 解析后的字典（可为空）
        instance: ModelInstance（用于 allocated_cpu_cores）

    Returns:
        命令行参数列表（不含 --model/--port/--host 等位置相关参数）
    """
    args: list[str] = []

    # 上下文大小（默认 4096，与阶段4保持一致）
    ctx_size = _as_int(params.get("ctx_size"), 4096)
    args.extend(["--ctx-size", str(ctx_size)])

    # 线程数（默认取实例分配的核心数，回退 4）
    threads = _as_int(
        getattr(instance, "allocated_cpu_cores", None) or params.get("threads"), 4
    )
    args.extend(["--threads", str(threads)])

    # 批处理大小
    n_batch = params.get("n_batch")
    if n_batch is not None:
        args.extend(["--batch-size", str(_as_int(n_batch, 512))])

    # 微批处理大小
    n_ubatch = params.get("n_ubatch")
    if n_ubatch is not None:
        args.extend(["--ubatch-size", str(_as_int(n_ubatch, 256))])

    # Flash Attention
    if _as_bool(params.get("flash_attn")):
        args.append("--flash-attn")

    # 锁定内存
    if _as_bool(params.get("mlock")):
        args.append("--mlock")

    # 连续批处理（吞吐 2-5×）
    if _as_bool(params.get("cont_batching")):
        args.append("--cont-batching")
        parallel = _as_int(params.get("parallel"), 0)
        if parallel > 0:
            args.extend(["--parallel", str(parallel)])

    # 前缀缓存复用
    cache_reuse = params.get("cache_reuse")
    if cache_reuse is not None:
        args.extend(["--cache-reuse", str(_as_int(cache_reuse, 256))])

    # GPU 层数（CPU 平台默认 0，留作异构节点兼容）
    n_gpu_layers = _as_int(params.get("n_gpu_layers"), 0)
    if n_gpu_layers > 0:
        args.extend(["--n-gpu-layers", str(n_gpu_layers)])

    # 投机解码（草稿模型路径必须存在才启用）
    draft_model = params.get("draft_model")
    if draft_model and os.path.exists(draft_model):
        args.extend(["--model-draft", draft_model])
        args.extend(["--draft-max", str(_as_int(params.get("draft_max_tokens"), 16))])
        draft_min_p = params.get("draft_min_p")
        if draft_min_p is not None:
            try:
                args.extend(["--draft-min-p", f"{float(draft_min_p):.4f}"])
            except (TypeError, ValueError):
                pass
    elif draft_model:
        logger.warning(
            "草稿模型路径不存在，跳过投机解码: %s",
            draft_model,
        )

    # 额外参数透传（运维自定义，如 --no-mmap --numa 等）
    extra_args = params.get("extra_args")
    if isinstance(extra_args, list):
        for a in extra_args:
            if isinstance(a, str) and a:
                args.append(a)

    return args


def parse_backend_parameters(raw: str | None | dict) -> dict:
    """解析 backend_parameters（兼容 JSON 字符串、dict、None）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        import json

        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("backend_parameters 解析失败: %r", raw)
        return {}
