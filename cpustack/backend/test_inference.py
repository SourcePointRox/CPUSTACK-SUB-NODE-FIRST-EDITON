"""测试推理脚本：创建 API Key + 通过网关调用 OpenAI 兼容接口。

运行: python test_inference.py
"""
from __future__ import annotations

import sys
import httpx

SERVER = "http://127.0.0.1:8000"
ADMIN_USER = "admin"
ADMIN_PASS = "cpustack"
MODEL_NAME = "qwen2.5-3b"


def main() -> int:
    with httpx.Client(base_url=SERVER, timeout=120) as c:
        # 1. 登录
        t = c.post("/v2/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}).json()["access_token"]
        h = {"Authorization": f"Bearer {t}"}
        print("[1] 登录成功")

        # 2. 创建 API Key
        k = c.post("/v2/auth/api-keys", json={"name": "test-key"}, headers=h).json()
        api_key = k["access_token"]
        print(f"[2] API Key 已创建: {api_key}")

        # 3. 通过网关调用推理
        print(f"[3] 调用推理: model={MODEL_NAME}")
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": "你好，请用一句话介绍你自己。"}],
                "max_tokens": 80,
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        print(f"    HTTP 状态: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            print(f"    推理结果: {content}")
            print(f"    Token 用量: {data.get('usage', {})}")
        else:
            print(f"    错误: {r.text}")

        return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
