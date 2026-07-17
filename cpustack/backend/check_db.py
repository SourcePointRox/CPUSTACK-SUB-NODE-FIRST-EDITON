"""快速查数据库 worker 状态。"""
import asyncio
from cpustack.db import session_scope
from cpustack.schemas.workers import Worker
from sqlmodel import select


async def c():
    async with session_scope() as s:
        for w in (await s.execute(select(Worker))).scalars().all():
            print(f"id={w.id} name={w.name} ip={w.ip} port={w.port} state={w.state} hb={w.heartbeat_at}")


asyncio.run(c())
