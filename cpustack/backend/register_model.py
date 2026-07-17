"""注册测试模型脚本：登录 + 创建模型 + 查询实例状态。

默认注册 llama-3.2-1b（0.9GB，最小测试模型）。
运行: python register_model.py
"""
from __future__ import annotations

import json
import sys
import time

import httpx

SERVER = "http://127.0.0.1:8000"
ADMIN_USER = "admin"
ADMIN_PASS = "cpustack"

# 模型配置：Qwen2.5-3B 官方 GGUF（公开仓库，无需授权，~2GB）
MODEL_CONFIG = {
    "name": "qwen2.5-3b",
    "display_name": "Qwen2.5 3B Instruct (Q4_K_M)",
    "description": "阿里 Qwen2.5 3B 指令微调版，多语言支持，公开 GGUF 仓库",
    "source_repo": "huggingface",
    "source_model_id": "Qwen/Qwen2.5-3B-Instruct-GGUF",
    "source_filename": "qwen2.5-3b-instruct-q4_k_m.gguf",
    "backend": "llama_cpp_standalone",
    "replicas": 1,
    "estimated_memory": 3072,
    "required_instruction_sets": [],
    "backend_parameters": {},
}


def login(client: httpx.Client) -> str:
    """登录获取 JWT。"""
    resp = client.post(
        "/v2/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    print(f"[登录成功] JWT 已获取")
    return token


def create_model(client: httpx.Client, token: str) -> dict:
    """创建模型。"""
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post("/v2/models", json=MODEL_CONFIG, headers=headers)
    if resp.status_code == 400 and "already exists" in resp.text.lower():
        print(f"[模型已存在] 跳过创建")
        # 查询现有模型
        resp = client.get("/v2/models", headers=headers)
        resp.raise_for_status()
        for m in resp.json():
            if m["name"] == MODEL_CONFIG["name"]:
                return m
        return {}
    resp.raise_for_status()
    model = resp.json()
    print(f"[模型创建成功] id={model['id']} name={model['name']}")
    return model


def list_instances(client: httpx.Client, token: str) -> list:
    """查询所有实例。"""
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/v2/models/instances", headers=headers)
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    with httpx.Client(base_url=SERVER, timeout=30) as client:
        # 1. 登录
        token = login(client)

        # 2. 创建模型
        model = create_model(client, token)
        if not model:
            print("[错误] 模型创建失败且未找到现有模型")
            return 1

        print(f"\n=== 模型信息 ===")
        print(f"  ID: {model['id']}")
        print(f"  名称: {model['name']}")
        print(f"  后端: {model['backend']}")
        print(f"  源: {model['source_repo']}/{model['source_model_id']}")
        print(f"  文件: {MODEL_CONFIG['source_filename']}")
        print(f"  估算内存: {model['estimated_memory']}MB")
        print(f"  副本数: {model['replicas']}")

        # 3. 查询实例状态
        print(f"\n=== 实例状态 ===")
        instances = list_instances(client, token)
        for inst in instances:
            print(f"  实例 {inst['id']}: {inst['name']}")
            print(f"    模型: {inst['model_name']}")
            print(f"    状态: {inst['state']}")
            print(f"    Worker: {inst['worker_name'] or '(未调度)'}")
            print(f"    下载进度: {inst['download_progress']*100:.1f}%")
            if inst['error_message']:
                print(f"    错误: {inst['error_message']}")

        print(f"\n[完成] 模型已注册，Worker 将自动下载模型文件（约 0.9GB）")
        print(f"[提示] 可访问 http://localhost:8000/ 查看控制台")
        print(f"[提示] 实例状态查询: GET /v2/models/instances")
        return 0


if __name__ == "__main__":
    sys.exit(main())
