"""状态采集器：聚合 CPU/内存/指令集/磁盘检测为 WorkerStatus 数据。"""

from __future__ import annotations

import json
import logging

from cpustack.config import settings
from cpustack.detector.cpu_detector import get_cpu_info, get_cpu_utilization
from cpustack.detector.instruction_set import detect_instruction_sets
from cpustack.detector.memory_detector import get_disk_info, get_memory_info, get_os_info

logger = logging.getLogger(__name__)


def collect_worker_status() -> dict:
    """采集当前节点的完整资源状态。

    Returns:
        可直接用于更新 WorkerStatus 的字典
    """
    cpu_info = get_cpu_info()
    mem_info = get_memory_info()
    disk_info = get_disk_info(settings.model_cache_dir)
    os_info = get_os_info()
    instruction_sets = detect_instruction_sets()

    status = {
        "cpu_arch": cpu_info["cpu_arch"],
        "cpu_model": cpu_info["cpu_model"],
        "cpu_cores": cpu_info["cpu_cores"],
        "cpu_allocated": 0,  # 由 Server 侧聚合计算
        "cpu_utilization": get_cpu_utilization(),
        "instruction_sets": json.dumps(instruction_sets),
        "numa_nodes": cpu_info["numa_nodes"],
        "memory_total": mem_info["memory_total"],
        "memory_allocated": 0,  # 由 Server 侧聚合计算
        "memory_available": mem_info["memory_available"],
        "swap_total": mem_info["swap_total"],
        "swap_used": mem_info["swap_used"],
        "disk_total": disk_info["disk_total"],
        "disk_available": disk_info["disk_available"],
        "network_bandwidth": 1000,  # 默认 1Gbps，可由网络检测覆盖
        "os": os_info["os"],
        "kernel": os_info["kernel"],
    }

    logger.debug(
        "采集状态完成: CPU=%s cores=%d mem=%dMB 指令集=%s",
        cpu_info["cpu_model"],
        cpu_info["cpu_cores"],
        mem_info["memory_total"],
        instruction_sets,
    )
    return status
