"""模型、模型实例、模型文件模型。

这是平台的核心数据模型，定义了模型部署的完整生命周期。
"""

from __future__ import annotations

from enum import Enum

from sqlmodel import Field

from cpustack.schemas.common import ActiveRecordMixin, TimestampMixin


class ModelBackend(str, Enum):
    """推理后端类型。"""

    LLAMA_CPP_STANDALONE = "llama_cpp_standalone"  # 单机推理
    LLAMA_CPP_RPC = "llama_cpp_rpc"  # RPC 内存池化
    PRIMA_CPP = "prima_cpp"  # 流水线并行
    DATA_PARALLEL = "data_parallel"  # 数据并行


class ModelInstanceState(str, Enum):
    """模型实例状态机（9 态，借鉴 GPUStack）。"""

    PENDING = "pending"  # 创建初始态
    ANALYZING = "analyzing"  # 调度器资源评估中
    SCHEDULED = "scheduled"  # Worker/资源已分配
    INITIALIZING = "initializing"  # Worker 收到任务
    DOWNLOADING = "downloading"  # 模型文件下载中
    STARTING = "starting"  # 推理后端启动中
    RUNNING = "running"  # 服务请求中
    ERROR = "error"  # 失败，可重启
    UNREACHABLE = "unreachable"  # Worker 停止心跳


class ModelFileState(str, Enum):
    """模型文件状态。"""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    READY = "ready"
    ERROR = "error"


class Model(TimestampMixin, ActiveRecordMixin, table=True):
    """模型定义（高层抽象，如 "Llama-3.2-3B"）。"""

    __tablename__ = "models"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True, nullable=False, max_length=128)
    display_name: str = Field(default="", nullable=False, max_length=256)
    description: str = Field(default="", nullable=False, max_length=2048)

    # 模型来源
    source_repo: str = Field(nullable=False, max_length=256)  # huggingface/modelscope
    source_model_id: str = Field(nullable=False, max_length=256)  # 如 "meta-llama/Llama-3.2-3B"
    source_filename: str = Field(default="", nullable=False, max_length=256)  # GGUF 文件名

    # 推理配置
    backend: ModelBackend = Field(
        default=ModelBackend.LLAMA_CPP_STANDALONE, nullable=False
    )
    replicas: int = Field(default=1, nullable=False)  # 期望副本数

    # 资源估算（MB）
    estimated_memory: int = Field(default=0, nullable=False)
    # 指令集要求（JSON 数组字符串）
    required_instruction_sets: str = Field(default="[]", nullable=False, max_length=256)

    # 用户
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)

    # 运行参数（JSON 字符串）
    backend_parameters: str = Field(default="{}", nullable=False, max_length=4096)


class ModelInstance(TimestampMixin, ActiveRecordMixin, table=True):
    """模型实例（具体运行进程）。"""

    __tablename__ = "model_instances"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(nullable=False, index=True, max_length=128)  # {model.name}-{5字符随机}
    model_id: int = Field(foreign_key="models.id", nullable=False, index=True)
    worker_id: int | None = Field(default=None, foreign_key="workers.id", nullable=True, index=True)

    state: ModelInstanceState = Field(
        default=ModelInstanceState.PENDING, nullable=False, index=True
    )

    # 分配的资源
    allocated_cpu_cores: int = Field(default=0, nullable=False)
    allocated_memory: int = Field(default=0, nullable=False)  # MB

    # RPC/分布式相关：从属 Worker 列表（JSON 数组字符串）
    rpc_worker_ids: str = Field(default="[]", nullable=False, max_length=512)

    # 分布式配置（JSON 字符串，存储流水线层分配、数据并行副本映射等）
    # 流水线并行: {"pipeline": [{"worker_id":1,"layer_start":0,"layer_end":15}, ...]}
    # 数据并行: {"replica_of": <parent_instance_id>} （子副本指向父实例）
    distributed_config: str = Field(default="{}", nullable=False, max_length=2048)

    # 服务端口
    service_port: int | None = Field(default=None, nullable=True)

    # 后端版本
    backend_version: str = Field(default="", nullable=False, max_length=64)

    # 错误信息
    error_message: str = Field(default="", nullable=False, max_length=2048)

    # 下载进度
    download_progress: float = Field(default=0.0, nullable=False)


class ModelFile(TimestampMixin, ActiveRecordMixin, table=True):
    """模型文件（下载状态跟踪）。"""

    __tablename__ = "model_files"

    id: int | None = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="models.id", nullable=False, index=True)
    worker_id: int = Field(foreign_key="workers.id", nullable=False, index=True)

    # 文件信息
    filename: str = Field(nullable=False, max_length=256)
    file_path: str = Field(nullable=False, max_length=512)
    file_size: int = Field(default=0, nullable=False)  # 字节
    sha256: str = Field(default="", nullable=False, max_length=64)

    state: ModelFileState = Field(default=ModelFileState.PENDING, nullable=False, index=True)
    download_progress: float = Field(default=0.0, nullable=False)
    download_url: str = Field(default="", nullable=False, max_length=1024)

    error_message: str = Field(default="", nullable=False, max_length=2048)
