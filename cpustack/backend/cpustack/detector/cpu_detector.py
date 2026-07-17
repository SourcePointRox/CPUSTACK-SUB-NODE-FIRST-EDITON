"""CPU 检测：核心数、型号、NUMA 拓扑。"""

from __future__ import annotations

import logging
import multiprocessing
import platform
import shutil
import subprocess

import psutil

logger = logging.getLogger(__name__)


def get_cpu_info() -> dict:
    """获取 CPU 信息。

    Returns:
        包含 cpu_arch, cpu_model, cpu_cores, numa_nodes 的字典
    """
    cpu_cores = psutil.cpu_count(logical=True) or multiprocessing.cpu_count()

    cpu_model = ""
    numa_nodes = 1

    system = platform.system().lower()
    if system == "linux":
        cpu_model, numa_nodes = _get_linux_cpu_info()
    elif system == "windows":
        cpu_model = _get_windows_cpu_model()

    return {
        "cpu_arch": platform.machine() or "x86_64",
        "cpu_model": cpu_model,
        "cpu_cores": cpu_cores,
        "numa_nodes": numa_nodes,
    }


def _get_linux_cpu_info() -> tuple[str, int]:
    """Linux 下获取 CPU 型号和 NUMA 节点数。"""
    cpu_model = ""
    numa_nodes = 1

    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[-1].strip()
                    break
    except Exception:
        logger.debug("读取 /proc/cpuinfo 失败")

    # NUMA 节点数
    lscpu_path = shutil.which("lscpu")
    if lscpu_path:
        try:
            result = subprocess.run(
                [lscpu_path], capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                if "NUMA node(s):" in line:
                    numa_nodes = int(line.split(":")[-1].strip())
                    break
        except Exception:
            logger.debug("lscpu 执行失败")

    return cpu_model, numa_nodes


def _get_windows_cpu_model() -> str:
    """Windows 下获取 CPU 型号。"""
    try:
        result = subprocess.run(
            ["wmic", "cpu", "get", "name"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        if len(lines) > 1:
            return lines[1]
    except Exception:
        logger.debug("wmic 执行失败")
    return ""


def get_cpu_utilization() -> float:
    """获取 CPU 利用率（0-100）。"""
    return psutil.cpu_percent(interval=0.1)
