"""ServeManager：推理后端进程生命周期管理。

监听 ModelInstance 状态变化 → 下载模型 → 启动/停止推理后端进程。

支持三种模式：
- 单机（standalone）：下载模型 → 启动 llama-server
- RPC Master：下载模型 → 等待 Slave 就绪 → 启动 llama-server --rpc
- RPC Slave：启动 rpc-server → 上报就绪

实例生命周期（Worker 侧）：
  SCHEDULED → INITIALIZING → DOWNLOADING → STARTING → RUNNING
                                                    ↘ ERROR
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from cpustack.config import settings
from cpustack.schemas.models import ModelInstance, ModelInstanceState

logger = logging.getLogger(__name__)


class ServeManager:
    """管理本节点上的推理后端进程。"""

    def __init__(self, worker_manager):
        self._wm = worker_manager
        self._processes: dict[int, asyncio.subprocess.Process] = {}  # instance_id -> llama-server
        self._rpc_processes: dict[int, asyncio.subprocess.Process] = {}  # instance_id -> rpc-server
        self._ports: dict[int, int] = {}  # instance_id -> port
        self._allocated_ports: set[int] = set()
        self._handling: set[int] = set()  # 正在处理的实例 ID（防重复）
        self._watch_task: asyncio.Task | None = None
        # 软文件锁：按 model_id:filename 去重，防止同 Worker 多实例并发下载同一模型
        self._download_locks: dict[str, asyncio.Lock] = {}
        # 实例日志缓冲（每实例最近 500 行）
        self._instance_logs: dict[int, list[str]] = {}
        self._MAX_LOG_LINES = 500

    def _add_log(self, instance_id: int, message: str) -> None:
        """追加实例日志行到内存缓冲。"""
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        logs = self._instance_logs.setdefault(instance_id, [])
        logs.append(line)
        if len(logs) > self._MAX_LOG_LINES:
            del logs[: len(logs) - self._MAX_LOG_LINES]
        logger.info("[实例 %d] %s", instance_id, message)

    def get_instance_logs(self, instance_id: int) -> list[str]:
        """获取实例日志。"""
        return list(self._instance_logs.get(instance_id, []))

    def allocate_port(self) -> int:
        """从服务端口范围分配一个端口。"""
        for port in range(
            settings.service_port_range_start, settings.service_port_range_end
        ):
            if port not in self._allocated_ports:
                self._allocated_ports.add(port)
                return port
        raise RuntimeError("无可用端口")

    def release_port(self, port: int) -> None:
        """释放端口。"""
        self._allocated_ports.discard(port)

    async def _report_state(
        self,
        instance_id: int,
        state: ModelInstanceState,
        error_message: str = "",
        service_port: int | None = None,
        download_progress: float = 0.0,
    ) -> None:
        """向 Server 上报实例状态。"""
        if not self._wm.worker_uuid or not self._wm.api_key:
            return

        payload: dict = {"state": state.value, "download_progress": download_progress}
        if error_message:
            payload["error_message"] = error_message
        if service_port is not None:
            payload["service_port"] = service_port

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._wm.effective_server_url}/v2/worker/instances/{instance_id}/state",
                    json=payload,
                    headers={
                        "X-Worker-UUID": self._wm.worker_uuid,
                        "X-Worker-Key": self._wm.api_key,
                    },
                )
                if resp.status_code != 200:
                    logger.warning(
                        "上报状态失败 %d -> %s: %s",
                        instance_id,
                        state.value,
                        resp.text,
                    )
        except Exception:
            logger.debug("上报状态异常 %d -> %s", instance_id, state.value)

    async def _download_model(self, inst: dict, instance_id: int) -> str | None:
        """下载模型文件（带进程内软锁）。

        软锁策略：按 source_model_id:source_filename 去重，
        同 Worker 上多个实例并发请求同一模型时，仅第一个执行实际下载，
        后续实例在锁内复检缓存命中后直接复用。

        返回模型文件路径，失败返回 None。
        """
        from cpustack.worker.downloader import (
            download_model_file,
            get_cached_model_path,
        )

        # 1. 无锁快速路径：检查 HF 缓存命中
        cached = get_cached_model_path(inst["source_model_id"], inst["source_filename"])
        if cached:
            logger.info("模型文件已缓存: %s", cached)
            return cached

        # 2. 进程内软锁（同模型并发下载去重）
        lock_key = f"{inst['source_model_id']}:{inst['source_filename']}"
        if lock_key not in self._download_locks:
            self._download_locks[lock_key] = asyncio.Lock()

        async with self._download_locks[lock_key]:
            # 双重检查：持有锁后再次检查缓存（可能其他实例刚下载完成）
            cached = get_cached_model_path(
                inst["source_model_id"], inst["source_filename"]
            )
            if cached:
                logger.info("模型文件已缓存（锁内复检）: %s", cached)
                return cached

            # 3. 实际下载
            await self._report_state(instance_id, ModelInstanceState.DOWNLOADING)

            # on_progress 在 executor 线程中调用，必须用 run_coroutine_threadsafe
            # 而非 ensure_future（executor 线程无事件循环）
            loop = asyncio.get_running_loop()

            def on_progress(p: float) -> None:
                asyncio.run_coroutine_threadsafe(
                    self._report_state(
                        instance_id,
                        ModelInstanceState.DOWNLOADING,
                        download_progress=p,
                    ),
                    loop,
                )

            model_path = await download_model_file(
                source_repo=inst["source_repo"],
                source_model_id=inst["source_model_id"],
                source_filename=inst["source_filename"],
                progress_callback=on_progress,
            )
            return model_path  # 失败返回 None

    async def _handle_slave_instance(self, inst: dict) -> None:
        """处理 RPC Slave 实例：启动 rpc-server。"""
        instance_id = inst["id"]

        if instance_id in self._rpc_processes:
            return  # 已在运行

        self._add_log(instance_id, f"启动 RPC Slave: 实例 {inst['name']}")
        logger.info("启动 RPC Slave: 实例 %s", inst["name"])

        try:
            self._add_log(instance_id, "状态变更为 INITIALIZING")
            await self._report_state(instance_id, ModelInstanceState.INITIALIZING)

            # RPC 端口约定：50000 + worker_id
            # worker_id 需要从 WorkerManager 获取
            worker_id = self._wm.worker_id or 0
            rpc_port = 50000 + worker_id
            self._add_log(instance_id, f"RPC Slave 端口: {rpc_port} (worker_id={worker_id})")

            from cpustack.worker.backends.llama_cpp_rpc import start_rpc_server

            self._add_log(instance_id, "正在启动 rpc-server...")
            process = await start_rpc_server(rpc_port)
            if not process:
                self._add_log(instance_id, "rpc-server 启动失败（未找到二进制或启动异常）")
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="rpc-server 启动失败",
                )
                return

            # 等待 rpc-server 启动
            await asyncio.sleep(2)

            # 检查进程是否仍在运行
            if process.returncode is not None:
                stdout = await process.stdout.read() if process.stdout else b""
                stderr = await process.stderr.read() if process.stderr else b""
                err_text = stderr.decode("utf-8", errors="replace")[:500]
                self._add_log(instance_id, f"rpc-server 进程已退出 (code={process.returncode}): {err_text}")
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message=f"rpc-server 启动后立即退出: {err_text}",
                )
                return

            self._rpc_processes[instance_id] = process
            self._add_log(instance_id, f"RPC Slave 就绪 (端口 {rpc_port})")
            await self._report_state(
                instance_id,
                ModelInstanceState.RUNNING,
                service_port=rpc_port,
            )
            logger.info("RPC Slave 就绪: 实例 %s (端口 %d)", inst["name"], rpc_port)

        except Exception:
            logger.exception("RPC Slave 处理异常: %s", inst["name"])
            await self._report_state(
                instance_id,
                ModelInstanceState.ERROR,
                error_message="RPC Slave 处理异常",
            )

    async def _handle_prima_worker_instance(self, inst: dict) -> None:
        """处理流水线 Worker 实例：下载模型 → 启动 prima-server worker。

        流水线并行中每节点需加载完整模型，故 Worker 也要下载模型。
        """
        instance_id = inst["id"]

        if instance_id in self._rpc_processes:
            return  # 复用 _rpc_processes 存储 prima-worker 进程

        logger.info(
            "启动流水线 Worker: 实例 %s (rank=%d, 层 %d-%d)",
            inst["name"], inst.get("rank", -1),
            inst.get("layer_start", -1), inst.get("layer_end", -1),
        )

        try:
            await self._report_state(instance_id, ModelInstanceState.INITIALIZING)

            # 1. 下载完整模型（流水线每节点加载完整模型权重）
            model_path = await self._download_model(inst, instance_id)
            if not model_path:
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="模型文件下载失败",
                )
                return

            # 2. 启动 prima-server worker
            await self._report_state(instance_id, ModelInstanceState.STARTING)

            worker_id = self._wm.worker_id or 0
            prima_port = 50000 + worker_id
            master_addr = f"{inst['pipeline_master_ip']}:{inst['pipeline_master_port']}"

            from cpustack.worker.backends.prima_cpp import start_prima_worker

            process = await start_prima_worker(
                port=prima_port,
                layer_start=inst["layer_start"],
                layer_end=inst["layer_end"],
                rank=inst["rank"],
                master_addr=master_addr,
                model_file_path=model_path,
            )
            if not process:
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="prima-server worker 启动失败",
                )
                return

            await asyncio.sleep(2)

            self._rpc_processes[instance_id] = process  # 复用存储
            await self._report_state(
                instance_id,
                ModelInstanceState.RUNNING,
                service_port=prima_port,
            )
            logger.info(
                "流水线 Worker 就绪: 实例 %s (rank=%d, 端口 %d)",
                inst["name"], inst["rank"], prima_port,
            )

        except Exception:
            logger.exception("流水线 Worker 处理异常: %s", inst["name"])
            await self._report_state(
                instance_id,
                ModelInstanceState.ERROR,
                error_message="流水线 Worker 处理异常",
            )

    async def _handle_prima_master_instance(self, inst: dict) -> None:
        """处理流水线 Master 实例：下载模型 → 等待 Worker → 启动 prima-server master。"""
        instance_id = inst["id"]

        if instance_id in self._handling or instance_id in self._processes:
            return

        self._handling.add(instance_id)
        logger.info(
            "启动流水线 Master: 实例 %s (模型: %s)",
            inst["name"], inst["source_model_id"],
        )

        try:
            # 1. INITIALIZING
            await self._report_state(instance_id, ModelInstanceState.INITIALIZING)

            # 2. 下载模型
            model_path = await self._download_model(inst, instance_id)
            if not model_path:
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="模型文件下载失败",
                )
                return

            # 3. STARTING
            await self._report_state(instance_id, ModelInstanceState.STARTING)

            # 4. 等待 Worker 就绪
            pipeline_workers = inst.get("pipeline_workers", [])
            if pipeline_workers:
                logger.info("等待 %d 个流水线 Worker 就绪...", len(pipeline_workers))
                # 构造探测目标 (ip, port)
                targets = [(w["ip"], w["port"]) for w in pipeline_workers]
                await self._wait_for_slaves(
                    [{"ip": ip, "rpc_port": port} for ip, port in targets],
                    timeout=90,
                )

            # 5. 启动 prima-server master
            port = self.allocate_port()
            from cpustack.worker.backends.prima_cpp import PrimaCppServer
            from cpustack.schemas.models import ModelBackend

            instance_obj = ModelInstance(
                id=instance_id,
                name=inst["name"],
                model_id=inst["model_id"],
                allocated_cpu_cores=inst["allocated_cpu_cores"],
                allocated_memory=inst["allocated_memory"],
            )
            instance_obj.backend = ModelBackend.PRIMA_CPP
            backend = PrimaCppServer(instance_obj)

            process = await backend.start(
                model_path, port,
                pipeline_workers=pipeline_workers,
                backend_parameters=inst.get("backend_parameters") or {},
            )
            if not process:
                self.release_port(port)
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="prima-server master 启动失败",
                )
                return

            # 6. 健康检查
            healthy = await self._wait_for_health(port, timeout=90)
            if not healthy:
                process.terminate()
                self.release_port(port)
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="prima-server master 健康检查超时",
                )
                return

            # 7. RUNNING
            self._processes[instance_id] = process
            self._ports[instance_id] = port
            await self._report_state(
                instance_id,
                ModelInstanceState.RUNNING,
                service_port=port,
            )
            logger.info(
                "流水线 Master 实例 %s 已就绪 (端口 %d, %d Workers)",
                inst["name"], port, len(pipeline_workers),
            )

        except Exception:
            logger.exception("流水线 Master 处理异常: %s", inst["name"])
            await self._report_state(
                instance_id,
                ModelInstanceState.ERROR,
                error_message="流水线 Master 处理异常",
            )
        finally:
            self._handling.discard(instance_id)

    async def _handle_master_instance(self, inst: dict) -> None:
        """处理 RPC Master 实例：下载模型 → 等待 Slave → 启动 llama-server --rpc。"""
        instance_id = inst["id"]

        if instance_id in self._handling or instance_id in self._processes:
            return

        self._handling.add(instance_id)
        self._add_log(instance_id, f"启动 RPC Master: 实例 {inst['name']} (模型: {inst['source_model_id']})")
        logger.info("启动 RPC Master: 实例 %s (模型: %s)", inst["name"], inst["source_model_id"])

        try:
            # 1. 等待 Slave 就绪（保持 SCHEDULED 状态，让 Slave Worker 能处理实例启动 rpc-server）
            # 注意：必须先等待 Slave，否则 Master 改状态后 Slave Worker（旧代码）无法处理非 SCHEDULED 实例
            rpc_slaves = inst.get("rpc_slaves", [])
            ready_slaves = []
            if rpc_slaves:
                self._add_log(instance_id, f"等待 {len(rpc_slaves)} 个 RPC Slave 就绪（保持 SCHEDULED 状态）...")
                ready_slaves = await self._wait_for_slaves(rpc_slaves, timeout=90)
                self._add_log(instance_id, f"RPC Slave 就绪: {len(ready_slaves)}/{len(rpc_slaves)}")

            # 2. INITIALIZING
            self._add_log(instance_id, "状态变更为 INITIALIZING")
            await self._report_state(instance_id, ModelInstanceState.INITIALIZING)

            # 3. 下载模型文件
            from cpustack.worker.downloader import download_model_file, get_cached_model_path

            cached = get_cached_model_path(inst["source_model_id"], inst["source_filename"])
            if cached:
                model_path = cached
                self._add_log(instance_id, f"模型文件已缓存: {model_path}")
            else:
                self._add_log(instance_id, f"开始下载模型: {inst['source_repo']}/{inst['source_model_id']}/{inst['source_filename']}")
                await self._report_state(instance_id, ModelInstanceState.DOWNLOADING)

                # on_progress 在 executor 线程中调用，必须用 run_coroutine_threadsafe
                loop = asyncio.get_running_loop()

                def on_progress(p: float) -> None:
                    asyncio.run_coroutine_threadsafe(
                        self._report_state(
                            instance_id,
                            ModelInstanceState.DOWNLOADING,
                            download_progress=p,
                        ),
                        loop,
                    )

                model_path = await download_model_file(
                    source_repo=inst["source_repo"],
                    source_model_id=inst["source_model_id"],
                    source_filename=inst["source_filename"],
                    progress_callback=on_progress,
                )

                if not model_path:
                    self._add_log(instance_id, "模型文件下载失败")
                    await self._report_state(
                        instance_id,
                        ModelInstanceState.ERROR,
                        error_message="模型文件下载失败",
                    )
                    return
                self._add_log(instance_id, f"模型下载完成: {model_path}")

            # 4. STARTING
            self._add_log(instance_id, "状态变更为 STARTING")
            await self._report_state(instance_id, ModelInstanceState.STARTING)

            # 5. 构建 RPC peers 列表（仅包含已就绪的 Slave）
            rpc_peers = [f"{s['ip']}:{s['rpc_port']}" for s in ready_slaves if s.get("rpc_port")]
            if not rpc_peers and rpc_slaves:
                self._add_log(instance_id, "无 RPC Slave 就绪，尝试单机模式启动")

            # 6. 启动 llama-server（RPC Master 模式）
            instance_obj = ModelInstance(
                id=instance_id,
                name=inst["name"],
                model_id=inst["model_id"],
                allocated_cpu_cores=inst["allocated_cpu_cores"],
                allocated_memory=inst["allocated_memory"],
            )

            port = self.allocate_port()
            self._add_log(instance_id, f"分配推理端口: {port}, RPC peers: {rpc_peers}")
            from cpustack.worker.backends.llama_cpp_rpc import LlamaCppRPCServer

            # ModelInstance 表无 backend 字段（backend 属于 Model 表），
            # 直接实例化 RPC 后端，与 _handle_prima_master_instance 做法一致
            backend = LlamaCppRPCServer(instance_obj)

            # RPC 后端的 start 方法接受 rpc_peers 参数
            self._add_log(instance_id, "正在启动 llama-server (RPC Master 模式)...")
            process = await backend.start(
                model_path, port,
                rpc_peers=rpc_peers,
                backend_parameters=inst.get("backend_parameters") or {},
            )

            if not process:
                self.release_port(port)
                self._add_log(instance_id, "推理后端启动失败")
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="推理后端启动失败",
                )
                return

            # 7. 等待健康检查（大模型加载需要较长时间，超时 600s）
            self._add_log(instance_id, f"等待健康检查 (端口 {port}, 超时 600s)...")
            healthy = await self._wait_for_health(port, timeout=600, process=process)
            if not healthy:
                # 读取 llama-server 输出用于诊断
                stdout = await process.stdout.read() if process.stdout else b""
                stderr = await process.stderr.read() if process.stderr else b""
                err_text = stderr.decode("utf-8", errors="replace")[:1000]
                out_text = stdout.decode("utf-8", errors="replace")[:500]
                if process.returncode is None:
                    process.terminate()
                self.release_port(port)
                diag = f"exit={process.returncode}, stderr={err_text}, stdout={out_text}"
                self._add_log(instance_id, f"推理后端健康检查失败: {diag}")
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message=f"推理后端健康检查失败: {diag}",
                )
                return

            # 8. RUNNING
            self._processes[instance_id] = process
            self._ports[instance_id] = port
            self._add_log(instance_id, f"实例已就绪! 端口 {port}, {len(rpc_peers)} Slaves")
            await self._report_state(
                instance_id,
                ModelInstanceState.RUNNING,
                service_port=port,
            )
            logger.info(
                "RPC Master 实例 %s 已就绪 (端口 %d, %d Slaves)",
                inst["name"], port, len(rpc_peers),
            )

        except Exception as e:
            self._add_log(instance_id, f"RPC Master 处理异常: {e}")
            logger.exception("RPC Master 处理异常: %s", inst["name"])
            await self._report_state(
                instance_id,
                ModelInstanceState.ERROR,
                error_message="RPC Master 处理异常",
            )
        finally:
            self._handling.discard(instance_id)

    async def _wait_for_slaves(self, slaves: list[dict], timeout: int = 60) -> list[dict]:
        """等待 RPC Slave 节点就绪（简单轮询 TCP 连接）。

        返回已就绪的 Slave 列表。
        超时后对未就绪的 Slave 查询诊断端点，记录具体原因。
        """
        import time

        deadline = time.time() + timeout
        all_targets = {f"{s['ip']}:{s['rpc_port']}": s for s in slaves if s.get("rpc_port")}
        pending = set(all_targets.keys())
        ready: list[dict] = []

        while time.time() < deadline and pending:
            newly_ready = []
            for key in list(pending):
                ip, port = key.split(":")
                if await self._check_tcp(ip, int(port)):
                    logger.info("RPC Slave %s 已就绪", key)
                    newly_ready.append(key)
            for key in newly_ready:
                ready.append(all_targets[key])
                pending.discard(key)
            if pending:
                await asyncio.sleep(3)

        if pending:
            logger.warning("部分 RPC Slave 未就绪: %s", list(pending))
            # 查询诊断端点，获取具体原因
            for key in list(pending):
                ip = key.split(":")[0]
                diag = await self._diagnose_slave(ip)
                if diag:
                    slave_info = all_targets.get(key, {})
                    slave_name = slave_info.get("worker_name", ip)
                    rpc_port = diag.get("rpc_port", "?")
                    binaries = diag.get("binaries", {})
                    rpc_binary = binaries.get("rpc-server")
                    port_listening = diag.get("rpc_port_listening", False)
                    firewall = diag.get("firewall_rules", [])

                    if not rpc_binary:
                        self._add_log(
                            0,
                            f"Slave {slave_name}({ip}) 诊断: rpc-server 未安装"
                            f"（rpc_port={rpc_port}, 二进制路径=None）",
                        )
                    elif not port_listening:
                        self._add_log(
                            0,
                            f"Slave {slave_name}({ip}) 诊断: rpc-server 已安装"
                            f"({rpc_binary}) 但端口 {rpc_port} 未监听"
                            f"— 请检查 Slave 日志",
                        )
                    else:
                        fw_ok = any(r.get("rule_exists") for r in firewall)
                        self._add_log(
                            0,
                            f"Slave {slave_name}({ip}) 诊断: rpc-server 运行中"
                            f"(端口 {rpc_port}) 但 Master 无法连接"
                            f"— 防火墙规则={'存在' if fw_ok else '缺失'}",
                        )

        return ready

    async def _diagnose_slave(self, ip: str) -> dict | None:
        """查询 Slave 节点的诊断端点。"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://{ip}:30080/internal/diagnose")
                if resp.status_code == 200:
                    return resp.json()
        except Exception:
            pass
        return None

    async def _check_tcp(self, host: str, port: int) -> bool:
        """检查 TCP 端口是否可连接。"""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _handle_instance(self, inst: dict) -> None:
        """处理单个分配的实例（根据 RPC/流水线角色分支）。"""
        instance_id = inst["id"]
        state = inst["state"]
        rpc_role = inst.get("rpc_role", "")
        pipeline_role = inst.get("pipeline_role", "")

        # 已在处理中或已运行，跳过
        if instance_id in self._handling:
            return
        if instance_id in self._processes or instance_id in self._rpc_processes:
            return

        # 仅处理 SCHEDULED 状态
        # Slave 角色：实例可能已被 Master 改为 INITIALIZING/STARTING，
        # 但 Slave 仍需启动 rpc-server（_handle_slave_instance 内部有重复启动防护）
        if state != "scheduled" and rpc_role != "slave":
            return

        # RPC 角色分支（llama_cpp_rpc）
        if rpc_role == "slave":
            asyncio.ensure_future(self._handle_slave_instance(inst))
            return
        elif rpc_role == "master":
            asyncio.ensure_future(self._handle_master_instance(inst))
            return

        # 流水线并行角色分支（prima_cpp）
        if pipeline_role == "worker":
            asyncio.ensure_future(self._handle_prima_worker_instance(inst))
            return
        elif pipeline_role == "master":
            asyncio.ensure_future(self._handle_prima_master_instance(inst))
            return

        # 单机模式（standalone）& 数据并行副本（每个副本独立处理）
        self._handling.add(instance_id)
        logger.info("开始处理实例 %s (模型: %s)", inst["name"], inst["source_model_id"])

        try:
            # 1. INITIALIZING
            await self._report_state(instance_id, ModelInstanceState.INITIALIZING)

            # 2. 检查/下载模型文件
            from cpustack.worker.downloader import download_model_file, get_cached_model_path

            cached = get_cached_model_path(inst["source_model_id"], inst["source_filename"])

            if cached:
                model_path = cached
                logger.info("模型文件已缓存: %s", model_path)
            else:
                await self._report_state(instance_id, ModelInstanceState.DOWNLOADING)

                def on_progress(p: float) -> None:
                    asyncio.ensure_future(
                        self._report_state(
                            instance_id,
                            ModelInstanceState.DOWNLOADING,
                            download_progress=p,
                        )
                    )

                model_path = await download_model_file(
                    source_repo=inst["source_repo"],
                    source_model_id=inst["source_model_id"],
                    source_filename=inst["source_filename"],
                    progress_callback=on_progress,
                )

                if not model_path:
                    await self._report_state(
                        instance_id,
                        ModelInstanceState.ERROR,
                        error_message="模型文件下载失败",
                    )
                    return

            # 3. STARTING
            await self._report_state(instance_id, ModelInstanceState.STARTING)

            # 4. 启动推理后端
            instance_obj = ModelInstance(
                id=instance_id,
                name=inst["name"],
                model_id=inst["model_id"],
                allocated_cpu_cores=inst["allocated_cpu_cores"],
                allocated_memory=inst["allocated_memory"],
            )

            port = self.allocate_port()
            from cpustack.worker.backends.base import get_backend

            backend = get_backend(instance_obj)
            process = await backend.start(
                model_path, port,
                backend_parameters=inst.get("backend_parameters") or {},
            )

            if not process:
                self.release_port(port)
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="推理后端启动失败",
                )
                return

            # 5. 等待健康检查
            healthy = await self._wait_for_health(port, timeout=60)
            if not healthy:
                process.terminate()
                self.release_port(port)
                await self._report_state(
                    instance_id,
                    ModelInstanceState.ERROR,
                    error_message="推理后端健康检查超时",
                )
                return

            # 6. RUNNING
            self._processes[instance_id] = process
            self._ports[instance_id] = port
            await self._report_state(
                instance_id,
                ModelInstanceState.RUNNING,
                service_port=port,
            )
            logger.info("实例 %s 已就绪 (端口 %d)", inst["name"], port)

        except Exception:
            logger.exception("处理实例 %s 异常", inst["name"])
            await self._report_state(
                instance_id,
                ModelInstanceState.ERROR,
                error_message="处理异常",
            )
        finally:
            self._handling.discard(instance_id)

    async def _wait_for_health(self, port: int, timeout: int = 60, process=None) -> bool:
        """等待推理后端健康检查通过。

        Args:
            port: 健康检查端口
            timeout: 超时秒数
            process: 可选的子进程对象，若进程退出则提前返回 False
        """
        import time

        url = f"http://127.0.0.1:{port}/health"
        deadline = time.time() + timeout

        while time.time() < deadline:
            # 进程已退出则无需继续等待
            if process is not None and process.returncode is not None:
                logger.warning(
                    "推理进程已退出 (code=%s)，停止健康检查", process.returncode
                )
                return False
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(2)

        return False

    async def stop_instance(self, instance_id: int) -> None:
        """停止推理后端实例（llama-server 和 rpc-server）。"""
        # 停止 llama-server（Master/standalone）
        process = self._processes.get(instance_id)
        if process:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
            except Exception:
                logger.exception("停止实例 %d 异常", instance_id)
            finally:
                del self._processes[instance_id]
                port = self._ports.pop(instance_id, None)
                if port:
                    self.release_port(port)
                logger.info("实例 %d 后端已停止", instance_id)

        # 停止 rpc-server（Slave）
        rpc_process = self._rpc_processes.get(instance_id)
        if rpc_process:
            try:
                rpc_process.terminate()
                await asyncio.wait_for(rpc_process.wait(), timeout=10)
            except asyncio.TimeoutError:
                rpc_process.kill()
            except Exception:
                logger.exception("停止 RPC Slave %d 异常", instance_id)
            finally:
                del self._rpc_processes[instance_id]
                logger.info("实例 %d rpc-server 已停止", instance_id)

    async def health_check(self, port: int) -> bool:
        """检查推理后端健康状态。"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def watch_instances(self) -> None:
        """轮询 Server 获取分配到本节点的实例并处理。"""
        while True:
            try:
                await self._poll_and_serve()
            except Exception:
                logger.exception("实例监听异常")
            await asyncio.sleep(10)

    async def _poll_and_serve(self) -> None:
        """轮询 Server 获取待启动实例。"""
        if not self._wm.worker_uuid or not self._wm.api_key:
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self._wm.effective_server_url}/v2/worker/instances",
                    headers={
                        "X-Worker-UUID": self._wm.worker_uuid,
                        "X-Worker-Key": self._wm.api_key,
                    },
                )
                if resp.status_code != 200:
                    logger.warning("查询分配实例失败: %s", resp.status_code)
                    return

                assigned = resp.json()

        except Exception:
            logger.debug("查询分配实例异常")
            return

        assigned_ids = set()

        for inst in assigned:
            instance_id = inst["id"]
            assigned_ids.add(instance_id)
            state = inst["state"]
            rpc_role = inst.get("rpc_role", "")

            # RUNNING 实例：健康检查（仅 Master/standalone）
            if state == "running":
                if instance_id in self._processes:
                    port = self._ports.get(instance_id)
                    if port and not await self.health_check(port):
                        logger.warning("实例 %s 健康检查失败，重启...", inst["name"])
                        await self.stop_instance(instance_id)
                        await self._report_state(
                            instance_id, ModelInstanceState.SCHEDULED
                        )
                # Slave 的 rpc-server 不需要健康检查（TCP 连接即可）
                continue

            # SCHEDULED 实例：启动处理
            # Slave 角色：实例可能已被 Master 改为 INITIALIZING/STARTING，
            # 但 Slave 仍需启动 rpc-server，故对所有 active 状态处理
            if state == "scheduled" or (
                rpc_role == "slave" and state in ("initializing", "downloading", "starting")
            ):
                await self._handle_instance(inst)

        # 清理已不在分配列表中的实例
        stale = (set(self._processes.keys()) | set(self._rpc_processes.keys())) - assigned_ids
        for instance_id in stale:
            logger.info("实例 %d 已从分配列表移除，停止后端", instance_id)
            await self.stop_instance(instance_id)
