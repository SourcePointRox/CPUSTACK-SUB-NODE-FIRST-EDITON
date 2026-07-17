"""阶段3 算力扩展端到端集成测试。

验证：
1. 流水线并行调度（prima_cpp 多节点层切片分配）
2. 流水线角色查询（Master/Worker 层分配信息）
3. 流水线单节点退化
4. 数据并行多副本调度（replicas=N 独立调度到不同节点）
5. 负载均衡器（轮询 + 最少连接）
6. 层切片分配策略（按 CPU 核心比例）

运行: python test_stage3.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_stage3")

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import cpustack.db as db_module

_engine = create_async_engine(
    "sqlite+aiosqlite://",
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
db_module._engine = _engine
db_module._session_maker = _session_maker

from cpustack.schemas.users import User
from cpustack.schemas.workers import Worker, WorkerStatus, WorkerState
from cpustack.schemas.models import (
    Model,
    ModelBackend,
    ModelInstance,
    ModelInstanceState,
)
from cpustack.server.scheduler.scheduler import _schedule_instance
from cpustack.server.routes.worker_api import (
    list_assigned_instances,
)
from cpustack.server.gateway.load_balancer import (
    RoundRobinBalancer,
    LeastConnectionsBalancer,
    set_load_balancer,
)
from cpustack.worker.backends.prima_cpp import compute_layer_split

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


async def setup_schema() -> None:
    from cpustack.schemas import users, workers, models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("数据库表已创建")


async def cleanup_tables() -> None:
    from sqlalchemy import text

    async with _engine.begin() as conn:
        for table in ["model_instances", "models", "worker_statuses", "workers", "api_keys", "users"]:
            await conn.execute(text(f"DELETE FROM {table}"))
    logger.info("已清空业务表")


async def make_worker(
    session,
    name: str,
    uuid: str,
    api_key: str,
    ip: str,
    memory_available: int,
    cpu_cores: int = 8,
    instruction_sets: list[str] | None = None,
) -> tuple[Worker, WorkerStatus]:
    w = Worker(name=name, uuid=uuid, api_key=api_key, ip=ip, port=30080, state=WorkerState.READY)
    session.add(w)
    await session.commit()

    s = WorkerStatus(
        worker_id=w.id,
        cpu_cores=cpu_cores,
        cpu_allocated=0,
        memory_total=memory_available + 1024,
        memory_allocated=0,
        memory_available=memory_available,
        instruction_sets=json.dumps(instruction_sets or ["AVX2"]),
    )
    session.add(s)
    await session.commit()
    return w, s


async def test_pipeline_multinode_schedule() -> None:
    """测试1: 流水线并行多节点调度（2节点层切片分配）。"""
    logger.info("=" * 60)
    logger.info("测试1: 流水线并行多节点调度")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t1", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        # 2节点，各 4096MB（每节点需装载完整 2048MB 模型）
        wA, _ = await make_worker(session, "node-A", "u-a", "k-a", "10.0.0.1", 4096, cpu_cores=8)
        wB, _ = await make_worker(session, "node-B", "u-b", "k-b", "10.0.0.2", 4096, cpu_cores=16)

        # 流水线模型：replicas=2 表示2节点流水线，total_layers=32
        model = Model(
            name="prima-model-1",
            source_repo="huggingface",
            source_model_id="test/prima-1",
            source_filename="model.gguf",
            backend=ModelBackend.PRIMA_CPP,
            replicas=2,
            estimated_memory=2048,
            user_id=user.id,
            backend_parameters=json.dumps({"total_layers": 32}),
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="prima-model-1-abc", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("流水线: 状态=SCHEDULED", inst.state == ModelInstanceState.SCHEDULED, f"实际={inst.state.value}")
        check("流水线: Master=node-B(核心多)", inst.worker_id == wB.id, f"实际 worker_id={inst.worker_id}")

        slave_ids = json.loads(inst.rpc_worker_ids)
        check("流水线: 含1个Worker", len(slave_ids) == 1, f"实际={slave_ids}")
        check("流水线: Worker=node-A", wA.id in slave_ids, f"实际={slave_ids}")

        # 验证层分配
        dist_cfg = json.loads(inst.distributed_config)
        pipeline = dist_cfg.get("pipeline", [])
        check("流水线: 配置含2节点", len(pipeline) == 2, f"实际={len(pipeline)}")

        if len(pipeline) == 2:
            # 层段连续且覆盖 [0, 32)
            layers = sorted([(p["layer_start"], p["layer_end"]) for p in pipeline])
            check("流水线: 首段从0开始", layers[0][0] == 0, f"实际={layers}")
            check("流水线: 末段到31", layers[-1][1] == 31, f"实际={layers}")
            check("流水线: 层段连续", layers[0][1] + 1 == layers[1][0], f"实际={layers}")

            # 核心多的节点(B, 16核)应分到更多层
            b_cfg = next(p for p in pipeline if p["worker_id"] == wB.id)
            a_cfg = next(p for p in pipeline if p["worker_id"] == wA.id)
            b_layers = b_cfg["layer_end"] - b_cfg["layer_start"] + 1
            a_layers = a_cfg["layer_end"] - a_cfg["layer_start"] + 1
            check("流水线: 核心多=层多", b_layers > a_layers, f"B={b_layers}层 A={a_layers}层")


async def test_pipeline_role_query() -> None:
    """测试2: 流水线 Master/Worker 角色查询。"""
    logger.info("=" * 60)
    logger.info("测试2: 流水线 Master/Worker 角色查询")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t2", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        wM, _ = await make_worker(session, "node-master", "u-m", "k-m", "10.0.0.10", 4096, cpu_cores=8)
        wW, _ = await make_worker(session, "node-worker", "u-w", "k-w", "10.0.0.11", 4096, cpu_cores=8)

        model = Model(
            name="prima-model-2",
            source_repo="huggingface",
            source_model_id="test/prima-2",
            source_filename="model.gguf",
            backend=ModelBackend.PRIMA_CPP,
            replicas=2,
            estimated_memory=2048,
            user_id=user.id,
            backend_parameters=json.dumps({"total_layers": 16}),
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="prima-model-2-def", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    # Master 查询
    async with _session_maker() as session:
        master_w = await session.get(Worker, wM.id)
        master_list = await list_assigned_instances(worker=master_w, session=session)

    check("Master查询: 1个实例", len(master_list) == 1, f"实际={len(master_list)}")
    if master_list:
        m = master_list[0]
        check("Master: pipeline_role=master", m.pipeline_role == "master", f"实际={m.pipeline_role}")
        check("Master: 含1个Worker", len(m.pipeline_workers) == 1, f"实际={len(m.pipeline_workers)}")
        if m.pipeline_workers:
            pw = m.pipeline_workers[0]
            check("Master: Worker=node-worker", pw.worker_name == "node-worker", f"实际={pw.worker_name}")
            check("Master: Worker IP=10.0.0.11", pw.ip == "10.0.0.11", f"实际={pw.ip}")
            check("Master: Worker port=50000+id", pw.port == 50000 + wW.id, f"实际={pw.port}")
            check("Master: Worker rank=1", pw.rank == 1, f"实际={pw.rank}")

    # Worker 查询
    async with _session_maker() as session:
        worker_w = await session.get(Worker, wW.id)
        worker_list = await list_assigned_instances(worker=worker_w, session=session)

    check("Worker查询: 1个实例", len(worker_list) == 1, f"实际={len(worker_list)}")
    if worker_list:
        w = worker_list[0]
        check("Worker: pipeline_role=worker", w.pipeline_role == "worker", f"实际={w.pipeline_role}")
        check("Worker: master_ip=10.0.0.10", w.pipeline_master_ip == "10.0.0.10", f"实际={w.pipeline_master_ip}")
        check("Worker: rank=1", w.rank == 1, f"实际={w.rank}")
        check("Worker: layer_start>=0", w.layer_start >= 0, f"实际={w.layer_start}")
        check("Worker: layer_end>layer_start", w.layer_end > w.layer_start, f"start={w.layer_start} end={w.layer_end}")


async def test_pipeline_single_node_degrade() -> None:
    """测试3: 流水线单节点退化（仅1个节点 → 退化为单机）。"""
    logger.info("=" * 60)
    logger.info("测试3: 流水线单节点退化")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t3", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        wS, _ = await make_worker(session, "node-solo", "u-s", "k-s", "10.0.0.20", 4096, cpu_cores=8)

        model = Model(
            name="prima-model-3",
            source_repo="huggingface",
            source_model_id="test/prima-3",
            source_filename="model.gguf",
            backend=ModelBackend.PRIMA_CPP,
            replicas=2,  # 期望2节点但只有1个
            estimated_memory=2048,
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="prima-model-3-ghi", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("单节点退化: 状态=SCHEDULED", inst.state == ModelInstanceState.SCHEDULED, f"实际={inst.state.value}")
        check("单节点退化: 部署到node-solo", inst.worker_id == wS.id, f"实际={inst.worker_id}")
        check("单节点退化: rpc_worker_ids=空", json.loads(inst.rpc_worker_ids) == [], f"实际={inst.rpc_worker_ids}")
        dist_cfg = json.loads(inst.distributed_config)
        check("单节点退化: 配置=single", dist_cfg.get("pipeline") == "single", f"实际={dist_cfg}")


async def test_data_parallel_multi_replica() -> None:
    """测试4: 数据并行多副本调度（replicas=2 → 2个实例分散到不同节点）。"""
    logger.info("=" * 60)
    logger.info("测试4: 数据并行多副本调度")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t4", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        w1, _ = await make_worker(session, "node-dp1", "u-d1", "k-d1", "10.0.0.30", 4096, cpu_cores=8)
        w2, _ = await make_worker(session, "node-dp2", "u-d2", "k-d2", "10.0.0.31", 4096, cpu_cores=8)

        model = Model(
            name="dp-model-1",
            source_repo="huggingface",
            source_model_id="test/dp-1",
            source_filename="model.gguf",
            backend=ModelBackend.DATA_PARALLEL,
            replicas=2,
            estimated_memory=2048,
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        # 模拟 routes/models.py 创建 replicas 个实例
        inst1 = ModelInstance(name="dp-model-1-a1b2", model_id=model.id)
        inst2 = ModelInstance(name="dp-model-1-c3d4", model_id=model.id)
        session.add(inst1)
        session.add(inst2)
        await session.commit()
        id1, id2 = inst1.id, inst2.id

    # 分别调度两个副本
    await _schedule_instance(id1)
    await _schedule_instance(id2)

    async with _session_maker() as session:
        i1 = await session.get(ModelInstance, id1)
        i2 = await session.get(ModelInstance, id2)

        check("数据并行: 副本1=SCHEDULED", i1.state == ModelInstanceState.SCHEDULED, f"实际={i1.state.value}")
        check("数据并行: 副本2=SCHEDULED", i2.state == ModelInstanceState.SCHEDULED, f"实际={i2.state.value}")
        # SPREAD 策略应分散到不同节点
        check(
            "数据并行: 副本分散到不同节点",
            i1.worker_id != i2.worker_id,
            f"副本1={i1.worker_id} 副本2={i2.worker_id}",
        )
        # 两个副本都无分布式角色（独立实例）
        check("数据并行: 副本1无rpc_role", i1.rpc_worker_ids == "[]", f"实际={i1.rpc_worker_ids}")
        check("数据并行: 副本2无rpc_role", i2.rpc_worker_ids == "[]", f"实际={i2.rpc_worker_ids}")


async def test_load_balancer_round_robin() -> None:
    """测试5: 轮询负载均衡器。"""
    logger.info("=" * 60)
    logger.info("测试5: 轮询负载均衡器")
    await cleanup_tables()

    # 构造3个模拟实例
    instances = [
        ModelInstance(id=i, name=f"inst-{i}", model_id=1, state=ModelInstanceState.RUNNING)
        for i in [101, 102, 103]
    ]

    balancer = RoundRobinBalancer()
    # 连续选择6次，应轮询覆盖所有实例各2次
    selected_ids = []
    for _ in range(6):
        chosen = balancer.select(instances)
        selected_ids.append(chosen.id)

    # 轮询应 101,102,103,101,102,103
    check("轮询: 覆盖所有实例", set(selected_ids) == {101, 102, 103}, f"实际={selected_ids}")
    check("轮询: 每个实例2次", selected_ids.count(101) == 2, f"实际={selected_ids}")

    # 空列表
    check("轮询: 空列表返回None", balancer.select([]) is None, "")

    # 单实例
    single = balancer.select([instances[0]])
    check("轮询: 单实例直接返回", single.id == 101, f"实际={single.id if single else None}")


async def test_load_balancer_least_connections() -> None:
    """测试6: 最少连接负载均衡器。"""
    logger.info("=" * 60)
    logger.info("测试6: 最少连接负载均衡器")

    instances = [
        ModelInstance(id=i, name=f"inst-{i}", model_id=1, state=ModelInstanceState.RUNNING)
        for i in [201, 202, 203]
    ]

    balancer = LeastConnectionsBalancer()

    # 模拟连接数：201有3个活跃，202有1个，203有0个
    balancer.acquire(201)
    balancer.acquire(201)
    balancer.acquire(201)
    balancer.acquire(202)

    # 应选活跃连接最少的 203
    chosen = balancer.select(instances)
    check("最少连接: 选最空闲实例", chosen.id == 203, f"实际={chosen.id if chosen else None}")
    check("最少连接: 203活跃=0", balancer.active_count(203) == 0, f"实际={balancer.active_count(203)}")
    check("最少连接: 201活跃=3", balancer.active_count(201) == 3, f"实际={balancer.active_count(201)}")

    # 释放后重新选择
    balancer.acquire(203)
    balancer.acquire(203)
    # 现在 202 最少(1个)
    chosen2 = balancer.select(instances)
    check("最少连接: 释放后重选", chosen2.id == 202, f"实际={chosen2.id if chosen2 else None}")

    # release 测试
    balancer.release(201)
    check("最少连接: release减少计数", balancer.active_count(201) == 2, f"实际={balancer.active_count(201)}")


async def test_layer_split_strategy() -> None:
    """测试7: 层切片分配策略（按CPU核心比例）。"""
    logger.info("=" * 60)
    logger.info("测试7: 层切片分配策略")

    # 3节点，核心数 4:8:12 = 1:2:3，总24层
    # A应得 4/24*24=4层, B应得 8/24*24=8层, C应得 12/24*24=12层
    split = compute_layer_split(24, [(1, 4), (2, 8), (3, 12)])

    check("层切片: 3段", len(split) == 3, f"实际={len(split)}")
    if len(split) == 3:
        check("层切片: 从0开始", split[0]["layer_start"] == 0, f"实际={split}")
        check("层切片: 到23结束", split[-1]["layer_end"] == 23, f"实际={split}")
        check("层切片: 层段连续", split[0]["layer_end"] + 1 == split[1]["layer_start"], f"实际={split}")
        check("层切片: rank递增", [s["rank"] for s in split] == [0, 1, 2], f"实际={split}")

        # 核心多的层多
        a_layers = split[0]["layer_end"] - split[0]["layer_start"] + 1
        c_layers = split[2]["layer_end"] - split[2]["layer_start"] + 1
        check("层切片: 核心多=层多", c_layers > a_layers, f"A={a_layers} C={c_layers}")

    # 边界：空输入
    check("层切片: 空输入", compute_layer_split(32, []) == [], "")
    check("层切片: 0层", compute_layer_split(0, [(1, 8)]) == [], "")

    # 单节点
    single = compute_layer_split(32, [(1, 8)])
    check("层切片: 单节点全层", len(single) == 1 and single[0]["layer_start"] == 0 and single[0]["layer_end"] == 31, f"实际={single}")


async def main() -> None:
    await setup_schema()

    await test_pipeline_multinode_schedule()
    await test_pipeline_role_query()
    await test_pipeline_single_node_degrade()
    await test_data_parallel_multi_replica()
    await test_load_balancer_round_robin()
    await test_load_balancer_least_connections()
    await test_layer_split_strategy()

    logger.info("=" * 60)
    logger.info("测试结果汇总: 通过 %d / 失败 %d", _passed, _failed)
    logger.info("=" * 60)

    await _engine.dispose()

    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
