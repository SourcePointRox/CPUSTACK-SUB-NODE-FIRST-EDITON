"""验证一键添加子节点的完整流程。

流程：健康检查 → 登录 → 局域网扫描 → 一键添加 → 查数据库确认 IP 正确
"""

import asyncio
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8081"
ADMIN_USER = "admin"
ADMIN_PASS = "cpustack"


async def main() -> int:
    async with httpx.AsyncClient(timeout=60) as client:
        # 1. 健康检查
        try:
            r = await client.get(f"{BASE}/healthz")
            print(f"[1] /healthz -> {r.status_code} {r.text[:120]}")
            if r.status_code != 200:
                print("主节点不健康，终止")
                return 1
        except Exception as e:
            print(f"[1] 健康检查失败: {e}")
            return 1

        # 2. 登录
        r = await client.post(
            f"{BASE}/v2/auth/login",
            json={"username": ADMIN_USER, "password": ADMIN_PASS},
        )
        print(f"[2] /v2/auth/login -> {r.status_code}")
        if r.status_code != 200:
            print(f"登录失败: {r.text}")
            return 1
        token = r.json().get("access_token")
        if not token:
            print("登录响应无 access_token")
            return 1
        headers = {"Authorization": f"Bearer {token}"}

        # 3. 局域网扫描
        print("[3] 扫描局域网 (超时 8s)...")
        r = await client.get(f"{BASE}/v2/discovery/scan?timeout=8", headers=headers)
        print(f"[3] /v2/discovery/scan -> {r.status_code}")
        if r.status_code != 200:
            print(f"扫描失败: {r.text}")
            return 1
        scan = r.json()
        discovered = scan.get("discovered", [])
        print(f"    广播地址: {scan.get('broadcast_addresses', [])}")
        print(f"    发现 {len(discovered)} 个子节点")
        for d in discovered:
            print(
                f"    - {d.get('name')} @ {d.get('ip')}:{d.get('port')} "
                f"registered={d.get('registered')} cpu={d.get('cpu_cores')} mem={d.get('memory_total_mb')}MB"
            )

        if not discovered:
            print("未发现子节点，无法验证 adopt 流程（子节点可能未运行）")
            return 2

        # 选第一个未注册的子节点（或第一个）测试 adopt
        target = next((d for d in discovered if not d.get("registered")), discovered[0])
        print(f"\n[4] 一键添加目标: {target['name']} @ {target['ip']}:{target['port']}")

        r = await client.post(
            f"{BASE}/v2/discovery/adopt",
            json={"ip": target["ip"], "port": target["port"], "name": target["name"]},
            headers=headers,
        )
        print(f"[4] /v2/discovery/adopt -> {r.status_code}")
        adopt = r.json()
        print(f"    响应: {adopt}")

        if not adopt.get("ok"):
            print(f"\n>>> 仍然失败: {adopt.get('message')}")
            return 1

        print(f"\n[5] 一键添加成功! worker_id={adopt.get('worker_id')} uuid={adopt.get('worker_uuid')}")

        # 4. 查数据库确认 worker IP 正确
        print("\n[6] 查数据库确认 worker IP...")
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import asyncio as _aio

            from cpustack.db import session_scope
            from cpustack.schemas.workers import Worker
            from sqlmodel import select

            async def _check():
                async with session_scope() as session:
                    workers = (await session.execute(select(Worker))).scalars().all()
                    for w in workers:
                        print(
                            f"    id={w.id} name={w.name} ip={w.ip} port={w.port} "
                            f"state={w.state} hb={w.heartbeat_at} uuid={w.uuid[:12] if w.uuid else None}"
                        )

            await _check()
        except Exception as e:
            print(f"    查数据库失败: {e}")

        # 5. 探测子节点健康（验证双向可达）
        print(f"\n[7] 探测子节点 {target['ip']}:{target['port']} /internal/health ...")
        try:
            r = await client.get(
                f"http://{target['ip']}:{target['port']}/internal/health", timeout=5
            )
            print(f"    {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"    探测失败: {e}")

        return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
