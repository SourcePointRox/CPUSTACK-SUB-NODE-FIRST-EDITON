"""内存与磁盘检测。"""

from __future__ import annotations

import logging

import psutil

logger = logging.getLogger(__name__)


def get_memory_info() -> dict:
    """获取内存信息。

    Returns:
        memory_total, memory_available, swap_total, swap_used (单位 MB)
    """
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    return {
        "memory_total": int(mem.total / 1024 / 1024),  # bytes -> MB
        "memory_available": int(mem.available / 1024 / 1024),
        "swap_total": int(swap.total / 1024 / 1024),
        "swap_used": int(swap.used / 1024 / 1024),
    }


def get_disk_info(path: str = "/var/lib/cpustack") -> dict:
    """获取磁盘信息。

    Returns:
        disk_total, disk_available (单位 GB)
    """
    try:
        usage = psutil.disk_usage(path)
        return {
            "disk_total": int(usage.total / 1024 / 1024 / 1024),  # bytes -> GB
            "disk_available": int(usage.free / 1024 / 1024 / 1024),
        }
    except Exception:
        logger.debug("磁盘信息获取失败，路径: %s", path)
        return {"disk_total": 0, "disk_available": 0}


def get_os_info() -> dict:
    """获取操作系统信息。"""
    import platform as plat

    return {
        "os": f"{plat.system()} {plat.release()}",
        "kernel": plat.version(),
    }
