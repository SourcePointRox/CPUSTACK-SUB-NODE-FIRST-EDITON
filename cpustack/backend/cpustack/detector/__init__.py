"""硬件检测包：CPU 指令集、内存、NUMA 拓扑检测。

CPUSTACK 特有模块（区别于 GPUStack 的 GPU 检测）：
- 指令集支持是 CPU 推理调度的关键依据
- AVX2/AVX-512/AMX 决定推理内核选择
"""

from cpustack.detector.collector import collect_worker_status

__all__ = ["collect_worker_status"]
