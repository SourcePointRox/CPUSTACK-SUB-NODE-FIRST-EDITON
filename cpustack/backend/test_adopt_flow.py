import httpx

BASE = "http://127.0.0.1:8081"


def main():
    c = httpx.Client(timeout=60)
    # 1. 登录
    r = c.post(f"{BASE}/v2/auth/login", json={"username": "admin", "password": "cpustack"})
    print("=== 登录 ===", r.status_code)
    if r.status_code != 200:
        print(r.text)
        return
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # 2. 扫描局域网
    r = c.get(f"{BASE}/v2/discovery/scan", headers=h, params={"timeout": 8})
    print("=== 扫描 ===", r.status_code)
    print(r.json())
    data = r.json()
    discovered = data.get("discovered", [])
    if not discovered:
        print("未发现节点")
        return

    # 3. 对未注册节点执行 adopt
    for w in discovered:
        if w.get("registered"):
            print(f"已注册跳过: {w['name']} {w['ip']}")
            continue
        port = w.get("worker_port") or w.get("port") or 30080
        print(f"=== adopt {w['name']} {w['ip']}:{port} ===")
        r = c.post(
            f"{BASE}/v2/discovery/adopt",
            headers=h,
            json={"ip": w["ip"], "port": port, "name": w.get("name")},
        )
        print(r.status_code, r.json())


if __name__ == "__main__":
    main()
