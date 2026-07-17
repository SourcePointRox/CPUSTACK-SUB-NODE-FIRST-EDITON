"""全局配置：CLI > 环境变量(CPUSTACK_前缀) > YAML > 默认值。

YAML 配置文件路径由 CPUSTACK_CONFIG_FILE 环境变量指定，默认 config.yaml。
环境变量优先于 YAML（显式设置的环境变量覆盖 YAML 同名字段）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _load_yaml_config() -> dict:
    """加载 YAML 配置文件，仅保留未被环境变量显式覆盖的字段。

    优先级：环境变量 > YAML > 默认值。
    """
    config_file = os.environ.get("CPUSTACK_CONFIG_FILE", "config.yaml")
    path = Path(config_file)
    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.exception("加载 YAML 配置文件 %s 失败", path)
        return {}

    # 仅保留未被环境变量显式设置的 key（env 优先于 yaml）
    result: dict = {}
    for key, value in data.items():
        env_key = f"CPUSTACK_{key.upper()}"
        if env_key not in os.environ:
            result[key] = value
    logger.debug("从 %s 加载 %d 个配置项", path, len(result))
    return result


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CPUSTACK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 基本配置
    debug: bool = False
    data_dir: str = "/var/lib/cpustack"
    log_level: str = "INFO"

    # 服务端配置
    host: str = "0.0.0.0"
    port: int = 80
    secret_key: str = "change-me-in-production-with-32+bytes-key"

    # 数据库配置
    db_url: str = "postgresql+asyncpg://cpustack:cpustack@localhost:5432/cpustack"
    db_url_sync: str = "postgresql+psycopg2://cpustack:cpustack@localhost:5432/cpustack"

    # Worker 配置
    server_url: str = "http://localhost:80"
    worker_token: str = ""
    worker_name: str = ""
    worker_port: int = 30080

    # 调度配置
    scheduler_interval_seconds: int = 180
    worker_heartbeat_timeout_seconds: int = 120

    # 模型存储
    model_cache_dir: str = "/var/lib/cpustack/cache"
    huggingface_mirror: str = "https://hf-mirror.com"

    # 服务端口范围
    service_port_range_start: int = 40000
    service_port_range_end: int = 41000

    # 局域网子节点发现（UDP 广播）
    discovery_port: int = 30090
    discovery_scan_timeout: int = 5

    # 知识库存储
    knowledge_base_dir: str = "/var/lib/cpustack/kb"

    # JWT 配置
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24

    # 阶段4 生产化配置
    # CORS 允许的源（生产环境应收紧为具体域名，如 ["https://cpustack.example.com"]）
    cors_origins: list[str] = ["*"]
    # Prometheus 指标导出开关
    metrics_enabled: bool = True
    # 配置 profile（dev/staging/prod，仅用于日志标识，不影响实际值）
    profile: str = "dev"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def model_cache_path(self) -> Path:
        return Path(self.model_cache_dir)

    @property
    def knowledge_base_path(self) -> Path:
        return Path(self.knowledge_base_dir)


# 加载顺序：环境变量 > YAML > 默认值（YAML 仅填充 env 未设置的字段）
settings = Settings(**_load_yaml_config())
