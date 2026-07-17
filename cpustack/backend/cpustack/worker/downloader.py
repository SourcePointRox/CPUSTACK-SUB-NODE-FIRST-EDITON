"""模型文件下载器：HuggingFace Hub 集成。

支持：
- 指定文件名下载（source_filename 明确时）
- 自动查找 GGUF 文件（source_filename 为空时，优先 Q4_K_M 量化）
- HuggingFace 镜像加速
- 下载进度回调
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from cpustack.config import settings

# 关键：在任何 huggingface_hub 导入之前设置 HF_ENDPOINT 环境变量。
# huggingface_hub 在模块导入时读取该常量（constants.ENDPOINT），
# 若导入后才设置则不生效，会导致连接 huggingface.co 而非镜像站。
if settings.huggingface_mirror:
    os.environ["HF_ENDPOINT"] = settings.huggingface_mirror

logger = logging.getLogger(__name__)


def _setup_mirror() -> None:
    """配置 HuggingFace 镜像（运行时补丁，防止 huggingface_hub 已被其他模块提前导入）。"""
    if settings.huggingface_mirror:
        os.environ["HF_ENDPOINT"] = settings.huggingface_mirror
        # 若 huggingface_hub 已被导入，直接 patch 其常量
        try:
            import huggingface_hub.constants as hf_constants
            hf_constants.ENDPOINT = settings.huggingface_mirror
        except ImportError:
            pass
        logger.info("使用 HuggingFace 镜像: %s", settings.huggingface_mirror)


def _find_gguf_filename(repo_id: str) -> str | None:
    """在仓库中查找合适的 GGUF 文件。

    优先级：Q4_K_M > Q4_K_S > Q4_0 > 任意 GGUF
    """
    from huggingface_hub import list_repo_files

    try:
        files = list_repo_files(repo_id=repo_id)
    except Exception:
        logger.exception("列出仓库文件失败: %s", repo_id)
        return None

    gguf_files = [f for f in files if f.endswith(".gguf")]
    if not gguf_files:
        logger.warning("仓库 %s 中未找到 GGUF 文件", repo_id)
        return None

    # 优先级匹配
    priorities = ["q4_k_m", "q4-k-m", "q4_k_s", "q4-k-s", "q4_0", "q4-0"]
    for pattern in priorities:
        for f in gguf_files:
            if pattern in f.lower():
                return f

    # 回退：取第一个 GGUF 文件（优先非量化分割文件）
    single_files = [f for f in gguf_files if "/" not in f]
    if single_files:
        return single_files[0]
    return gguf_files[0]


async def download_model_file(
    source_repo: str,
    source_model_id: str,
    source_filename: str,
    progress_callback: Callable[[float], None] | None = None,
) -> str | None:
    """下载模型文件到本地缓存。

    Args:
        source_repo: 模型仓库平台（目前仅支持 huggingface）
        source_model_id: 仓库 ID，如 "Qwen/Qwen2.5-3B-Instruct-GGUF"
        source_filename: 指定文件名，为空则自动查找
        progress_callback: 进度回调（0.0 - 1.0）

    Returns:
        本地文件路径，失败返回 None
    """
    if source_repo != "huggingface":
        logger.error("不支持的模型仓库: %s", source_repo)
        return None

    _setup_mirror()

    # 确定要下载的文件名
    filename = source_filename
    if not filename:
        filename = _find_gguf_filename(source_model_id)
        if not filename:
            logger.error("无法确定模型文件名: %s", source_model_id)
            return None
        logger.info("自动选择 GGUF 文件: %s/%s", source_model_id, filename)

    cache_dir = str(settings.model_cache_path)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    if progress_callback:
        progress_callback(0.0)

    try:
        from huggingface_hub import hf_hub_download

        logger.info("开始下载模型: %s/%s -> %s", source_model_id, filename, cache_dir)

        # hf_hub_download 支持断点续传和缓存
        local_path = hf_hub_download(
            repo_id=source_model_id,
            filename=filename,
            cache_dir=cache_dir,
        )

        if progress_callback:
            progress_callback(1.0)

        logger.info("模型下载完成: %s", local_path)
        return local_path

    except Exception:
        logger.exception("模型下载失败: %s/%s", source_model_id, filename)
        if progress_callback:
            progress_callback(0.0)
        return None


def get_cached_model_path(source_model_id: str, source_filename: str) -> str | None:
    """检查模型文件是否已缓存。

    Returns:
        缓存路径（存在时），None（未缓存）
    """
    from huggingface_hub import try_to_load_from_cache

    filename = source_filename
    if not filename:
        # 无法在不访问网络的情况下确定文件名
        return None

    try:
        cached = try_to_load_from_cache(
            repo_id=source_model_id,
            filename=filename,
            cache_dir=str(settings.model_cache_path),
        )
        if cached and Path(cached).exists():
            return str(cached)
    except Exception:
        pass
    return None
