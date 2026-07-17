"""CPU 指令集检测：AVX2 / AVX-512 / AMX / VNNI / BF16。

这是 CPUSTACK 的核心差异化能力：
- AVX2：256-bit SIMD，CPU 推理基础
- AVX-512：512-bit SIMD，较 AVX2 提升 23%+
- AMX：矩阵加速，Intel 至强 SPR+ 革命性提升
- VNNI：INT8 点积加速
- BF16：BF16 计算

注意 AVX-512 降频陷阱：Cascade Lake 上原生 AVX-512 内核可能因
频率降频反而比 AVX2 慢 18-31%，需实测验证。
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _detect_linux_instruction_sets() -> list[str]:
    """Linux 下通过 /proc/cpuinfo 检测指令集。"""
    supported = []
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            flags_line = ""
            for line in f:
                if line.startswith("flags"):
                    flags_line = line
                    break

        flags = flags_line.split(":", 1)[-1].strip().split() if flags_line else []

        if "avx2" in flags:
            supported.append("AVX2")
        if "avx512f" in flags:
            supported.append("AVX-512")
        if "avx512_vnni" in flags:
            supported.append("AVX512_VNNI")
        if "amx_bf16" in flags or "amx_tile" in flags:
            supported.append("AMX")
        if "avx_vnni" in flags:
            supported.append("AVX_VNNI")
    except Exception:
        logger.exception("Linux 指令集检测失败")
    return supported


def _detect_windows_instruction_sets() -> list[str]:
    """Windows 下通过 WSL2 内的 /proc/cpuinfo 或 coreinfo 检测。

    在容器内（Linux 容器），仍走 Linux 检测路径。
    此函数用于原生 Windows 调试场景。
    """
    supported = []
    try:
        # 尝试通过 WSL 检测
        wsl_path = shutil.which("wsl")
        if wsl_path:
            result = subprocess.run(
                ["wsl", "grep", "-o", "-E", "avx2|avx512f|amx_bf16|avx_vnni", "/proc/cpuinfo"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = result.stdout
            if "avx2" in output:
                supported.append("AVX2")
            if "avx512f" in output:
                supported.append("AVX-512")
            if "amx_bf16" in output:
                supported.append("AMX")
            if "avx_vnni" in output:
                supported.append("AVX_VNNI")
    except Exception:
        logger.debug("Windows/WSL 指令集检测失败，可能是非 Windows 宿主")
    return supported


def detect_instruction_sets() -> list[str]:
    """检测当前环境支持的 CPU 指令集。

    返回排序后的指令集列表，如 ["AVX2", "AVX-512", "AMX"]。
    """
    system = platform.system().lower()
    if system == "linux":
        return _detect_linux_instruction_sets()
    elif system == "windows":
        return _detect_windows_instruction_sets()
    else:
        logger.warning("不支持的操作系统进行指令集检测: %s", system)
        return []


def meets_requirement(supported: list[str], required: list[str]) -> bool:
    """检查节点是否满足模型的指令集要求。

    所有 required 指令集都必须在 supported 中。
    """
    supported_set = set(supported)
    return all(req in supported_set for req in required)
