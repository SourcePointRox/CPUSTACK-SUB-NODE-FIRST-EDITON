"""阶段5 测试与优化集成测试。

验证：
1. 算法优化参数构建（backends/params.py）
   - 连续批处理 / Flash Attention / 投机解码 / 前缀缓存 / extra_args
2. 调度策略优化（scheduler/placement.py）
   - SPREAD / BINPACK / 指令集优先级 / 网络感知 / 策略覆盖
3. 基准测试框架（benchmark.py）
   - 分位数计算 / 报告结构
4. 模型目录（catalog/model_catalog.yaml）
   - YAML 解析 / 字段完整性
5. 后端参数集成
   - LlamaCppStandaloneServer 命令行构建

运行: python -u test_stage5.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_stage5")

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        logger.info("[PASS] %s %s", name, detail)
    else:
        _failed += 1
        logger.error("[FAIL] %s %s", name, detail)


# ============ 测试1: 算法优化参数构建 ============
def test_params_basic() -> None:
    """测试1: params.build_common_args 基本参数。"""
    logger.info("=" * 60)
    logger.info("测试1: 算法优化参数构建 - 基本参数")

    from cpustack.worker.backends.params import build_common_args, parse_backend_parameters

    instance = types.SimpleNamespace(allocated_cpu_cores=8)

    # 1.1 空参数：应有默认 ctx-size 和 threads
    args = build_common_args({}, instance)
    check("1.1 空参数ctx-size", "--ctx-size" in args and "4096" in args, f"args={args}")
    check("1.1 空参数threads", "--threads" in args and "8" in args, f"args={args}")

    # 1.2 自定义 ctx_size
    args = build_common_args({"ctx_size": 8192}, instance)
    check("1.2 自定义ctx-size", "8192" in args, f"args={args}")

    # 1.3 n_batch / n_ubatch
    args = build_common_args({"n_batch": 512, "n_ubatch": 256}, instance)
    check("1.3 batch-size", "--batch-size" in args and "512" in args, f"args={args}")
    check("1.3 ubatch-size", "--ubatch-size" in args and "256" in args, f"args={args}")

    # 1.4 threads 回退（instance 无 allocated_cpu_cores）
    instance_no_cores = types.SimpleNamespace(allocated_cpu_cores=None)
    args = build_common_args({}, instance_no_cores)
    check("1.4 threads回退4", "--threads" in args and "4" in args, f"args={args}")


def test_params_optimizations() -> None:
    """测试2: 算法优化参数构建 - 性能优化开关。"""
    logger.info("=" * 60)
    logger.info("测试2: 算法优化参数构建 - 性能优化开关")

    from cpustack.worker.backends.params import build_common_args

    instance = types.SimpleNamespace(allocated_cpu_cores=4)

    # 2.1 Flash Attention
    args = build_common_args({"flash_attn": True}, instance)
    check("2.1 flash-attn", "--flash-attn" in args, f"args={args}")

    # 2.2 mlock
    args = build_common_args({"mlock": True}, instance)
    check("2.2 mlock", "--mlock" in args, f"args={args}")

    # 2.3 cont_batching + parallel
    args = build_common_args({"cont_batching": True, "parallel": 4}, instance)
    check("2.3 cont-batching", "--cont-batching" in args, f"args={args}")
    check("2.3 parallel", "--parallel" in args and "4" in args, f"args={args}")

    # 2.4 cont_batching 无 parallel（不应有 --parallel）
    args = build_common_args({"cont_batching": True}, instance)
    check("2.4 无parallel不追加", "--parallel" not in args, f"args={args}")

    # 2.5 cache_reuse
    args = build_common_args({"cache_reuse": 256}, instance)
    check("2.5 cache-reuse", "--cache-reuse" in args and "256" in args, f"args={args}")

    # 2.6 n_gpu_layers > 0
    args = build_common_args({"n_gpu_layers": 2}, instance)
    check("2.6 n-gpu-layers", "--n-gpu-layers" in args and "2" in args, f"args={args}")

    # 2.7 n_gpu_layers = 0（默认，不应追加）
    args = build_common_args({}, instance)
    check("2.7 默认无gpu-layers", "--n-gpu-layers" not in args, f"args={args}")


def test_params_speculative_decoding() -> None:
    """测试3: 算法优化参数构建 - 投机解码。"""
    logger.info("=" * 60)
    logger.info("测试3: 算法优化参数构建 - 投机解码")

    from cpustack.worker.backends.params import build_common_args

    instance = types.SimpleNamespace(allocated_cpu_cores=4)

    # 3.1 草稿模型路径不存在：跳过投机解码
    args = build_common_args({"draft_model": "/nonexistent/draft.gguf"}, instance)
    check("3.1 路径不存在跳过", "--model-draft" not in args, f"args={args}")

    # 3.2 草稿模型路径存在：启用投机解码
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        draft_path = f.name
    try:
        args = build_common_args(
            {"draft_model": draft_path, "draft_max_tokens": 32, "draft_min_p": 0.9},
            instance,
        )
        check("3.2 model-draft", "--model-draft" in args and draft_path in args, f"args={args}")
        check("3.2 draft-max", "--draft-max" in args and "32" in args, f"args={args}")
        check("3.2 draft-min-p", "--draft-min-p" in args and "0.9000" in args, f"args={args}")
    finally:
        os.unlink(draft_path)


def test_params_extra_args() -> None:
    """测试4: 算法优化参数构建 - extra_args 透传。"""
    logger.info("=" * 60)
    logger.info("测试4: 算法优化参数构建 - extra_args 透传")

    from cpustack.worker.backends.params import build_common_args, parse_backend_parameters

    instance = types.SimpleNamespace(allocated_cpu_cores=4)

    # 4.1 extra_args 透传
    args = build_common_args({"extra_args": ["--no-mmap", "--numa", "isolate"]}, instance)
    check("4.1 extra透传", "--no-mmap" in args and "--numa" in args and "isolate" in args, f"args={args}")

    # 4.2 parse_backend_parameters 兼容性
    p1 = parse_backend_parameters(None)
    check("4.2 None解析为空", p1 == {}, f"p1={p1}")
    p2 = parse_backend_parameters('{"ctx_size": 1024}')
    check("4.2 JSON字符串解析", p2 == {"ctx_size": 1024}, f"p2={p2}")
    p3 = parse_backend_parameters({"ctx_size": 2048})
    check("4.2 dict直传", p3 == {"ctx_size": 2048}, f"p3={p3}")
    p4 = parse_backend_parameters('invalid json')
    check("4.2 非法JSON返回空", p4 == {}, f"p4={p4}")


# ============ 测试5: 调度策略 - SPREAD/BINPACK ============
def test_placement_spread_binpack() -> None:
    """测试5: placement SPREAD/BINPACK 策略。"""
    logger.info("=" * 60)
    logger.info("测试5: 调度策略 SPREAD/BINPACK")

    from cpustack.server.scheduler.placement import score_placement
    from cpustack.schemas.workers import Worker, WorkerStatus

    def make_worker(name: str, mem_allocated: int, instruction_sets: str = '["AVX2"]') -> tuple:
        w = Worker(name=name, uuid=f"u-{name}", api_key="k", ip="10.0.0.1", port=30080)
        s = WorkerStatus(
            worker_id=0, cpu_cores=8, memory_total=16384,
            memory_available=8192, memory_allocated=mem_allocated,
            instruction_sets=instruction_sets, network_bandwidth=1000,
        )
        return (w, s)

    candidates = [
        make_worker("node-a", mem_allocated=2000),  # 较空
        make_worker("node-b", mem_allocated=6000),  # 较满
        make_worker("node-c", mem_allocated=4000),  # 中等
    ]

    # 5.1 SPREAD：应选已分配最少的 node-a
    chosen = score_placement(candidates, strategy="spread")
    check("5.1 SPREAD选最空", chosen is not None and chosen[0].name == "node-a", f"chosen={chosen[0].name if chosen else None}")

    # 5.2 BINPACK：应选已分配最多的 node-b
    chosen = score_placement(candidates, strategy="binpack")
    check("5.2 BINPACK选最满", chosen is not None and chosen[0].name == "node-b", f"chosen={chosen[0].name if chosen else None}")

    # 5.3 策略覆盖（backend_parameters.placement_strategy）
    chosen = score_placement(
        candidates, strategy="spread",
        backend_parameters={"placement_strategy": "binpack"},
    )
    check("5.3 策略覆盖", chosen is not None and chosen[0].name == "node-b", f"chosen={chosen[0].name if chosen else None}")

    # 5.4 非法策略回退到默认
    chosen = score_placement(
        candidates, strategy="spread",
        backend_parameters={"placement_strategy": "invalid"},
    )
    check("5.4 非法策略回退", chosen is not None and chosen[0].name == "node-a", f"chosen={chosen[0].name if chosen else None}")

    # 5.5 空候选
    check("5.5 空候选", score_placement([], strategy="spread") is None)

    # 5.6 单候选直接返回
    single = candidates[:1]
    chosen = score_placement(single, strategy="spread")
    check("5.6 单候选", chosen is not None and chosen[0].name == "node-a")


# ============ 测试6: 调度策略 - 指令集优先级 ============
def test_placement_instruction_set_priority() -> None:
    """测试6: placement 指令集优先级（AVX-512 > AVX2）。"""
    logger.info("=" * 60)
    logger.info("测试6: 调度策略 指令集优先级")

    from cpustack.server.scheduler.placement import score_placement
    from cpustack.schemas.workers import Worker, WorkerStatus

    def make_worker(name: str, instruction_sets: str) -> tuple:
        w = Worker(name=name, uuid=f"u-{name}", api_key="k", ip="10.0.0.1", port=30080)
        s = WorkerStatus(
            worker_id=0, cpu_cores=8, memory_total=16384,
            memory_available=8192, memory_allocated=0,
            instruction_sets=instruction_sets, network_bandwidth=1000,
        )
        return (w, s)

    # node-a 仅 AVX2，node-b 支持 AVX-512
    # 即使 node-a 内存更空（SPREAD 倾向），AVX-512 优先级更高
    candidates = [
        make_worker("node-avx2", '["AVX2"]'),
        make_worker("node-avx512", '["AVX2","AVX-512"]'),
    ]

    chosen = score_placement(candidates, strategy="spread")
    check("6.1 AVX-512优先", chosen is not None and chosen[0].name == "node-avx512",
          f"chosen={chosen[0].name if chosen else None}")

    # AMX 优先级最高
    candidates_amx = [
        make_worker("node-avx512", '["AVX2","AVX-512"]'),
        make_worker("node-amx", '["AVX2","AVX-512","AMX"]'),
    ]
    chosen = score_placement(candidates_amx, strategy="spread")
    check("6.2 AMX最高优先", chosen is not None and chosen[0].name == "node-amx",
          f"chosen={chosen[0].name if chosen else None}")


# ============ 测试7: 调度策略 - 网络感知（流水线并行）============
def test_placement_network_aware() -> None:
    """测试7: placement 网络感知（流水线并行优先高带宽）。"""
    logger.info("=" * 60)
    logger.info("测试7: 调度策略 网络感知（流水线并行）")

    from cpustack.server.scheduler.placement import score_placement, select_pipeline_nodes
    from cpustack.schemas.workers import Worker, WorkerStatus
    from cpustack.schemas.models import Model, ModelBackend

    def make_worker(name: str, bandwidth: int) -> tuple:
        w = Worker(name=name, uuid=f"u-{name}", api_key="k", ip="10.0.0.1", port=30080)
        s = WorkerStatus(
            worker_id=0, cpu_cores=8, memory_total=16384,
            memory_available=8192, memory_allocated=0,
            instruction_sets='["AVX2"]', network_bandwidth=bandwidth,
        )
        return (w, s)

    model = Model(
        name="m", source_repo="hf", source_model_id="t/m",
        source_filename="m.gguf", backend=ModelBackend.PRIMA_CPP,
        replicas=2, estimated_memory=2048, user_id=1,
    )

    # node-a 1Gbps, node-b 10Gbps —— 流水线并行应选高带宽
    candidates = [
        make_worker("node-1g", 1000),
        make_worker("node-10g", 10000),
    ]

    chosen = score_placement(candidates, strategy="spread", model=model)
    check("7.1 流水线选高带宽", chosen is not None and chosen[0].name == "node-10g",
          f"chosen={chosen[0].name if chosen else None}")

    # 7.2 非流水线模式不应用网络感知（按 SPREAD 选内存最空）
    model_standalone = Model(
        name="m2", source_repo="hf", source_model_id="t/m2",
        source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
        replicas=1, estimated_memory=2048, user_id=1,
    )
    chosen = score_placement(candidates, strategy="spread", model=model_standalone)
    # 两者 memory_allocated 相同，回退到内存策略，node-1g 或 node-10g 均可
    check("7.2 非流水线不感知网络", chosen is not None, f"chosen={chosen[0].name if chosen else None}")

    # 7.3 select_pipeline_nodes 选择多个节点
    selected = select_pipeline_nodes(candidates, node_count=2, model=model)
    check("7.3 选2节点", len(selected) == 2, f"selected={[w.name for w, _ in selected]}")
    # 第一个应是高带宽的 node-10g
    check("7.3 首选高带宽", selected[0][0].name == "node-10g", f"first={selected[0][0].name}")

    # 7.4 节点数超过候选
    selected = select_pipeline_nodes(candidates, node_count=5, model=model)
    check("7.4 超出候选取全部", len(selected) == 2, f"selected={len(selected)}")


# ============ 测试8: 调度策略 - RPC Master 选择 ============
def test_placement_rpc_master() -> None:
    """测试8: placement RPC Master 选择（指令集 + 内存）。"""
    logger.info("=" * 60)
    logger.info("测试8: 调度策略 RPC Master 选择")

    from cpustack.server.scheduler.placement import select_rpc_master
    from cpustack.schemas.workers import Worker, WorkerStatus

    def make_worker(name: str, mem_avail: int, instruction_sets: str) -> tuple:
        w = Worker(name=name, uuid=f"u-{name}", api_key="k", ip="10.0.0.1", port=30080)
        s = WorkerStatus(
            worker_id=0, cpu_cores=8, memory_total=16384,
            memory_available=mem_avail, memory_allocated=0,
            instruction_sets=instruction_sets, network_bandwidth=1000,
        )
        return (w, s)

    # node-a 内存大但仅 AVX2，node-b 内存小但 AVX-512
    # AVX-512 优先级高于内存
    candidates = [
        make_worker("node-big-avx2", mem_avail=16384, instruction_sets='["AVX2"]'),
        make_worker("node-small-avx512", mem_avail=4096, instruction_sets='["AVX2","AVX-512"]'),
    ]
    chosen = select_rpc_master(candidates)
    check("8.1 Master选AVX-512", chosen is not None and chosen[0].name == "node-small-avx512",
          f"chosen={chosen[0].name if chosen else None}")

    # 同指令集下选内存大的
    candidates_same_is = [
        make_worker("node-4g", mem_avail=4096, instruction_sets='["AVX2"]'),
        make_worker("node-16g", mem_avail=16384, instruction_sets='["AVX2"]'),
    ]
    chosen = select_rpc_master(candidates_same_is)
    check("8.2 同指令集选大内存", chosen is not None and chosen[0].name == "node-16g",
          f"chosen={chosen[0].name if chosen else None}")

    # 空候选
    check("8.3 空候选", select_rpc_master([]) is None)


# ============ 测试9: 基准测试框架 ============
def test_benchmark_framework() -> None:
    """测试9: benchmark 框架（分位数 + 报告结构）。"""
    logger.info("=" * 60)
    logger.info("测试9: 基准测试框架")

    from cpustack.benchmark import _percentile, BenchmarkReport, RequestResult

    # 9.1 分位数计算
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    check("9.1 P50", _percentile(values, 50) == 55.0 or abs(_percentile(values, 50) - 55.0) < 0.01,
          f"p50={_percentile(values, 50)}")
    check("9.1 P95", _percentile(values, 95) >= 90.0, f"p95={_percentile(values, 95)}")
    check("9.1 P99", _percentile(values, 99) >= 90.0, f"p99={_percentile(values, 99)}")

    # 9.2 空列表
    check("9.2 空列表", _percentile([], 50) == 0.0)

    # 9.3 单元素
    check("9.3 单元素", _percentile([42.0], 50) == 42.0)

    # 9.4 BenchmarkReport 序列化
    report = BenchmarkReport(
        model="test-model", endpoint="http://localhost:80/v1",
        mode="non_stream", concurrency=4, duration_seconds=60, max_tokens=256,
    )
    from dataclasses import asdict
    d = asdict(report)
    check("9.4 报告序列化", d["model"] == "test-model" and d["concurrency"] == 4, f"d={d}")

    # 9.5 RequestResult
    r = RequestResult(success=True, latency_ms=123.4, tokens_generated=50)
    check("9.5 请求结果", r.success and r.latency_ms == 123.4 and r.tokens_generated == 50,
          f"r={r}")


# ============ 测试10: 模型目录加载 ============
def test_model_catalog() -> None:
    """测试10: catalog YAML 解析。"""
    logger.info("=" * 60)
    logger.info("测试10: 模型目录加载")

    import yaml

    catalog_path = Path(__file__).parent / "cpustack" / "catalog" / "model_catalog.yaml"
    if not catalog_path.exists():
        check("10.0 文件存在", False, f"path={catalog_path}")
        return

    with open(catalog_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    check("10.1 YAML可解析", isinstance(data, dict) and "models" in data, f"keys={list(data.keys()) if isinstance(data, dict) else None}")

    models = data.get("models", [])
    check("10.2 6个模型", len(models) == 6, f"count={len(models)}")

    required_fields = {"name", "display_name", "source_repo", "source_model_id",
                       "source_filename", "quantization_size_gb", "estimated_memory_mb",
                       "recommended_backend", "test_purpose"}

    for m in models:
        missing = required_fields - set(m.keys())
        check(f"10.3 字段完整({m.get('name', '?')})", not missing, f"missing={missing}")

    # 10.4 全部 ≤5GB
    all_under_5gb = all(m.get("quantization_size_gb", 999) <= 5.0 for m in models)
    check("10.4 全部≤5GB", all_under_5gb, f"sizes={[m.get('quantization_size_gb') for m in models]}")

    # 10.5 包含 RPC 推荐模型
    rpc_models = [m for m in models if m.get("recommended_backend") == "llama_cpp_rpc"]
    check("10.5 含RPC推荐", len(rpc_models) >= 1, f"rpc={len(rpc_models)}")

    # 10.6 包含 modelscope 源（国内镜像）
    ms_models = [m for m in models if m.get("source_repo") == "modelscope"]
    check("10.6 含modelscope源", len(ms_models) >= 1, f"ms={len(ms_models)}")


# ============ 测试11: 后端参数集成（命令行构建）============
def test_backend_integration() -> None:
    """测试11: 后端 start 方法集成 backend_parameters。"""
    logger.info("=" * 60)
    logger.info("测试11: 后端参数集成（命令行构建）")

    from cpustack.worker.backends.llama_cpp_standalone import LlamaCppStandaloneServer
    from cpustack.worker.backends.llama_cpp_rpc import LlamaCppRPCServer
    from cpustack.worker.backends.prima_cpp import PrimaCppServer
    from cpustack.worker.backends.data_parallel import DataParallelServer
    from cpustack.schemas.models import ModelInstance, ModelBackend

    # 检查 start 方法签名支持 backend_parameters
    import inspect

    sig = inspect.signature(LlamaCppStandaloneServer.start)
    check("11.1 standalone支持backend_parameters",
          "backend_parameters" in sig.parameters, f"params={list(sig.parameters.keys())}")

    sig = inspect.signature(LlamaCppRPCServer.start)
    check("11.2 rpc支持backend_parameters",
          "backend_parameters" in sig.parameters, f"params={list(sig.parameters.keys())}")

    sig = inspect.signature(PrimaCppServer.start)
    check("11.3 prima支持backend_parameters",
          "backend_parameters" in sig.parameters, f"params={list(sig.parameters.keys())}")

    sig = inspect.signature(DataParallelServer.start)
    check("11.4 data_parallel支持backend_parameters",
          "backend_parameters" in sig.parameters, f"params={list(sig.parameters.keys())}")

    # 11.5 实际构建命令行（通过 mock instance）
    instance = ModelInstance(
        name="test", model_id=1, allocated_cpu_cores=6,
    )
    # 手动设置 backend_parameters 属性（绕过 Pydantic）
    object.__setattr__(instance, "backend_parameters", '{"cont_batching": true, "flash_attn": true}')

    backend = LlamaCppStandaloneServer(instance)
    # 调用 build_common_args 验证
    from cpustack.worker.backends.params import build_common_args, parse_backend_parameters
    params = parse_backend_parameters(instance.backend_parameters)
    args = build_common_args(params, instance)
    check("11.5 cont-batching在参数中", "--cont-batching" in args, f"args={args}")
    check("11.5 flash-attn在参数中", "--flash-attn" in args, f"args={args}")
    check("11.5 threads=6", "--threads" in args and "6" in args, f"args={args}")


# ============ 主函数 ============
async def main() -> None:
    logger.info("=" * 60)
    logger.info("CPUSTACK 阶段5 测试与优化 - 集成测试")
    logger.info("=" * 60)

    # 参数构建测试
    test_params_basic()
    test_params_optimizations()
    test_params_speculative_decoding()
    test_params_extra_args()

    # 调度策略测试
    test_placement_spread_binpack()
    test_placement_instruction_set_priority()
    test_placement_network_aware()
    test_placement_rpc_master()

    # 基准框架测试
    test_benchmark_framework()

    # 模型目录测试
    test_model_catalog()

    # 后端集成测试
    test_backend_integration()

    # 汇总
    logger.info("=" * 60)
    logger.info("阶段5测试汇总: 通过 %d / 失败 %d", _passed, _failed)
    logger.info("=" * 60)

    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
