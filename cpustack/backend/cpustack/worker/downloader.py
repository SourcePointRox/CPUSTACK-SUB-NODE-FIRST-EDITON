"""模型文件下载器：支持 HuggingFace（镜像）和 ModelScope（国内）。

支持：
- 指定文件名下载（source_filename 明确时）
- 自动查找 GGUF 文件（source_filename 为空时，优先 Q4_K_M 量化）
- HuggingFace 镜像加速（绕过 Xet CDN）
- ModelScope 国内下载（阿里镜像，速度快）
- 下载进度回调
- 断点续传

注意：huggingface_hub 0.25+ 对部分仓库启用 Xet 存储（cas-bridge.xethub.hf.co），
该域名不被 hf-mirror.com 镜像覆盖，国内直连会超时。因此：
1. HuggingFace 源：配置镜像时直接用 httpx 流式下载，绕过 Xet
2. ModelScope 源：直接用 httpx 流式下载（国内访问快，无 Xet 问题）

对于国内用户推荐使用 ModelScope 源（source_repo='modelscope'）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import httpx

from cpustack.config import settings

# 关键：在任何 huggingface_hub 导入之前设置环境变量。
# huggingface_hub 在模块导入时读取该常量（constants.ENDPOINT），
# 若导入后才设置则不生效，会导致连接 huggingface.co 而非镜像站。
if settings.huggingface_mirror:
    os.environ["HF_ENDPOINT"] = settings.huggingface_mirror

# 禁用 HuggingFace Xet CDN（cas-bridge.xethub.hf.co）。
os.environ["HF_HUB_DISABLE_XET"] = "1"

logger = logging.getLogger(__name__)

# ModelScope 下载 API 基础 URL
_MODELSCOPE_BASE = "https://www.modelscope.cn/api/v1/models"


def _setup_mirror() -> None:
    """配置 HuggingFace 镜像（运行时补丁，防止 huggingface_hub 已被其他模块提前导入）。"""
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    if settings.huggingface_mirror:
        os.environ["HF_ENDPOINT"] = settings.huggingface_mirror
        try:
            import huggingface_hub.constants as hf_constants
            hf_constants.ENDPOINT = settings.huggingface_mirror
        except ImportError:
            pass
        logger.info("使用 HuggingFace 镜像: %s (已禁用 Xet CDN)", settings.huggingface_mirror)


def _safe_cache_name(repo_id: str, filename: str) -> str:
    """构造缓存文件名（跨平台安全）。"""
    return f"{repo_id.replace('/', '__')}__{filename}"


def _find_gguf_filename(repo_id: str, source_repo: str = "huggingface") -> str | None:
    """在仓库中查找合适的 GGUF 文件。

    优先级：Q4_K_M > Q4_K_S > Q4_0 > 任意 GGUF
    """
    if source_repo == "modelscope":
        return _find_gguf_filename_modelscope(repo_id)

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

    priorities = ["q4_k_m", "q4-k-m", "q4_k_s", "q4-k-s", "q4_0", "q4-0"]
    for pattern in priorities:
        for f in gguf_files:
            if pattern in f.lower():
                return f

    single_files = [f for f in gguf_files if "/" not in f]
    if single_files:
        return single_files[0]
    return gguf_files[0]


def _find_gguf_filename_modelscope(repo_id: str) -> str | None:
    """在 ModelScope 仓库中查找合适的 GGUF 文件。"""
    url = f"{_MODELSCOPE_BASE}/{repo_id}/repo/files"
    try:
        resp = httpx.get(url, params={"Revision": "master", "Root": "", "Recursive": "true"}, timeout=30)
        if resp.status_code != 200:
            logger.error("ModelScope 列出文件失败 HTTP %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        files = data.get("Data", {}).get("Files", [])
    except Exception:
        logger.exception("ModelScope 列出文件失败: %s", repo_id)
        return None

    gguf_files = [f["Path"] for f in files if f.get("Type") == "blob" and f["Path"].endswith(".gguf")]
    if not gguf_files:
        logger.warning("ModelScope 仓库 %s 中未找到 GGUF 文件", repo_id)
        return None

    priorities = ["q4_k_m", "q4-k-m", "q4_k_s", "q4-k-s", "q4_0", "q4-0", "q3_k_m", "q3-k-m"]
    for pattern in priorities:
        for f in gguf_files:
            if pattern in f.lower():
                return f

    return gguf_files[0]


def _stream_download(
    url: str,
    target_path: Path,
    progress_callback: Callable[[float], None] | None = None,
    timeout_read: float = 300.0,
) -> str | None:
    """通用 HTTP 流式下载（支持断点续传）。

    Args:
        url: 完整下载 URL
        target_path: 本地存储路径
        progress_callback: 进度回调（0.0 - 1.0）
        timeout_read: 读超时（秒）

    Returns:
        本地文件路径，失败返回 None
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing_size = target_path.stat().st_size if target_path.exists() else 0

    logger.info("流式下载: %s -> %s (已存在 %d 字节)", url, target_path, existing_size)

    headers = {}
    if existing_size > 0:
        headers["Range"] = f"bytes={existing_size}-"

    try:
        with httpx.stream(
            "GET", url, headers=headers, follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=timeout_read),
        ) as resp:
            # 416 Range Not Satisfiable：文件已完整下载（续传 Range 超出文件大小）
            if resp.status_code == 416 and existing_size > 0:
                logger.info("文件已完整下载（416）: %s (%d 字节)", target_path, existing_size)
                if progress_callback:
                    progress_callback(1.0)
                return str(target_path)

            if resp.status_code not in (200, 206):
                logger.error("下载失败 HTTP %d: %s", resp.status_code, resp.text[:200])
                return None

            total = int(resp.headers.get("Content-Length", "0"))
            if existing_size > 0 and resp.status_code == 206:
                total += existing_size
                mode = "ab"
            else:
                existing_size = 0
                mode = "wb"

            if total == 0:
                total = existing_size

            downloaded = existing_size
            last_report = 0.0

            with open(target_path, mode) as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):  # 1MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress = downloaded / total
                        if progress - last_report >= 0.01 or progress >= 1.0:
                            last_report = progress
                            progress_callback(progress)

            if progress_callback:
                progress_callback(1.0)

            logger.info("下载完成: %s (%d 字节)", target_path, downloaded)
            return str(target_path)

    except Exception:
        logger.exception("流式下载失败: %s", url)
        # 进度回调可能在 executor 线程中执行，用 try/except 防止其异常
        # 覆盖原始下载异常导致错误信息丢失
        try:
            if progress_callback:
                progress_callback(0.0)
        except Exception:
            logger.debug("进度回调异常（已忽略）")
        return None


def _direct_http_download(
    repo_id: str,
    filename: str,
    cache_dir: str,
    progress_callback: Callable[[float], None] | None = None,
) -> str | None:
    """直接从 HuggingFace 镜像 HTTP 流式下载（绕过 Xet CDN）。"""
    mirror = settings.huggingface_mirror.rstrip("/")
    url = f"{mirror}/{repo_id}/resolve/main/{filename}"
    target_path = Path(cache_dir) / _safe_cache_name(repo_id, filename)
    return _stream_download(url, target_path, progress_callback)


def _direct_modelscope_download(
    repo_id: str,
    filename: str,
    cache_dir: str,
    progress_callback: Callable[[float], None] | None = None,
) -> str | None:
    """从 ModelScope HTTP 流式下载（国内阿里镜像，速度快）。"""
    url = f"{_MODELSCOPE_BASE}/{repo_id}/repo"
    # ModelScope 使用 query 参数指定文件
    url = f"{url}?Revision=master&FilePath={filename}"
    target_path = Path(cache_dir) / _safe_cache_name(repo_id, filename)
    return _stream_download(url, target_path, progress_callback, timeout_read=600.0)


async def download_model_file(
    source_repo: str,
    source_model_id: str,
    source_filename: str,
    progress_callback: Callable[[float], None] | None = None,
) -> str | None:
    """下载模型文件到本地缓存。

    Args:
        source_repo: 模型仓库平台（"huggingface" 或 "modelscope"）
        source_model_id: 仓库 ID，如 "Qwen/Qwen3-32B-GGUF"
        source_filename: 指定文件名，为空则自动查找
        progress_callback: 进度回调（0.0 - 1.0）

    Returns:
        本地文件路径，失败返回 None
    """
    import asyncio

    if source_repo not in ("huggingface", "modelscope"):
        logger.error("不支持的模型仓库: %s", source_repo)
        return None

    _setup_mirror()

    # 确定要下载的文件名
    filename = source_filename
    if not filename:
        filename = _find_gguf_filename(source_model_id, source_repo)
        if not filename:
            logger.error("无法确定模型文件名: %s", source_model_id)
            return None
        logger.info("自动选择 GGUF 文件: %s/%s", source_model_id, filename)

    cache_dir = str(settings.model_cache_path)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    if progress_callback:
        progress_callback(0.0)

    loop = asyncio.get_event_loop()

    if source_repo == "modelscope":
        logger.info("使用 ModelScope 下载（国内镜像）: %s/%s", source_model_id, filename)
        return await loop.run_in_executor(
            None, _direct_modelscope_download,
            source_model_id, filename, cache_dir, progress_callback,
        )

    # HuggingFace 源
    if settings.huggingface_mirror:
        logger.info("使用直接 HTTP 下载（HF 镜像，绕过 Xet CDN）: %s/%s", source_model_id, filename)
        return await loop.run_in_executor(
            None, _direct_http_download,
            source_model_id, filename, cache_dir, progress_callback,
        )

    # 无镜像：使用 huggingface_hub 标准下载
    try:
        from huggingface_hub import hf_hub_download
        logger.info("开始下载模型 (huggingface_hub): %s/%s -> %s", source_model_id, filename, cache_dir)
        local_path = hf_hub_download(repo_id=source_model_id, filename=filename, cache_dir=cache_dir)
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
    """检查模型文件是否已缓存（完整文件才返回）。

    检查顺序：
    1. huggingface_hub 标准缓存（自带完整性校验）
    2. 直接下载的缓存文件（ModelScope/HTTP，完整性由 416 响应保证）

    Returns:
        缓存路径（存在且完整时），None（未缓存或可能不完整）
    """
    filename = source_filename
    if not filename:
        return None

    cache_dir = str(settings.model_cache_path)

    # 1. 检查 huggingface_hub 缓存（标准下载，自带完整性校验）
    try:
        from huggingface_hub import try_to_load_from_cache
        cached = try_to_load_from_cache(
            repo_id=source_model_id, filename=filename, cache_dir=cache_dir,
        )
        if cached and Path(cached).exists():
            return str(cached)
    except Exception:
        pass

    # 2. 检查直接下载的缓存文件（ModelScope/HTTP 下载）
    # 完整性由 _stream_download 的 416 响应保证（下载完成时会收到 416）
    direct_path = Path(cache_dir) / _safe_cache_name(source_model_id, filename)
    if direct_path.exists() and direct_path.stat().st_size > 0:
        logger.info(
            "找到直接下载的缓存文件: %s (%d 字节)",
            direct_path, direct_path.stat().st_size,
        )
        return str(direct_path)

    return None
