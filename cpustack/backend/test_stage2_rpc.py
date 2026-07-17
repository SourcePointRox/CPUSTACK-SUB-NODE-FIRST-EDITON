"""阶段2 RPC 分布式推理端到端集成测试。

使用 SQLite 内存数据库，验证：
1. RPC 多节点调度（Master + Slaves 内存聚合）
2. RPC 单节点调度（单节点内存足够时退化为单机）
3. Master 角色查询（返回 rpc_role="master" + rpc_slaves 列表）
4. Slave 角色查询（返回 rpc_role="slave" + rpc_master_ip）
5. Master/Slave 状态上报
6. 指令集过滤（不匹配的节点被排除）

运行: python test_stage2_rpc.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# 确保导入项目模块
sys.path.insert(0, str(Path(__file__).parent))

# 配置日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_stage2")

# 必须先替换数据库引擎，再导入业务模块
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import cpustack.db as db_module

# 用 SQLite 内存库替换全局 engine（StaticPool 保持单连接，内存表才不会丢失）
_engine = create_async_engine(
    "sqlite+aiosqlite://",
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
db_module._engine = _engine
db_module._session_maker = _session_maker

# 导入业务模块（此时它们引用的 get_session/session_scope 已指向 SQLite）
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
    AssignedInstance,
    InstanceStateUpdate,
    list_assigned_instances,
    update_instance_state,
)

# 统计
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
    """创建所有表。"""
    # 触发所有模型注册到 SQLModel.metadata
    from cpustack.schemas import users, workers, models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("数据库表已创建")


async def cleanup_tables() -> None:
    """清空所有业务表（测试间数据隔离）。"""
    from sqlalchemy import text

    async with _engine.begin() as conn:
        for table in [
            "model_instances",
            "models",
            "worker_statuses",
            "workers",
            "api_keys",
            "users",
        ]:
            await conn.execute(text(f"DELETE FROM {table}"))
    logger.info("已清空业务表")


async def make_worker(
    session,
    name: str,
    uuid: str,
    api_key: str,
    ip: str,
    memory_available: int,
    instruction_sets: list[str] | None = None,
    cpu_cores: int = 8,
) -> tuple[Worker, WorkerStatus]:
    """创建一个 READY 状态的 Worker 及其 WorkerStatus。"""
    w = Worker(
        name=name,
        uuid=uuid,
        api_key=api_key,
        ip=ip,
        port=30080,
        state=WorkerState.READY,
    )
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


async def test_rpc_multinode_schedule() -> None:
    """测试1: RPC 多节点调度（模型内存超过单节点 → Master + Slave 聚合）。"""
    logger.info("=" * 60)
    logger.info("测试1: RPC 多节点调度")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="tester1", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        # 两个节点，各 2048MB 可用，模型需要 3072MB → 必须聚合
        wA, sA = await make_worker(session, "node-A", "uuid-a1", "key-a1", "10.0.0.1", 2048)
        wB, sB = await make_worker(session, "node-B", "uuid-b1", "key-b1", "10.0.0.2", 2048)

        model = Model(
            name="rpc-model-1",
            source_repo="huggingface",
            source_model_id="test/rpc-model-1",
            source_filename="model.gguf",
            backend=ModelBackend.LLAMA_CPP_RPC,
            estimated_memory=3072,
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="rpc-model-1-abc", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    # 触发调度（调度器内部用 session_scope，已指向 SQLite）
    await _schedule_instance(inst_id)

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check(
            "多节点: 状态=SCHEDULED",
            inst.state == ModelInstanceState.SCHEDULED,
            f"实际={inst.state.value}",
        )
        # Master 应是内存最大的节点（A 和 B 相等，A 在前）
        check(
            "多节点: Master=node-A",
            inst.worker_id == wA.id,
            f"实际 worker_id={inst.worker_id}",
        )
        rpc_ids = json.loads(inst.rpc_worker_ids)
        check(
            "多节点: Slaves含node-B",
            wB.id in rpc_ids,
            f"实际 rpc_worker_ids={inst.rpc_worker_ids}",
        )
        check(
            "多节点: 已分配内存=3072",
            inst.allocated_memory == 3072,
            f"实际={inst.allocated_memory}",
        )


async def test_rpc_single_node_schedule() -> None:
    """测试2: RPC 单节点调度（单节点内存足够 → 退化为单机，rpc_worker_ids=[]）。"""
    logger.info("=" * 60)
    logger.info("测试2: RPC 单节点调度（内存充足）")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="tester2", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        # 单节点 4096MB 足够 1024MB 模型
        wC, sC = await make_worker(session, "node-C", "uuid-c1", "key-c1", "10.0.0.3", 4096)
        wD, sD = await make_worker(session, "node-D", "uuid-d1", "key-d1", "10.0.0.4", 1024)

        model = Model(
            name="rpc-model-2",
            source_repo="huggingface",
            source_model_id="test/rpc-model-2",
            source_filename="model.gguf",
            backend=ModelBackend.LLAMA_CPP_RPC,
            estimated_memory=1024,
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="rpc-model-2-xyz", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check(
            "单节点: 状态=SCHEDULED",
            inst.state == ModelInstanceState.SCHEDULED,
            f"实际={inst.state.value}",
        )
        # 应选内存最大的 node-C
        check(
            "单节点: 部署到node-C",
            inst.worker_id == wC.id,
            f"实际 worker_id={inst.worker_id}",
        )
        check(
            "单节点: rpc_worker_ids=空数组",
            json.loads(inst.rpc_worker_ids) == [],
            f"实际={inst.rpc_worker_ids}",
        )


async def test_master_slave_role_query() -> None:
    """测试3: Master/Slave 角色查询（核心 RPC 协调逻辑）。"""
    logger.info("=" * 60)
    logger.info("测试3: Master/Slave 角色查询")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="tester3", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        wM, sM = await make_worker(session, "node-master", "uuid-m1", "key-m1", "10.0.0.10", 2048)
        wS, sS = await make_worker(session, "node-slave", "uuid-s1", "key-s1", "10.0.0.11", 2048)

        model = Model(
            name="rpc-model-3",
            source_repo="huggingface",
            source_model_id="test/rpc-model-3",
            source_filename="model.gguf",
            backend=ModelBackend.LLAMA_CPP_RPC,
            estimated_memory=3072,
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="rpc-model-3-def", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    # Master 查询
    async with _session_maker() as session:
        master_w = await session.get(Worker, wM.id)
        master_list = await list_assigned_instances(worker=master_w, session=session)

    check("Master: 查询到1个实例", len(master_list) == 1, f"实际={len(master_list)}")
    if master_list:
        m_inst = master_list[0]
        check(
            "Master: rpc_role=master",
            m_inst.rpc_role == "master",
            f"实际={m_inst.rpc_role}",
        )
        check(
            "Master: 含1个Slave",
            len(m_inst.rpc_slaves) == 1,
            f"实际={len(m_inst.rpc_slaves)}",
        )
        if m_inst.rpc_slaves:
            slave_info = m_inst.rpc_slaves[0]
            check(
                "Master: Slave=node-slave",
                slave_info.worker_name == "node-slave",
                f"实际={slave_info.worker_name}",
            )
            check(
                "Master: Slave IP=10.0.0.11",
                slave_info.ip == "10.0.0.11",
                f"实际={slave_info.ip}",
            )
            # RPC 端口约定: 50000 + worker_id
            expected_port = 50000 + wS.id
            check(
                "Master: Slave rpc_port=50000+id",
                slave_info.rpc_port == expected_port,
                f"实际={slave_info.rpc_port}, 期望={expected_port}",
            )

    # Slave 查询
    async with _session_maker() as session:
        slave_w = await session.get(Worker, wS.id)
        slave_list = await list_assigned_instances(worker=slave_w, session=session)

    check("Slave: 查询到1个实例", len(slave_list) == 1, f"实际={len(slave_list)}")
    if slave_list:
        s_inst = slave_list[0]
        check(
            "Slave: rpc_role=slave",
            s_inst.rpc_role == "slave",
            f"实际={s_inst.rpc_role}",
        )
        check(
            "Slave: rpc_master_ip=10.0.0.10",
            s_inst.rpc_master_ip == "10.0.0.10",
            f"实际={s_inst.rpc_master_ip}",
        )
        check(
            "Slave: rpc_slaves为空",
            s_inst.rpc_slaves == [],
            f"实际={s_inst.rpc_slaves}",
        )


async def test_state_report_master_and_slave() -> None:
    """测试4: Master 和 Slave 都能上报状态。"""
    logger.info("=" * 60)
    logger.info("测试4: Master/Slave 状态上报")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="tester4", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        wM, _ = await make_worker(session, "node-m2", "uuid-m2", "key-m2", "10.0.0.20", 2048)
        wS, _ = await make_worker(session, "node-s2", "uuid-s2", "key-s2", "10.0.0.21", 2048)

        model = Model(
            name="rpc-model-4",
            source_repo="huggingface",
            source_model_id="test/rpc-model-4",
            source_filename="model.gguf",
            backend=ModelBackend.LLAMA_CPP_RPC,
            estimated_memory=3072,
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="rpc-model-4-ghi", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    # Master 上报 RUNNING + service_port
    async with _session_maker() as session:
        master_w = await session.get(Worker, wM.id)
        await update_instance_state(
            instance_id=inst_id,
            update=InstanceStateUpdate(
                state=ModelInstanceState.RUNNING,
                service_port=40001,
            ),
            worker=master_w,
            session=session,
        )

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check(
            "Master上报: state=RUNNING",
            inst.state == ModelInstanceState.RUNNING,
            f"实际={inst.state.value}",
        )
        check(
            "Master上报: service_port=40001",
            inst.service_port == 40001,
            f"实际={inst.service_port}",
        )

    # Slave 上报 RUNNING + rpc-server 端口 (50000 + slave_id)
    slave_rpc_port = 50000 + wS.id
    async with _session_maker() as session:
        slave_w = await session.get(Worker, wS.id)
        # 注意: Slave 上报会覆盖实例的 service_port（当前设计 Slave 和 Master 共享一行）
        # 这里验证 Slave 有权上报（不被 403 拒绝）
        await update_instance_state(
            instance_id=inst_id,
            update=InstanceStateUpdate(
                state=ModelInstanceState.RUNNING,
                service_port=slave_rpc_port,
            ),
            worker=slave_w,
            session=session,
        )

    logger.info("Slave 状态上报成功（未被 403 拒绝）")


async def test_instruction_set_filter() -> None:
    """测试5: 指令集过滤（节点不满足 AVX-512 要求 → 调度失败保持 PENDING）。"""
    logger.info("=" * 60)
    logger.info("测试5: 指令集过滤")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="tester5", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        # 节点只支持 AVX2，但模型要求 AVX-512
        wE, _ = await make_worker(
            session, "node-e", "uuid-e1", "key-e1", "10.0.0.30", 8192,
            instruction_sets=["AVX2"],
        )

        model = Model(
            name="rpc-model-5",
            source_repo="huggingface",
            source_model_id="test/rpc-model-5",
            source_filename="model.gguf",
            backend=ModelBackend.LLAMA_CPP_RPC,
            estimated_memory=1024,
            required_instruction_sets=json.dumps(["AVX-512"]),
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="rpc-model-5-jkl", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check(
            "指令集: 状态=PENDING（无候选节点）",
            inst.state == ModelInstanceState.PENDING,
            f"实际={inst.state.value}",
        )
        check(
            "指令集: 含错误信息",
            "指令集" in inst.error_message or "节点" in inst.error_message,
            f"实际 error_message={inst.error_message}",
        )


async def test_memory_insufficient() -> None:
    """测试6: 集群总内存不足 → 调度失败。"""
    logger.info("=" * 60)
    logger.info("测试6: 集群内存不足")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="tester6", password_hash="x", is_admin=True)
        session.add(user)
        await session.commit()

        # 两个小节点，总内存仍不足
        wF, _ = await make_worker(session, "node-f", "uuid-f1", "key-f1", "10.0.0.40", 512)
        wG, _ = await make_worker(session, "node-g", "uuid-g1", "key-g1", "10.0.0.41", 512)

        model = Model(
            name="rpc-model-6",
            source_repo="huggingface",
            source_model_id="test/rpc-model-6",
            source_filename="model.gguf",
            backend=ModelBackend.LLAMA_CPP_RPC,
            estimated_memory=4096,  # 需要 4096+512=4608，集群只有 1024
            user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="rpc-model-6-mno", model_id=model.id)
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    await _schedule_instance(inst_id)

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check(
            "内存不足: 状态=PENDING",
            inst.state == ModelInstanceState.PENDING,
            f"实际={inst.state.value}",
        )
        check(
            "内存不足: 含'内存不足'",
            "内存不足" in inst.error_message,
            f"实际 error_message={inst.error_message}",
        )


async def main() -> None:
    await setup_schema()

    await test_rpc_multinode_schedule()
    await test_rpc_single_node_schedule()
    await test_master_slave_role_query()
    await test_state_report_master_and_slave()
    await test_instruction_set_filter()
    await test_memory_insufficient()

    logger.info("=" * 60)
    logger.info("测试结果汇总: 通过 %d / 失败 %d", _passed, _failed)
    logger.info("=" * 60)

    await _engine.dispose()

    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
