"""模型目录服务：加载 YAML 目录 + 按名称拉取部署。

目录文件：cpustack/catalog/model_catalog.yaml
提供：
- list_catalog(): 返回所有目录条目（含分类标签）
- get_catalog_entry(name): 按名称获取条目
- pull_from_catalog(name, ...): 创建 Model + 实例并触发调度
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import select

from cpustack.db import session_scope
from cpustack.schemas.models import Model, ModelBackend, ModelInstance, ModelInstanceState
from cpustack.schemas.users import User

logger = logging.getLogger(__name__)

_CATALOG_CACHE: list[dict[str, Any]] | None = None


def _catalog_path() -> Path:
    return Path(__file__).resolve().parent / "model_catalog.yaml"


def list_catalog() -> list[dict[str, Any]]:
    """加载并返回模型目录（带分类标签）。

    缓存首次加载结果，避免每次请求读文件。
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    path = _catalog_path()
    if not path.exists():
        logger.warning("模型目录文件不存在: %s", path)
        _CATALOG_CACHE = []
        return _CATALOG_CACHE

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.exception("加载模型目录失败: %s", path)
        _CATALOG_CACHE = []
        return _CATALOG_CACHE

    raw_models = data.get("models", []) or []
    result: list[dict[str, Any]] = []
    for m in raw_models:
        entry = {
            "name": m.get("name", ""),
            "display_name": m.get("display_name", m.get("name", "")),
            "description": m.get("description", ""),
            "parameters": m.get("parameters", ""),
            "source_repo": m.get("source_repo", "huggingface"),
            "source_model_id": m.get("source_model_id", ""),
            "source_filename": m.get("source_filename", ""),
            "quantization_size_gb": m.get("quantization_size_gb", 0),
            "estimated_memory_mb": m.get("estimated_memory_mb", 0),
            "required_instruction_sets": m.get("required_instruction_sets", []) or [],
            "recommended_backend": m.get("recommended_backend", "llama_cpp_standalone"),
            "test_purpose": m.get("test_purpose", ""),
            # 派生字段：用于 UI 分类
            "category": _categorize(m),
            "size_tier": _size_tier(m.get("quantization_size_gb", 0)),
        }
        result.append(entry)

    _CATALOG_CACHE = result
    logger.info("已加载模型目录：%d 个模型", len(result))
    return result


def _categorize(entry: dict) -> str:
    """根据模型名/描述推断分类。"""
    name = (entry.get("name", "") + " " + entry.get("display_name", "")).lower()
    if "llama" in name:
        return "Llama"
    if "qwen" in name:
        return "Qwen"
    if "phi" in name:
        return "Phi"
    if "gemma" in name:
        return "Gemma"
    if "mistral" in name or "mixtral" in name:
        return "Mistral"
    return "其他"


def _size_tier(size_gb: float) -> str:
    """按大小分级（用于 UI 徽章）。"""
    if size_gb <= 0:
        return "unknown"
    if size_gb < 1.5:
        return "tiny"
    if size_gb < 4:
        return "small"
    if size_gb < 8:
        return "medium"
    return "large"


def get_catalog_entry(name: str) -> dict[str, Any] | None:
    """按名称获取目录条目。"""
    for entry in list_catalog():
        if entry["name"] == name:
            return entry
    return None


async def pull_from_catalog(
    name: str,
    user: User,
    replicas: int = 1,
    backend_override: str | None = None,
    custom_model_name: str | None = None,
) -> dict[str, Any]:
    """从目录拉取并部署模型。

    创建 Model + 期望数量的 ModelInstance（PENDING），由调度器接管。
    若同名 Model 已存在，返回提示而非报错。

    Returns:
        {"model_id": int, "model_name": str, "instances": int, "message": str}
    """
    entry = get_catalog_entry(name)
    if not entry:
        raise ValueError(f"目录中未找到模型: {name}")

    model_name = custom_model_name or entry["name"]

    async with session_scope() as session:
        # 检查同名
        existing = (
            await session.execute(select(Model).where(Model.name == model_name))
        ).scalar_one_or_none()
        if existing:
            return {
                "model_id": existing.id,
                "model_name": existing.name,
                "instances": 0,
                "message": f"模型 {model_name} 已存在（id={existing.id}），跳过创建",
            }

        backend_str = backend_override or entry["recommended_backend"]
        try:
            backend = ModelBackend(backend_str)
        except ValueError:
            backend = ModelBackend.LLAMA_CPP_STANDALONE

        import json
        model = Model(
            name=model_name,
            display_name=entry["display_name"],
            description=entry["description"],
            source_repo=entry["source_repo"],
            source_model_id=entry["source_model_id"],
            source_filename=entry["source_filename"],
            backend=backend,
            replicas=replicas,
            estimated_memory=entry["estimated_memory_mb"],
            required_instruction_sets=json.dumps(entry["required_instruction_sets"]),
            user_id=user.id,
            backend_parameters="{}",
        )
        session.add(model)
        await session.commit()
        await session.refresh(model)

        # 创建实例
        import secrets
        for _ in range(replicas):
            instance = ModelInstance(
                name=f"{model.name}-{secrets.token_hex(3)}",
                model_id=model.id,
                state=ModelInstanceState.PENDING,
            )
            session.add(instance)
        await session.commit()

        # 发布事件触发调度
        instances = (
            await session.execute(
                select(ModelInstance).where(ModelInstance.model_id == model.id)
            )
        ).scalars().all()
        for inst in instances:
            await ModelInstance.publish_created(inst.id, {"name": inst.name})

        logger.info(
            "从目录拉取模型 %s (id=%d)，创建 %d 个实例",
            model.name,
            model.id,
            len(instances),
        )
        return {
            "model_id": model.id,
            "model_name": model.name,
            "instances": len(instances),
            "message": f"模型 {model.name} 已创建，{len(instances)} 个实例等待调度",
        }
