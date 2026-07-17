"""阶段4 生产化端到端集成测试。

验证：
1. WorkerController：UNREACHABLE 节点实例迁移
2. ModelController：副本扩缩容
3. InstanceController：失败实例自动重启（带退避）
4. RBAC：角色权限校验
5. API Key 模型白名单
6. Prometheus 指标采集
7. 配置中心 YAML 加载
8. 软文件锁

运行: python -u test_stage4.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_stage4")

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

from cpustack.bus import get_event_bus
from cpustack.schemas.users import User, UserRole, APIKey
from cpustack.schemas.workers import Worker, WorkerStatus, WorkerState
from cpustack.schemas.models import (
    Model,
    ModelBackend,
    ModelInstance,
    ModelInstanceState,
)
from cpustack.server.controllers import (
    WorkerController,
    ModelController,
    InstanceController,
)
from cpustack.server.controllers.instance_controller import (
    _parse_retry_count,
    _bump_retry,
    reset_retry_count,
    _MAX_RETRIES,
)
from cpustack.server.auth import _is_admin, require_role, create_jwt_token

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
    # 启动事件总线（控制器会发布事件）
    bus = get_event_bus()
    await bus.start()
    logger.info("数据库表已创建，事件总线已启动")


async def cleanup_tables() -> None:
    from sqlalchemy import text

    async with _engine.begin() as conn:
        for table in ["model_instances", "models", "worker_statuses", "workers", "api_keys", "users"]:
            await conn.execute(text(f"DELETE FROM {table}"))


async def make_worker(
    session, name: str, state: WorkerState, memory_available: int = 4096
) -> Worker:
    w = Worker(
        name=name, uuid=f"u-{name}", api_key=f"k-{name}",
        ip="10.0.0.1", port=30080, state=state,
    )
    session.add(w)
    await session.commit()
    s = WorkerStatus(
        worker_id=w.id, cpu_cores=8, memory_total=8192,
        memory_available=memory_available, instruction_sets='["AVX2"]',
    )
    session.add(s)
    await session.commit()
    return w


# ============ 测试1: WorkerController 实例迁移 ============
async def test_worker_controller_migration() -> None:
    logger.info("=" * 60)
    logger.info("测试1: WorkerController 实例迁移")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t1", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        model = Model(
            name="m1", source_repo="huggingface", source_model_id="t/m1",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=1, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        # UNREACHABLE Worker + UNREACHABLE 实例
        w = await make_worker(session, "node-fail", WorkerState.UNREACHABLE)
        inst = ModelInstance(
            name="m1-abc", model_id=model.id, worker_id=w.id,
            state=ModelInstanceState.UNREACHABLE,
            error_message="Worker node-fail 心跳超时",
        )
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    # 执行迁移
    ctrl = WorkerController()
    await ctrl.reconcile_all()

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("迁移: 状态=PENDING", inst.state == ModelInstanceState.PENDING, f"实际={inst.state.value}")
        check("迁移: worker_id=None", inst.worker_id is None, f"实际={inst.worker_id}")
        check("迁移: error_message含迁移标记", "迁移自故障节点" in inst.error_message, f"实际={inst.error_message}")


# ============ 测试2: WorkerController 跳过手动停止 ============
async def test_worker_controller_skip_pinned() -> None:
    logger.info("=" * 60)
    logger.info("测试2: WorkerController 跳过手动停止实例")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t2", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        model = Model(
            name="m2", source_repo="huggingface", source_model_id="t/m2",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=1, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        w = await make_worker(session, "node-fail2", WorkerState.UNREACHABLE)
        # 手动停止的实例（ERROR + 用户手动停止）
        inst = ModelInstance(
            name="m2-abc", model_id=model.id, worker_id=w.id,
            state=ModelInstanceState.ERROR,
            error_message="用户手动停止",
        )
        session.add(inst)
        await session.commit()
        inst_id = inst.id

    ctrl = WorkerController()
    await ctrl.reconcile_all()

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("跳过手动停止: 状态不变", inst.state == ModelInstanceState.ERROR, f"实际={inst.state.value}")
        check("跳过手动停止: worker_id不变", inst.worker_id == w.id, f"实际={inst.worker_id}")


# ============ 测试3: ModelController 扩容 ============
async def test_model_controller_scale_up() -> None:
    logger.info("=" * 60)
    logger.info("测试3: ModelController 扩容")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t3", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        # replicas=3 但只有1个实例
        model = Model(
            name="m3", source_repo="huggingface", source_model_id="t/m3",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=3, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(name="m3-abc", model_id=model.id, state=ModelInstanceState.RUNNING)
        session.add(inst)
        await session.commit()
        model_id = model.id

    ctrl = ModelController()
    await ctrl._reconcile_model(model_id)

    async with _session_maker() as session:
        from sqlmodel import select
        stmt = select(ModelInstance).where(ModelInstance.model_id == model_id)
        instances = (await session.execute(stmt)).scalars().all()
        check("扩容: 总实例数=3", len(instances) == 3, f"实际={len(instances)}")
        pending = [i for i in instances if i.state == ModelInstanceState.PENDING]
        check("扩容: 新增2个PENDING", len(pending) == 2, f"实际PENDING={len(pending)}")


# ============ 测试4: ModelController 缩容（优先删ERROR）============
async def test_model_controller_scale_down() -> None:
    logger.info("=" * 60)
    logger.info("测试4: ModelController 缩容优先删ERROR")
    await cleanup_tables()

    async with _session_maker() as session:
        from sqlmodel import select

        user = User(username="t4", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        # replicas=1 但有3个实例（1 RUNNING + 2 ERROR）
        model = Model(
            name="m4", source_repo="huggingface", source_model_id="t/m4",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=1, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        i1 = ModelInstance(name="m4-run", model_id=model.id, state=ModelInstanceState.RUNNING)
        i2 = ModelInstance(name="m4-err1", model_id=model.id, state=ModelInstanceState.ERROR, error_message="crash")
        i3 = ModelInstance(name="m4-err2", model_id=model.id, state=ModelInstanceState.ERROR, error_message="crash")
        session.add_all([i1, i2, i3])
        await session.commit()
        model_id = model.id
        running_id = i1.id

    ctrl = ModelController()
    await ctrl._reconcile_model(model_id)

    async with _session_maker() as session:
        from sqlmodel import select
        stmt = select(ModelInstance).where(ModelInstance.model_id == model_id)
        instances = (await session.execute(stmt)).scalars().all()
        check("缩容: 总实例数=1", len(instances) == 1, f"实际={len(instances)}")
        # 应保留 RUNNING（ERROR 被优先删除）
        check("缩容: 保留RUNNING", instances[0].state == ModelInstanceState.RUNNING, f"实际={instances[0].state.value}")


# ============ 测试5: InstanceController 自动重启 ============
async def test_instance_controller_auto_restart() -> None:
    logger.info("=" * 60)
    logger.info("测试5: InstanceController 自动重启")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t5", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        model = Model(
            name="m5", source_repo="huggingface", source_model_id="t/m5",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=1, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        # ERROR 实例，updated_at 设为 2 分钟前（超过 60s 冷却）
        inst = ModelInstance(
            name="m5-err", model_id=model.id, state=ModelInstanceState.ERROR,
            error_message="推理后端崩溃",
        )
        session.add(inst)
        await session.commit()
        # 直接更新 updated_at
        from sqlalchemy import text
        await session.execute(
            text("UPDATE model_instances SET updated_at = :t WHERE id = :id"),
            {"t": datetime.now(timezone.utc) - timedelta(seconds=120), "id": inst.id},
        )
        await session.commit()
        inst_id = inst.id

    ctrl = InstanceController()
    await ctrl.reconcile_all()

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("自动重启: 状态=PENDING", inst.state == ModelInstanceState.PENDING, f"实际={inst.state.value}")
        check("自动重启: worker_id=None", inst.worker_id is None, f"实际={inst.worker_id}")
        check("自动重启: error含[retry:1]", "[retry:1]" in inst.error_message, f"实际={inst.error_message}")


# ============ 测试6: InstanceController 退避上限 ============
async def test_instance_controller_max_retry() -> None:
    logger.info("=" * 60)
    logger.info("测试6: InstanceController 退避上限")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t6", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        model = Model(
            name="m6", source_repo="huggingface", source_model_id="t/m6",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=1, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        # 已达最大重试次数
        inst = ModelInstance(
            name="m6-max", model_id=model.id, state=ModelInstanceState.ERROR,
            error_message=f"[retry:{_MAX_RETRIES}] 持续崩溃",
        )
        session.add(inst)
        await session.commit()
        from sqlalchemy import text
        await session.execute(
            text("UPDATE model_instances SET updated_at = :t WHERE id = :id"),
            {"t": datetime.now(timezone.utc) - timedelta(seconds=120), "id": inst.id},
        )
        await session.commit()
        inst_id = inst.id

    ctrl = InstanceController()
    await ctrl.reconcile_all()

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("退避上限: 状态不变=ERROR", inst.state == ModelInstanceState.ERROR, f"实际={inst.state.value}")


# ============ 测试7: InstanceController 跳过手动停止 ============
async def test_instance_controller_skip_pinned() -> None:
    logger.info("=" * 60)
    logger.info("测试7: InstanceController 跳过手动停止")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t7", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        model = Model(
            name="m7", source_repo="huggingface", source_model_id="t/m7",
            source_filename="m.gguf", backend=ModelBackend.LLAMA_CPP_STANDALONE,
            replicas=1, estimated_memory=2048, user_id=user.id,
        )
        session.add(model)
        await session.commit()

        inst = ModelInstance(
            name="m7-stop", model_id=model.id, state=ModelInstanceState.ERROR,
            error_message="用户手动停止",
        )
        session.add(inst)
        await session.commit()
        from sqlalchemy import text
        await session.execute(
            text("UPDATE model_instances SET updated_at = :t WHERE id = :id"),
            {"t": datetime.now(timezone.utc) - timedelta(seconds=120), "id": inst.id},
        )
        await session.commit()
        inst_id = inst.id

    ctrl = InstanceController()
    await ctrl.reconcile_all()

    async with _session_maker() as session:
        inst = await session.get(ModelInstance, inst_id)
        check("跳过手动停止: 状态不变=ERROR", inst.state == ModelInstanceState.ERROR, f"实际={inst.state.value}")


# ============ 测试8: 退避函数单元测试 ============
async def test_retry_functions() -> None:
    logger.info("=" * 60)
    logger.info("测试8: 退避函数单元测试")

    check("parse: 无标记=0", _parse_retry_count("普通错误") == 0, "")
    check("parse: [retry:2]=2", _parse_retry_count("[retry:2] 崩溃") == 2, "")
    check("bump: 0→1", _bump_retry("err", 0) == "[retry:1] err", f"实际={_bump_retry('err', 0)}")
    check("bump: 递增2→3", _bump_retry("[retry:2] err", 2) == "[retry:3] err", f"实际={_bump_retry('[retry:2] err', 2)}")
    check("reset: 清除标记", reset_retry_count("[retry:3] 原始错误") == "原始错误", f"实际={reset_retry_count('[retry:3] 原始错误')}")


# ============ 测试9: RBAC 角色校验 ============
async def test_rbac_role_check() -> None:
    logger.info("=" * 60)
    logger.info("测试9: RBAC 角色校验")

    admin = User(id=1, username="admin", password_hash="x", role=UserRole.ADMIN, is_admin=True)
    normal = User(id=2, username="user", password_hash="x", role=UserRole.USER, is_admin=False)
    # 兼容性：is_admin=True 但 role=USER（旧数据）
    legacy = User(id=3, username="legacy", password_hash="x", role=UserRole.USER, is_admin=True)

    check("RBAC: admin 是管理员", _is_admin(admin), "")
    check("RBAC: user 非管理员", not _is_admin(normal), "")
    check("RBAC: 兼容 is_admin=True", _is_admin(legacy), "")

    # JWT 含 role
    token = create_jwt_token(1, "admin", True, role="admin")
    check("RBAC: JWT 生成成功", bool(token), "")
    from cpustack.server.auth import decode_jwt_token
    payload = decode_jwt_token(token)
    check("RBAC: JWT 含 role=admin", payload and payload.get("role") == "admin", f"实际={payload}")


# ============ 测试10: API Key 模型白名单 ============
async def test_api_key_whitelist() -> None:
    logger.info("=" * 60)
    logger.info("测试10: API Key 模型白名单")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t10", password_hash="x", role=UserRole.USER, is_admin=False)
        session.add(user)
        await session.commit()

        # 白名单只允许 model-a
        api_key = APIKey(
            name="test-key", access_token="sk-test10", user_id=user.id,
            enabled=True, allowed_model_names=json.dumps(["model-a"]),
        )
        session.add(api_key)

        # 创建两个模型
        ma = Model(
            name="model-a", source_repo="huggingface", source_model_id="t/a",
            source_filename="a.gguf", replicas=1, estimated_memory=1024, user_id=user.id,
        )
        mb = Model(
            name="model-b", source_repo="huggingface", source_model_id="t/b",
            source_filename="b.gguf", replicas=1, estimated_memory=1024, user_id=user.id,
        )
        session.add_all([ma, mb])
        await session.commit()

        # 给 model-a 创建 RUNNING 实例
        inst_a = ModelInstance(name="a-run", model_id=ma.id, state=ModelInstanceState.RUNNING, worker_id=1)
        session.add(inst_a)
        await session.commit()

    # 模拟 user 关联 api_key
    async with _session_maker() as session:
        user = await session.get(User, user.id)
        object.__setattr__(user, "api_key", api_key)

        from cpustack.server.routes.openai import _select_instance
        from fastapi import HTTPException

        # 白名单内的模型应通过
        try:
            inst = await _select_instance("model-a", session, user=user)
            check("白名单: 允许 model-a", inst is not None, "")
        except HTTPException as e:
            check("白名单: 允许 model-a", False, f"异常: {e.detail}")

        # 白名单外的模型应被拒
        try:
            await _select_instance("model-b", session, user=user)
            check("白名单: 拒绝 model-b", False, "应抛403")
        except HTTPException as e:
            check("白名单: 拒绝 model-b", e.status_code == 403, f"实际 status={e.status_code}")


# ============ 测试11: Prometheus 指标 ============
async def test_prometheus_metrics() -> None:
    logger.info("=" * 60)
    logger.info("测试11: Prometheus 指标采集")
    await cleanup_tables()

    async with _session_maker() as session:
        user = User(username="t11", password_hash="x", is_admin=True, role=UserRole.ADMIN)
        session.add(user)
        await session.commit()

        w = await make_worker(session, "node-metrics", WorkerState.READY)
        model = Model(
            name="m11", source_repo="huggingface", source_model_id="t/m11",
            source_filename="m.gguf", replicas=2, estimated_memory=1024, user_id=user.id,
        )
        session.add(model)
        await session.commit()
        inst = ModelInstance(name="m11-run", model_id=model.id, state=ModelInstanceState.RUNNING, worker_id=w.id)
        session.add(inst)
        await session.commit()

    from cpustack.server.metrics import collect_metrics, render_metrics

    await collect_metrics()
    body, content_type = render_metrics()

    check("指标: content_type 正确", "text/plain" in content_type, f"实际={content_type}")
    body_str = body.decode("utf-8") if isinstance(body, bytes) else body
    check("指标: 含 workers_total", "cpustack_workers_total" in body_str, "")
    check("指标: 含 instances_total", "cpustack_instances_total" in body_str, "")
    check("指标: 含 model_replicas", "cpustack_model_replicas" in body_str, "")
    check("指标: 含 ready=1", 'cpustack_model_replicas_ready{model_name="m11"} 1.0' in body_str, "")


# ============ 测试12: 配置中心 YAML ============
async def test_config_yaml() -> None:
    logger.info("=" * 60)
    logger.info("测试12: 配置中心 YAML 加载")

    # 写临时 YAML
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write("profile: prod\ncors_origins: ['https://example.com', 'https://api.example.com']\nmetrics_enabled: false\n")
        yaml_path = f.name

    import os
    os.environ["CPUSTACK_CONFIG_FILE"] = yaml_path

    # 重新加载 config
    import importlib
    import cpustack.config
    importlib.reload(cpustack.config)

    check("YAML: profile=prod", cpustack.config.settings.profile == "prod", f"实际={cpustack.config.settings.profile}")
    check("YAML: cors_origins 收紧", "https://example.com" in cpustack.config.settings.cors_origins, f"实际={cpustack.config.settings.cors_origins}")
    check("YAML: metrics_enabled=false", cpustack.config.settings.metrics_enabled == False, f"实际={cpustack.config.settings.metrics_enabled}")

    # 清理
    del os.environ["CPUSTACK_CONFIG_FILE"]
    Path(yaml_path).unlink(missing_ok=True)
    importlib.reload(cpustack.config)


# ============ 测试13: 软文件锁 ============
async def test_soft_file_lock() -> None:
    logger.info("=" * 60)
    logger.info("测试13: 软文件锁（ServeManager._download_model）")

    from cpustack.worker.serve_manager import ServeManager

    # 验证 _download_locks 字典和 _download_model 方法存在
    class FakeWM:
        worker_id = 1
        worker_uuid = "test"
        api_key = "test"

    sm = ServeManager(FakeWM())
    check("软锁: _download_locks 初始化", isinstance(sm._download_locks, dict), "")
    check("软锁: _download_model 方法存在", hasattr(sm, "_download_model"), "")

    # 模拟缓存命中（不实际下载）
    inst = {"source_repo": "huggingface", "source_model_id": "test/soft", "source_filename": "model.gguf"}

    # Mock get_cached_model_path 返回路径
    from cpustack.worker import downloader
    original = downloader.get_cached_model_path
    downloader.get_cached_model_path = lambda *a, **k: "/fake/path/model.gguf"

    try:
        path = await sm._download_model(inst, 999)
        check("软锁: 缓存命中返回路径", path == "/fake/path/model.gguf", f"实际={path}")
        check("软锁: 无锁创建（缓存命中不锁）", len(sm._download_locks) == 0, f"实际={len(sm._download_locks)}")
    finally:
        downloader.get_cached_model_path = original


async def main() -> None:
    await setup_schema()

    await test_worker_controller_migration()
    await test_worker_controller_skip_pinned()
    await test_model_controller_scale_up()
    await test_model_controller_scale_down()
    await test_instance_controller_auto_restart()
    await test_instance_controller_max_retry()
    await test_instance_controller_skip_pinned()
    await test_retry_functions()
    await test_rbac_role_check()
    await test_api_key_whitelist()
    await test_prometheus_metrics()
    await test_config_yaml()
    await test_soft_file_lock()

    logger.info("=" * 60)
    logger.info("测试结果汇总: 通过 %d / 失败 %d", _passed, _failed)
    logger.info("=" * 60)

    bus = get_event_bus()
    await bus.stop()
    await _engine.dispose()

    if _failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
