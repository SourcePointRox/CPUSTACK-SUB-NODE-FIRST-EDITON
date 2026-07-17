import httpx
import asyncio
from cpustack.db import session_scope
from cpustack.schemas.workers import Worker
from sqlmodel import select


async def main():
    # 1. 查数据库 workers 表
    print("=== 数据库 workers ===")
    async with session_scope() as session:
        ws = (await session.execute(select(Worker))).scalars().all()
        for w in ws:
            print(f"id={w.id} name={w.name} ip={w.ip} port={w.port} state={w.state} hb={w.heartbeat_at} uuid={w.uuid}")

    # 2. 子节点 /internal/health
    print("\n=== 子节点 /internal/health ===")
    try:
        r = httpx.get("http://192.168.1.232:30080/internal/health", timeout=10)
        print(r.status_code, r.text)
    except Exception as e:
        print("health failed:", e)

    # 3. 子节点 /internal/register（用正确参数）
    print("\n=== 子节点 /internal/register ===")
    try:
        r = httpx.post(
            "http://192.168.1.232:30080/internal/register",
            json={
                "server_url": "http://192.168.1.240:8081",
                "worker_token": "cpustack-cluster-token",
                "name": "DESKTOP-QSD5S23",
            },
            timeout=40,
        )
        print(r.status_code, r.text)
    except Exception as e:
        print("register failed:", e)


asyncio.run(main())
