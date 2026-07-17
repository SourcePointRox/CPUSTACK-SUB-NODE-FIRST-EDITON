"""性能基准测试框架：吞吐/延迟/资源利用率。

支持阶段5测试维度（规划文档 §11.2）：
1. 单机推理性能：吞吐(tok/s)、TTFT(ms)
2. 并发吞吐：多副本/连续批处理下的 QPS
3. 流式延迟：首 token 延迟（TTFT）
4. 资源利用率：CPU/内存（可选 psutil）

用法：
    # 基准测试（非流式）
    python -m cpustack.benchmark --endpoint http://localhost:80/v1 \\
        --model llama-3.2-3b --api-key sk-xxx --concurrency 4 --duration 60

    # 流式 TTFT 测试
    python -m cpustack.benchmark --endpoint http://localhost:80/v1 \\
        --model llama-3.2-3b --api-key sk-xxx --stream --duration 30

    # 输出 JSON 报告
    python -m cpustack.benchmark ... --report report.json

设计原则：
- 仅依赖 httpx（与项目一致），psutil 可选
- 支持流式与非流式，流式测量 TTFT
- 并发模型用 asyncio.Semaphore 控制并发数
- 报告可序列化为 JSON，便于对比与归档
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 默认测试 prompt（规划文档 §11.1 测试模型选取）
_DEFAULT_PROMPT = "请用中文详细介绍 CPU 分布式推理的几种模式及其适用场景。"
_DEFAULT_MAX_TOKENS = 256


@dataclass
class RequestResult:
    """单次请求结果。"""

    success: bool
    latency_ms: float = 0.0  # 总延迟
    ttft_ms: float = 0.0  # Time To First Token（流式）
    tokens_generated: int = 0  # 生成 token 数
    prompt_tokens: int = 0  # 输入 token 数
    error: str = ""


@dataclass
class BenchmarkReport:
    """基准测试报告。"""

    model: str
    endpoint: str
    mode: str  # "non_stream" | "stream"
    concurrency: int
    duration_seconds: float
    max_tokens: int
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    throughput_tok_per_s: float = 0.0  # 生成 token 吞吐
    request_qps: float = 0.0  # 请求 QPS
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    avg_ttft_ms: float = 0.0  # 平均首 token 延迟（流式）
    p95_ttft_ms: float = 0.0
    total_tokens_generated: int = 0
    total_tokens_prompt: int = 0
    error_breakdown: dict[str, int] = field(default_factory=dict)
    resource_snapshot: dict[str, Any] = field(default_factory=dict)


def _percentile(values: list[float], p: float) -> float:
    """计算分位数（p ∈ [0, 100]）。"""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


async def _send_non_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
) -> RequestResult:
    """发送非流式请求。"""
    start = time.perf_counter()
    try:
        resp = await client.post(
            f"{endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=120,
        )
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code != 200:
            return RequestResult(
                success=False,
                latency_ms=latency,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        usage = data.get("usage", {})
        return RequestResult(
            success=True,
            latency_ms=latency,
            tokens_generated=usage.get("completion_tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return RequestResult(
            success=False,
            latency_ms=latency,
            error=f"{type(e).__name__}: {e}",
        )


async def _send_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
) -> RequestResult:
    """发送流式请求，测量 TTFT。"""
    start = time.perf_counter()
    ttft: float | None = None
    tokens = 0
    try:
        async with client.stream(
            "POST",
            f"{endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "stream": True,
            },
            timeout=120,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                latency = (time.perf_counter() - start) * 1000
                return RequestResult(
                    success=False,
                    latency_ms=latency,
                    error=f"HTTP {resp.status_code}: {body[:200]!r}",
                )
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                if ttft is None:
                    ttft = (time.perf_counter() - start) * 1000
                try:
                    chunk = json.loads(payload)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    if delta.get("content"):
                        tokens += 1
                except json.JSONDecodeError:
                    continue
        latency = (time.perf_counter() - start) * 1000
        return RequestResult(
            success=True,
            latency_ms=latency,
            ttft_ms=ttft or 0.0,
            tokens_generated=tokens,
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return RequestResult(
            success=False,
            latency_ms=latency,
            ttft_ms=ttft or 0.0,
            error=f"{type(e).__name__}: {e}",
        )


async def _worker(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
    deadline: float,
    results: list[RequestResult],
) -> None:
    """并发工作协程：持续发送请求直到截止时间。"""
    sender = _send_stream if stream else _send_non_stream
    while time.perf_counter() < deadline:
        async with sem:
            r = await sender(client, endpoint, model, api_key, prompt, max_tokens)
            results.append(r)


def _snapshot_resources() -> dict[str, Any]:
    """采集本机资源快照（psutil 可选）。"""
    snapshot: dict[str, Any] = {}
    try:
        import psutil

        snapshot["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        snapshot["memory_total_mb"] = mem.total // (1024 * 1024)
        snapshot["memory_available_mb"] = mem.available // (1024 * 1024)
        snapshot["memory_percent"] = mem.percent
        snapshot["cpu_cores"] = psutil.cpu_count(logical=True)
    except ImportError:
        snapshot["note"] = "psutil 未安装，跳过资源采集"
    except Exception as e:
        snapshot["error"] = f"{type(e).__name__}: {e}"
    return snapshot


async def run_benchmark(
    endpoint: str,
    model: str,
    api_key: str,
    concurrency: int = 1,
    duration: int = 60,
    prompt: str = _DEFAULT_PROMPT,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    stream: bool = False,
) -> BenchmarkReport:
    """执行基准测试。

    Args:
        endpoint: OpenAI 兼容 API 根地址（如 http://host/v1）
        model: 模型名称
        api_key: API Key
        concurrency: 并发数
        duration: 持续秒数
        prompt: 测试 prompt
        max_tokens: 单请求最大生成 token
        stream: 是否流式（测量 TTFT）

    Returns:
        BenchmarkReport
    """
    mode = "stream" if stream else "non_stream"
    report = BenchmarkReport(
        model=model,
        endpoint=endpoint,
        mode=mode,
        concurrency=concurrency,
        duration_seconds=duration,
        max_tokens=max_tokens,
    )

    sem = asyncio.Semaphore(concurrency)
    results: list[RequestResult] = []
    deadline = time.perf_counter() + duration

    # 资源监控快照（开始）
    resource_samples: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        tasks = [
            asyncio.create_task(
                _worker(
                    sem, client, endpoint, model, api_key,
                    prompt, max_tokens, stream, deadline, results,
                )
            )
            for _ in range(concurrency)
        ]

        # 周期性采样资源（若 psutil 可用）
        while time.perf_counter() < deadline:
            resource_samples.append(_snapshot_resources())
            await asyncio.sleep(min(5, max(1, duration // 10)))

        await asyncio.gather(*tasks, return_exceptions=True)

    # 统计
    report.total_requests = len(results)
    report.successful_requests = sum(1 for r in results if r.success)
    report.failed_requests = report.total_requests - report.successful_requests

    success_results = [r for r in results if r.success]
    latencies = [r.latency_ms for r in success_results]
    ttfts = [r.ttft_ms for r in success_results if r.ttft_ms > 0]
    total_tokens = sum(r.tokens_generated for r in success_results)
    total_prompt = sum(r.prompt_tokens for r in success_results)

    report.total_tokens_generated = total_tokens
    report.total_tokens_prompt = total_prompt

    if latencies:
        report.avg_latency_ms = statistics.mean(latencies)
        report.p50_latency_ms = _percentile(latencies, 50)
        report.p95_latency_ms = _percentile(latencies, 95)
        report.p99_latency_ms = _percentile(latencies, 99)

    if ttfts:
        report.avg_ttft_ms = statistics.mean(ttfts)
        report.p95_ttft_ms = _percentile(ttfts, 95)

    # 吞吐：生成 token / 总耗时
    if duration > 0:
        report.throughput_tok_per_s = total_tokens / duration
        report.request_qps = report.successful_requests / duration

    # 错误分类
    for r in results:
        if not r.success:
            key = r.error.split(":")[0][:60] if r.error else "unknown"
            report.error_breakdown[key] = report.error_breakdown.get(key, 0) + 1

    # 资源快照汇总
    if resource_samples:
        cpu_vals = [s.get("cpu_percent") for s in resource_samples if "cpu_percent" in s]
        if cpu_vals:
            report.resource_snapshot["cpu_avg_percent"] = statistics.mean(cpu_vals)
            report.resource_snapshot["cpu_max_percent"] = max(cpu_vals)
        last = resource_samples[-1]
        for k in ("memory_total_mb", "memory_available_mb", "memory_percent", "cpu_cores"):
            if k in last:
                report.resource_snapshot[k] = last[k]
        report.resource_snapshot["samples"] = len(resource_samples)

    return report


def main() -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        description="CPUSTACK 性能基准测试框架（阶段5）",
    )
    parser.add_argument("--endpoint", required=True, help="OpenAI 兼容 API 根地址（如 http://host/v1）")
    parser.add_argument("--model", required=True, help="模型名称")
    parser.add_argument("--api-key", default="", help="API Key")
    parser.add_argument("--concurrency", type=int, default=1, help="并发数")
    parser.add_argument("--duration", type=int, default=60, help="持续秒数")
    parser.add_argument("--max-tokens", type=int, default=_DEFAULT_MAX_TOKENS, help="单请求最大生成 token")
    parser.add_argument("--prompt", default=_DEFAULT_PROMPT, help="测试 prompt")
    parser.add_argument("--stream", action="store_true", help="流式模式（测量 TTFT）")
    parser.add_argument("--report", default="", help="报告输出 JSON 文件路径")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    logger.info(
        "开始基准测试: model=%s concurrency=%d duration=%ds stream=%s",
        args.model, args.concurrency, args.duration, args.stream,
    )

    report = asyncio.run(
        run_benchmark(
            endpoint=args.endpoint,
            model=args.model,
            api_key=args.api_key,
            concurrency=args.concurrency,
            duration=args.duration,
            prompt=args.prompt,
            max_tokens=args.max_tokens,
            stream=args.stream,
        )
    )

    # 控制台摘要
    print("\n" + "=" * 60)
    print(f"基准测试报告: {report.model} ({report.mode})")
    print("=" * 60)
    print(f"并发数:           {report.concurrency}")
    print(f"持续时长:         {report.duration_seconds}s")
    print(f"总请求数:         {report.total_requests}")
    print(f"成功请求:         {report.successful_requests}")
    print(f"失败请求:         {report.failed_requests}")
    print(f"吞吐量:           {report.throughput_tok_per_s:.2f} tok/s")
    print(f"请求 QPS:         {report.request_qps:.2f}")
    print(f"平均延迟:         {report.avg_latency_ms:.2f} ms")
    print(f"P50 延迟:         {report.p50_latency_ms:.2f} ms")
    print(f"P95 延迟:         {report.p95_latency_ms:.2f} ms")
    print(f"P99 延迟:         {report.p99_latency_ms:.2f} ms")
    if report.mode == "stream":
        print(f"平均 TTFT:        {report.avg_ttft_ms:.2f} ms")
        print(f"P95 TTFT:         {report.p95_ttft_ms:.2f} ms")
    print(f"总生成 token:     {report.total_tokens_generated}")
    if report.resource_snapshot:
        print(f"资源快照:         {report.resource_snapshot}")
    if report.error_breakdown:
        print(f"错误分类:         {report.error_breakdown}")
    print("=" * 60)

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, ensure_ascii=False, indent=2)
        logger.info("报告已写入: %s", args.report)


if __name__ == "__main__":
    main()
