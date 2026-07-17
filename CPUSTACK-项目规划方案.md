# CPUSTACK —— CPU 分布式 AI 模型部署与推理平台

## 项目规划方案

> 版本：v1.0  |  日期：2026-06-27  |  状态：规划阶段
> 参考架构：[GPUStack](https://github.com/gpustack/gpustack)（控制平面/数据平面分离）
> 目标平台：Windows 宿主 + WSL2 + Docker Linux 容器

---

## 目录

1. [项目概述与目标](#1-项目概述与目标)
2. [关键技术研究结论](#2-关键技术研究结论)
3. [技术选型建议](#3-技术选型建议)
4. [系统架构设计](#4-系统架构设计)
5. [核心模块划分](#5-核心模块划分)
6. [数据模型与状态机](#6-数据模型与状态机)
7. [API 接口体系](#7-api-接口体系)
8. [开发里程碑与阶段计划](#8-开发里程碑与阶段计划)
9. [关键技术难点与解决方案](#9-关键技术难点与解决方案)
10. [部署方案](#10-部署方案)
11. [测试与优化策略](#11-测试与优化策略)
12. [风险评估与可行性分析](#12-风险评估与可行性分析)
13. [交付清单](#13-交付清单)

---

## 1. 项目概述与目标

### 1.1 项目定位

构建一个**生产可用的 CPU 分布式 AI 模型部署与推理平台**，将局域网内多台 Windows 计算机（或异构节点）的 CPU 算力与内存资源池化，支持单机无法承载的大模型分布式部署，并提供 OpenAI 兼容的统一 API 服务和可视化管理界面。

### 1.2 核心目标

| 目标 | 描述 | 验收标准 |
|------|------|----------|
| **算力池化** | 多机 CPU+内存资源统一调度 | 2+ 节点协同运行单机无法装入的模型 |
| **模型仓库** | 浏览/选择/拉取/部署模型 | 支持 HuggingFace 拉取，进度可视化 |
| **统一 API** | OpenAI 兼容接口供局域网调用 | `/v1/chat/completions` 等端点可用 |
| **可视化管理** | 节点/模型/任务全可视 | 节点资源监控、模型生命周期管理 |
| **生产可用** | 高可用、可扩展、容错 | 节点故障自动重调度，无单点故障 |

### 1.3 与 GPUStack 的差异定位

GPUStack 面向 GPU 集群管理；CPUSTACK 面向 **CPU 资源池化**，核心差异：

| 维度 | GPUStack | CPUSTACK |
|------|----------|----------|
| 调度资源 | GPU 显存/算力 | CPU 核心/内存/指令集 |
| 推理后端 | vLLM/SGLang/llama-box | llama.cpp (RPC) + prima.cpp (流水线并行) |
| 并行策略 | 张量并行(TP)/流水线并行(PP) | **内存池化(RPC) + 流水线并行(prima) + 数据并行** |
| 硬件检测 | CUDA/ROCm 厂商识别 | AVX2/AVX-512/AMX 指令集检测 |
| 部署形态 | Linux/K8s 为主 | **Windows + WSL2 + Docker** |

---

## 2. 关键技术研究结论

> 以下结论直接来自全网最新技术资料研究，是技术选型的依据。

### 2.1 CPU 分布式推理的本质认知（关键）

**最重要的研究发现**：llama.cpp 的 RPC 模式是**内存池化方案，而非算力扩展方案**。

- **llama.cpp RPC**：跨节点分片模型权重，让超大模型装入集群内存，但**不会加速推理**（实测仅少数核心被使用）。若模型已能装入单机，添加 RPC 节点反而因网络开销变慢。
- **prima.cpp**（llama.cpp fork）：实现**流水线并行**，每节点负责模型的一段层(layers)，顺序传递激活值，**真正实现 CPU 算力横向扩展**，代价是对网络延迟敏感。
- **数据并行**：每节点完整模型副本，请求负载均衡，线性扩展并发吞吐。

**平台设计结论**：CPUSTACK 必须同时支持三种模式，按场景选择：
1. 模型超出单机内存 → llama.cpp RPC（内存池化）
2. 需要加速单请求推理 → prima.cpp（流水线并行）
3. 需要提升并发吞吐 → 数据并行（多副本负载均衡）

### 2.2 Windows 环境最优路径

- **必须使用 WSL2 + Linux 容器**，禁用 Windows 容器（镜像 4GB+，AI 生态受限，性能损耗）
- WSL2 上 llama.cpp 性能可达原生 Linux 90-100%（计算密集型），但需配置 `.wslconfig` 显式分配内存（默认仅 50%）
- 文件必须放在 WSL2 ext4 文件系统内，访问 `/mnt/c`（NTFS）慢 3-5 倍
- Docker Desktop 需配置 WSL2 后端 + Resource Saver

### 2.3 CPU 优化关键

- **GGUF Q4_K_M 量化**是 CPU 推理甜点：95-98% 质量保持，7B 模型仅 ~4.1GB
- **AVX-512/AMX 指令集**是性能分水岭：AVX-512 较 AVX2 提升 23%+，AMX（Intel 至强 SPR+）有革命性提升
- **陷阱**：AVX-512 在 Cascade Lake 上可能因频率降频反而更慢，需实测验证

### 2.4 网络要求

- 1Gbps 网络**绝对不足**用于分布式推理
- 10GbE 是起步门槛，低延迟场景需 RDMA（RoCE v2/InfiniBand）
- 流水线并行的"流水线气泡"大小直接由网络延迟决定

---

## 3. 技术选型建议

### 3.1 技术栈总览

借鉴 GPUStack 验证过的技术栈，针对 CPU 场景调整：

| 层级 | 技术 | 选型理由 |
|------|------|----------|
| **后端语言** | Python 3.11 | 与 AI 生态无缝集成，GPUStack 验证可行 |
| **Web 框架** | FastAPI + Uvicorn (ASGI) | 异步高性能，自动 OpenAPI 文档 |
| **ORM** | SQLModel + SQLAlchemy (async) | Pydantic 集成，类型安全 |
| **数据库** | PostgreSQL（内嵌） | 生产级可靠性，JSON/并发支持好 |
| **DB 迁移** | Alembic | SQLAlchemy 标准配套 |
| **调度框架** | APScheduler (AsyncIOScheduler) | 事件驱动 + 周期巡检双机制 |
| **HTTP 客户端** | httpx (async) | 异步原生，HTTP/2 支持 |
| **模型仓库 SDK** | huggingface_hub + modelscope | 双源拉取，自动择优 |
| **前端框架** | React 18 + UmiJS Max | 企业级，GPUStack UI 验证 |
| **UI 库** | Ant Design v5 + Pro Components | 组件完备，管理后台首选 |
| **状态管理** | Jotai + ahooks | 原子化状态，轻量高效 |
| **图表** | ECharts | 资源监控可视化 |
| **容器化** | Docker（Linux 容器 + WSL2） | 跨节点一致环境 |
| **编排** | docker-compose（单机） / K8s（集群可选） | 渐进式部署 |
| **推理引擎** | llama.cpp + prima.cpp | CPU 推理事实标准 |
| **进程管理** | s6-overlay（容器内） | 进程监督，自动重启 |
| **监控** | prometheus-client + Grafana | 指标采集可视化 |
| **配置** | pydantic-settings | CLI > 环境变量 > YAML |
| **认证** | argon2-cffi + PyJWT | 密码哈希 + JWT |
| **依赖管理** | uv + pyproject.toml | 极速依赖解析 |

### 3.2 推理后端选型（核心决策）

采用**可插拔后端架构**，抽象 `InferenceServer` 基类：

| 后端 | 用途 | 并行模式 | 适用场景 |
|------|------|----------|----------|
| **LlamaCppRPCServer** | 内存池化 | RPC 权重分片 | 模型超出单机内存 |
| **PrimaCppServer** | 算力扩展 | 流水线并行 | 加速单请求推理 |
| **LlamaCppStandaloneServer** | 单机推理 | 无 | 小模型单节点部署 |
| **DataParallelServer** | 并发扩展 | 数据并行 | 多副本负载均衡 |

### 3.3 为何选择 Docker 而非其他方案

| 方案 | 评价 |
|------|------|
| **Docker（推荐）** | 环境一致、隔离性好、GPUStack 同方案、WSL2 支持成熟 |
| 直接进程部署 | 环境依赖难管理，跨节点一致性差 |
| K8s | 运维复杂度过高，不适合中小规模 CPU 集群起步（可作为后续可选） |
| Windows 服务 | 无法享受 Linux AI 生态，指令集检测有缺陷 |

---

## 4. 系统架构设计

### 4.1 总体架构：控制平面/数据平面分离

```
┌─────────────────────────────────────────────────────────────────┐
│                        控制平面 (Control Plane)                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              CPUSTACK Server (单点/主备)                  │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │   │
│  │  │ FastAPI  │ │Scheduler │ │Controller│ │  EventBus  │  │   │
│  │  │  路由层   │ │ 调度器   │ │ 控制器   │ │  事件总线  │  │   │
│  │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐  │   │
│  │  │PostgreSQL│ │ModelRepo │ │ LoadBal- │ │  Auth/RBAC │  │   │
│  │  │ 元数据   │ │ 模型仓库 │ │  ancer   │ │  认证鉴权  │  │   │
│  │  └──────────┘ └──────────┘ └──────────┘ └────────────┘  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│              Token 握手 + 心跳 + gRPC/HTTP                        │
└──────────────────────────────┼───────────────────────────────────┘
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
┌────────────────┐   ┌────────────────┐   ┌────────────────┐
│  Worker Node 1 │   │  Worker Node 2 │   │  Worker Node N │
│  (数据平面)    │   │  (数据平面)    │   │  (数据平面)    │
│ ┌────────────┐ │   │ ┌────────────┐ │   │ ┌────────────┐ │
│ │ServeManager│ │   │ │ServeManager│ │   │ │ServeManager│ │
│ │WorkerStatus│ │   │ │WorkerStatus│ │   │ │WorkerStatus│ │
│ │ModelFileMgr│ │   │ │ModelFileMgr│ │   │ │ModelFileMgr│ │
│ └────────────┘ │   │ └────────────┘ │   │ └────────────┘ │
│ ┌────────────┐ │   │ ┌────────────┐ │   │ ┌────────────┐ │
│ │Docker容器  │ │   │ │Docker容器  │ │   │ │Docker容器  │ │
│ │llama-server│ │   │ │rpc-server  │ │   │ │prima-server│ │
│ └────────────┘ │   │ └────────────┘ │   │ └────────────┘ │
└────────────────┘   └────────────────┘   └────────────────┘
```

### 4.2 三大核心组件

#### 4.2.1 Server（控制平面）

- **职责**：Web UI、API 网关、调度器、元数据存储、控制器调谐循环
- **特性**：可运行在无 CPU 算力贡献的管理节点上；管理成百上千 Worker
- **核心子系统**：
  - **API 网关**：OpenAI 兼容 `/v1/*` + 管理 `/v2/*`
  - **调度器**：Filter Chain + Placement Scorer，支持 SPREAD/BINPACK
  - **控制器**：Model/Worker/Instance/Cluster 四类控制器，事件驱动调谐
  - **事件总线**：进程内 asyncio.Queue，挂钩 DB 生命周期自动发事件
  - **负载均衡器**：轮询/最少连接选择实例

#### 4.2.2 Worker（数据平面）

- **职责**：执行模型拉取、推理引擎启动、容器生命周期管理
- **5 大管理器**：
  - `WorkerManager`：注册认证、心跳同步
  - `WorkerStatusCollector`：CPU/内存/指令集/磁盘状态采集
  - `ServeManager`：推理后端进程生命周期
  - `ModelFileManager`：模型文件下载（软锁 + 进度上报）
  - `MetricExporter`：Prometheus 指标导出

#### 4.2.3 AI Gateway（网关层）

- **职责**：统一入口，OpenAI 兼容 API 路由、负载均衡、Token 计量
- **实现**：初期用 FastAPI 内置路由（简化部署），后期可引入 Higress/Envoy

### 4.3 事件驱动 + 调谐循环架构（K8s 风格）

```
用户创建模型 → DB 写入 → ActiveRecordMixin 自动发布 Model.CREATED 事件
                                    │
                                    ▼
                    ModelController._reconcile() 调谐
                    ├── sync_replicas(): 比较期望副本数 vs 实际
                    │   ├── 扩容: 创建 ModelInstance(state=PENDING)
                    │   └── 缩容: 选最佳缩容候选
                    └── 发布 ModelInstance.CREATED 事件
                                    │
                                    ▼
                    Scheduler 订阅 Instance.CREATED 事件
                    ├── _evaluate(): 资源评估(估算 CPU/内存需求)
                    ├── find_candidate(): Filter Chain 过滤
                    │   ├── ClusterFilter (集群匹配)
                    │   ├── InstructionSetFilter (AVX2/AVX-512/AMX 匹配)  ← CPU 特有
                    │   ├── LabelMatchingFilter (标签匹配)
                    │   └── StatusFilter (Worker 就绪)
                    ├── PlacementScorer 打分 (SPREAD/BINPACK)
                    └── 分配 worker_id, state=SCHEDULED
                                    │
                                    ▼
                    ModelInstanceController._reconcile()
                    ├── ensure_model_file(): 创建下载任务
                    └── state=DOWNLOADING
                                    │
                                    ▼
                    Worker.ServeManager 监听
                    ├── ModelFileManager 下载模型
                    └── 启动推理后端容器 → state=RUNNING
```

---

## 5. 核心模块划分

### 5.1 模块全景图

```
cpustack/
├── server/                      # 控制平面
│   ├── server.py                # Server 启动编排
│   ├── routes/                  # API 路由层
│   │   ├── openai.py            # OpenAI 兼容 /v1/*
│   │   ├── models.py            # 模型管理 /v2/models
│   │   ├── workers.py           # 节点管理 /v2/workers
│   │   ├── instances.py         # 实例管理 /v2/model-instances
│   │   └── auth.py              # 认证 /v2/auth
│   ├── controllers/             # 控制器(调谐循环)
│   │   ├── model_controller.py
│   │   ├── worker_controller.py
│   │   ├── instance_controller.py
│   │   └── cluster_controller.py
│   ├── scheduler/               # 调度器
│   │   ├── scheduler.py         # 调度主循环
│   │   ├── filters.py           # Filter Chain
│   │   ├── selectors.py         # 资源适配选择器
│   │   └── placement.py         # 放置打分器
│   ├── policies/                # 策略
│   │   └── utils.py             # 资源核算
│   ├── bus.py                   # 事件总线
│   ├── gateway/                 # API 网关层
│   │   ├── load_balancer.py     # 负载均衡
│   │   └── proxy.py             # 请求代理
│   └── auth/                    # 认证鉴权
│       ├── api_key.py
│       └── rbac.py
├── worker/                      # 数据平面
│   ├── worker.py                # Worker 启动编排
│   ├── worker_manager.py        # 注册/心跳
│   ├── collector.py             # 状态采集(CPU/内存/指令集)
│   ├── serve_manager.py         # 后端生命周期
│   ├── model_file_manager.py    # 模型文件管理
│   ├── exporter.py              # 指标导出
│   └── backends/                # 推理后端(可插拔)
│       ├── base.py              # InferenceServer 抽象基类
│       ├── llama_cpp_rpc.py     # RPC 内存池化
│       ├── prima_cpp.py         # 流水线并行
│       ├── llama_cpp_standalone.py  # 单机推理
│       └── data_parallel.py     # 数据并行
├── schemas/                     # 数据模型(SQLModel)
│   ├── models.py                # Model/ModelInstance
│   ├── workers.py               # Worker
│   ├── model_files.py           # ModelFile
│   └── users.py                 # User/APIKey
├── detector/                    # 硬件检测(CPU 特有)
│   ├── cpu_detector.py          # CPU 核心/频率/NUMA
│   ├── instruction_set.py       # AVX2/AVX-512/AMX 检测
│   └── memory_detector.py       # 内存/swap 检测
├── catalog/                     # 模型目录
│   └── model_catalog.yaml       # 预置模型清单
├── assets/                      # 静态资源
└── migration/                   # Alembic 迁移
```

### 5.2 核心模块职责

#### 5.2.1 硬件检测模块（CPUSTACK 特有，区别于 GPUStack）

GPUStack 检测 GPU 厂商/显存；CPUSTACK 检测 **CPU 指令集 + 内存容量**，这是调度决策的关键依据。

```
检测项:
├── CPU 架构(x86_64/aarch64)
├── 物理核/逻辑核数
├── NUMA 拓扑
├── 指令集支持: AVX2 / AVX-512 / AMX / VNNI / BF16
├── 总内存 / 可用内存 / Swap
├── 磁盘容量与 I/O
└── 网络带宽(用于分布式延迟评估)
```

#### 5.2.2 调度器（核心）

调度三阶段流水线：

1. **资源评估**：调用 `gguf-parser` 估算模型 CPU/内存需求
2. **候选选择**：Filter Chain 过滤
   - `ClusterFilter`：集群匹配
   - `InstructionSetFilter`：**指令集匹配**（AVX-512 模型只能调度到支持的节点）
   - `MemoryFitFilter`：**内存适配**（核心！模型必须能装入节点/集群内存）
   - `LabelMatchingFilter`：标签匹配
   - `StatusFilter`：Worker 就绪
3. **放置打分**：
   - **SPREAD**：跨 Worker 分散，最大化可用性
   - **BINPACK**：打包到同 Worker，腾出大内存节点

#### 5.2.3 推理后端（可插拔）

抽象基类 `InferenceServer`，工厂模式映射：

```python
_SERVER_CLASS_MAPPING = {
    "llama_cpp_rpc": LlamaCppRPCServer,        # 内存池化
    "prima_cpp": PrimaCppServer,                # 流水线并行
    "llama_cpp_standalone": LlamaCppStandaloneServer,  # 单机
    "data_parallel": DataParallelServer,        # 数据并行
}
```

后端自动选择逻辑：
```
模型内存需求 > 单机内存 → llama_cpp_rpc (内存池化)
模型内存需求 ≤ 单机内存 且 用户要求加速 → prima_cpp (流水线并行)
模型内存需求 ≤ 单机内存 且 多副本 → data_parallel
小模型单节点 → llama_cpp_standalone
```

---

## 6. 数据模型与状态机

### 6.1 核心实体关系

```
User ──┬── APIKey
       └── Model ──┬── ModelInstance ──┬── ModelFile
                   │                   └── 分配到 Worker
                   └── ModelRoute (路由规则)

Worker ──┬── WorkerStatus (心跳上报)
         └── 承载 ModelInstance
```

### 6.2 Worker 资源模型（CPU 特有）

```python
class WorkerStatus:
    cpu_cores: int                    # 逻辑核数
    cpu_allocated: int                # 已分配核数
    memory_total: int                 # 总内存(GB)
    memory_allocated: int             # 已分配内存
    memory_available: int             # 可用内存
    instruction_sets: List[str]       # ["AVX2","AVX-512","AMX"]
    numa_nodes: int                   # NUMA 节点数
    disk_total: int
    disk_available: int
    network_bandwidth: int            # 网络带宽(Mbps),分布式评估用
```

### 6.3 ModelInstance 状态机（9 态，借鉴 GPUStack）

```
PENDING ──→ ANALYZING ──→ SCHEDULED ──→ INITIALIZING ──→ DOWNLOADING ──→ STARTING ──→ RUNNING
   │            │             │               │                │              │
   └────────────┴─────────────┴───────────────┴────────────────┴──────────────┴──→ ERROR
                                                                              │
                                                                     UNREACHABLE (Worker 离线)
```

| 状态 | 责任组件 | 触发 |
|------|----------|------|
| PENDING | — | 创建初始态 |
| ANALYZING | Scheduler | 资源评估开始 |
| SCHEDULED | Scheduler | Worker/资源已分配 |
| INITIALIZING | ServeManager | Worker 收到任务 |
| DOWNLOADING | ModelInstanceController | 模型文件下载中 |
| STARTING | ServeManager | 推理后端启动中 |
| RUNNING | ServeManager | 服务请求中 |
| ERROR | 多个 | 失败,可重启 |
| UNREACHABLE | WorkerController | Worker 停止心跳 |

---

## 7. API 接口体系

### 7.1 OpenAI 兼容 API（`/v1`）

供局域网应用程序调用，零改造接入：

| 端点 | 方法 | 用途 |
|------|------|------|
| `/v1/models` | GET | 列出可用模型 |
| `/v1/chat/completions` | POST | 对话补全（流式/非流式） |
| `/v1/completions` | POST | 文本补全 |
| `/v1/embeddings` | POST | 文本向量化 |

**请求流（7 阶段）**：认证 → 模型名提取 → 访问控制 → 模型解析 → 实例选择(负载均衡) → 代理转发(到 Worker `/proxy`) → 流式响应处理。

### 7.2 管理 API（`/v2`）

| 资源 | 端点 | 功能 |
|------|------|------|
| 模型 | `/v2/models` | CRUD + `?watch=true` SSE 订阅 |
| 节点 | `/v2/workers` | 查询/标签管理 |
| 实例 | `/v2/model-instances` | 查询/启停/重启 |
| 模型目录 | `/v2/model-sets` | 浏览可部署模型 |
| 用户 | `/v2/users` | 用户管理 |
| API Key | `/v2/api-keys` | 密钥管理 |
| 系统设置 | `/v2/system-settings` | 集群配置 |

### 7.3 鉴权

- **API Key**：`sk-xxx` 格式，关联用户与模型白名单
- **JWT Bearer Token**：管理界面登录
- **模型访问控制**：Admin 全权限；普通用户限 `MyModel`

---

## 8. 开发里程碑与阶段计划

### 阶段 0：基础设施搭建（M0）

**目标**：建立开发环境与项目骨架

- [ ] 初始化项目结构（前后端分离）
- [ ] 搭建 FastAPI + SQLModel + PostgreSQL 骨架
- [ ] 搭建 React + UmiJS + Ant Design 前端骨架
- [ ] 编写 Dockerfile + docker-compose.yml
- [ ] 配置 WSL2 开发环境（.wslconfig 资源分配）
- [ ] Alembic 数据库迁移初始化
- [ ] CI 流程（lint + 测试）

**交付**：可启动的空壳系统（登录页 + 健康检查）

### 阶段 1：单机推理 MVP（M1）

**目标**：单节点上部署一个模型并通过 API 调用

- [ ] 硬件检测模块（CPU/内存/指令集）
- [ ] Worker 注册 + 心跳机制
- [ ] 模型文件下载管理（HuggingFace 拉取 + 进度上报）
- [ ] `LlamaCppStandaloneServer` 后端实现
- [ ] OpenAI 兼容 `/v1/chat/completions` 端点
- [ ] 基础调度器（单节点资源适配）
- [ ] 前端：节点列表 + 模型部署页 + Playground

**交付**：可在单节点部署 Llama 3.2 3B 并对话

### 阶段 2：分布式推理（M2，核心难点）

**目标**：多节点协同运行单机无法装入的模型

- [ ] `LlamaCppRPCServer` 后端（内存池化）
- [ ] 多节点调度器（MemoryFitFilter + 跨节点资源核算）
- [ ] RPC 主从节点协调（主节点 + rpc-server 工作节点）
- [ ] 模型文件多节点分发
- [ ] 前端：分布式拓扑可视化 + 节点资源聚合视图

**交付**：2 节点 RPC 模式运行 7B 模型

### 阶段 3：算力扩展（M3）

**目标**：流水线并行加速推理

- [ ] `PrimaCppServer` 后端（流水线并行）
- [ ] 层切片分配策略
- [ ] 网络延迟评估与节点选择
- [ ] `DataParallelServer` 后端（多副本负载均衡）
- [ ] 负载均衡器（轮询/最少连接）

**交付**：prima.cpp 流水线并行 + 数据并行多副本

### 阶段 4：生产化（M4）

**目标**：高可用、可扩展、容错

- [ ] 控制器调谐循环（自动重调度、缩容）
- [ ] Worker 故障检测 + 实例迁移
- [ ] 软文件锁（并发下载保护）
- [ ] Prometheus 指标 + Grafana 面板
- [ ] 多租户 + RBAC
- [ ] 日志聚合
- [ ] 配置中心（CLI > 环境变量 > YAML）

**交付**：节点故障自动恢复，生产可用

### 阶段 5：测试与优化（M5）

- [ ] 选取 ≤5GB 模型部署测试（Llama 3.2 3B / Phi-4 Mini / Qwen 2.5 3B / Qwen 2.5 7B / Gemma 3 4B）
- [ ] 分布式推理性能基准（吞吐/延迟/资源利用率）
- [ ] 算法优化（批处理、投机解码、KV Cache 共享）
- [ ] 资源调度优化
- [ ] 测试报告 + 性能优化文档

**交付**：测试报告 + 优化方案

### 里程碑甘特图（建议节奏）

```
M0 基础设施   ████████
M1 单机 MVP            ████████████
M2 分布式推理                        ██████████████
M3 算力扩展                                          ██████████
M4 生产化                                                      ██████████████
M5 测试优化                                                                  ██████████
```

---

## 9. 关键技术难点与解决方案

### 难点 1：CPU 分布式推理的"内存池化 vs 算力扩展"误区

**问题**：用户期望"多机 CPU 算力共享加速推理"，但 llama.cpp RPC 实际只是内存池化，不加速。

**解决方案**：
- 平台明确区分三种模式，UI 上向用户清晰说明各模式适用场景
- 模型超出单机内存 → 自动选择 RPC 模式（能跑起来 > 不能跑）
- 用户要求加速 → 提供 prima.cpp 流水线并行选项
- 文档明确告知：RPC 模式不会加速，仅解决内存不足
- 提供基准测试工具，让用户实测各模式性能

### 难点 2：Windows 环境的 Docker 资源限制

**问题**：WSL2 默认仅分配 50% 内存，导致大模型 OOM；文件跨边界访问慢 3-5 倍。

**解决方案**：
- 提供 `.wslconfig` 配置模板，指导用户显式分配资源
- 模型文件必须存储在 WSL2 ext4 文件系统内（非 `/mnt/c`）
- 容器挂载卷指向 WSL2 内部路径
- Docker Desktop 启用 Resource Saver
- 安装文档包含完整的 WSL2 调优指南

### 难点 3：CPU 指令集异构调度

**问题**：集群中节点 CPU 指令集不同（AVX2 vs AVX-512 vs AMX），AVX-512 优化的模型不能调度到仅支持 AVX2 的节点。

**解决方案**：
- `InstructionSetFilter` 调度过滤器，匹配模型要求与节点能力
- 模型目录标注指令集要求
- 编译多版本 llama.cpp 二进制（AVX2 / AVX-512 / AMX），按节点能力选择
- AVX-512 降频陷阱：提供基准测试，对 Cascade Lake 节点自动回退 AVX2

### 难点 4：流水线并行的网络延迟敏感性

**问题**：prima.cpp 流水线并行的"气泡"大小由网络延迟决定，1Gbps 网络性能崩溃。

**解决方案**：
- 部署前网络带宽检测，低于 10Gbps 警告
- 调度器考虑网络拓扑，优先选择低延迟节点组合
- 支持节点分组（高带宽组优先用于流水线并行）
- 文档明确网络要求，提供网络调优指南

### 难点 5：模型文件多节点并发下载冲突

**问题**：多 Worker 同时下载同一模型文件导致损坏。

**解决方案**（借鉴 GPUStack）：
- `HeartbeatSoftFileLock` 软文件锁
- 进度上报回写 DB 的 `download_progress` 字段
- 状态机：DOWNLOADING → READY / ERROR（可重置重试）

### 难点 6：跨节点内存资源核算

**问题**：分布式模式下需聚合多节点内存判断是否可部署。

**解决方案**：
- `get_worker_allocatable_resource` = 总容量 − 已分配 − system_reserved
- RPC 模式：主节点 + 工作节点内存总和 ≥ 模型需求
- 流水线并行：每节点内存 ≥ 分配到的层的需求
- 调度器实现 `MemoryFitSelector`，按并行模式差异化核算

### 难点 7：高可用与容错

**问题**：Worker 故障导致实例不可用；Server 单点故障。

**解决方案**：
- Worker 心跳过期 → 标记 UNREACHABLE → `WorkerInstanceCleaner` 清理实例
- 控制器自动重调度：故障实例重新进入 PENDING → 重新调度
- Server 高可用：初期单点 + 数据库持久化；后期支持主备（PostgreSQL 流复制）
- 模型文件多副本存储，避免单节点故障导致文件丢失

---

## 10. 部署方案

### 10.1 推荐部署形态

```
Windows 宿主机
├── WSL2 (Ubuntu 24.04)
│   ├── Docker Desktop (WSL2 后端)
│   │   ├── cpustack-server 容器
│   │   ├── cpustack-worker 容器 (本机)
│   │   └── postgres 容器
│   └── 模型文件存储 (/var/lib/cpustack/cache, ext4)
└── 其他 Windows 机器
    └── WSL2 + Docker
        └── cpustack-worker 容器 (远程)
```

### 10.2 docker-compose.yml 结构（示例）

```yaml
version: "3.9"
services:
  cpustack-server:
    image: cpustack/server:latest
    ports:
      - "80:80"        # Web UI + API
    environment:
      - CPUSTACK_DB_URL=postgresql://cpustack:secret@postgres:5432/cpustack
    volumes:
      - cpustack-data:/var/lib/cpustack
    depends_on:
      - postgres

  postgres:
    image: postgres:16
    environment:
      - POSTGRES_DB=cpustack
      - POSTGRES_USER=cpustack
      - POSTGRES_PASSWORD=secret
    volumes:
      - pg-data:/var/lib/postgresql/data

  cpustack-worker:
    image: cpustack/worker:latest
    environment:
      - CPUSTACK_SERVER_URL=http://cpustack-server:80
      - CPUSTACK_TOKEN=${WORKER_TOKEN}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # 管理推理容器
      - model-cache:/var/lib/cpustack/cache

volumes:
  cpustack-data:
  pg-data:
  model-cache:
```

### 10.3 WSL2 配置模板（`.wslconfig`）

```ini
[wsl2]
memory=24GB          # 限制 WSL2 最大内存(留 4-8GB 给 Windows)
swap=8GB             # 防止模型加载 OOM
processors=8         # CPU 核心数
localhostForwarding=true
networkingMode=mirrored  # Win11 22H2+,服务可被 LAN 访问

[experimental]
autoMemoryReclaim=dropcache
sparseVhd=true
```

---

## 11. 测试与优化策略

### 11.1 测试模型选取（≤5GB，GGUF Q4_K_M）

| 模型 | 参数量 | 量化大小 | RAM 占用 | 测试用途 |
|------|--------|----------|----------|----------|
| Llama 3.2 1B | 1B | ~0.9 GB | ~1.5 GB | 单机极速基准 |
| Llama 3.2 3B | 3B | ~2.5 GB | ~3.5 GB | 综合入门 / 分布式验证 |
| Phi-4 Mini | 3.8B | ~2.5 GB | ~3.5 GB | 推理/编程场景 |
| Qwen 2.5 3B | 3B | ~2.0 GB | ~3 GB | 多语言 |
| Qwen 2.5 7B | 7B | ~4.7 GB | ~5.2 GB | 接近 5GB 上限 / RPC 测试 |
| Gemma 3 4B | 4B | ~2.8 GB | ~4 GB | 移动端优化对比 |

### 11.2 测试维度

1. **单机推理性能**：吞吐(tok/s)、TTFT(ms)、峰值 RAM
2. **分布式 RPC 性能**：2/3 节点内存池化，对比单机不可部署→可部署
3. **流水线并行性能**：prima.cpp 多节点加速比 vs 网络延迟
4. **数据并行性能**：多副本并发吞吐线性扩展验证
5. **资源利用率**：CPU 利用率、内存占用、网络带宽
6. **故障恢复**：节点宕机后重调度时延
7. **长稳测试**：72 小时持续负载稳定性

### 11.3 优化方向

| 优化项 | 方法 | 预期收益 |
|--------|------|----------|
| 批处理 | 连续批处理(continuous batching) | 吞吐 2-5× |
| 投机解码 | 小模型草稿 + 大模型验证 | 延迟降 2-3× |
| KV Cache 共享 | 同节点进程共享 /dev/shm | 降显存/内存 |
| 指令集优化 | 按节点能力选 AVX-512/AMX 内核 | 23%+ |
| 量化优化 | GGUF Q4_K_M 甜点 | 内存降 50%+ |
| 前缀缓存 | 跨请求 KV Cache 复用 | 降 TTFT |

---

## 12. 风险评估与可行性分析

### 12.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| prima.cpp 流水线并行在网络差时性能崩塌 | 高 | 高 | 网络检测预警，优先用 RPC/数据并行 |
| WSL2 内存限制导致大模型 OOM | 中 | 高 | .wslconfig 配置指南 + 内存预留 |
| AVX-512 降频陷阱 | 中 | 中 | 基准测试 + 自动回退 AVX2 |
| llama.cpp 版本迭代导致兼容性问题 | 中 | 中 | 锁定版本 + 兼容性测试 |
| 多节点模型文件同步延迟 | 中 | 中 | 软锁 + 预下载机制 |

### 12.2 可行性结论

| 维度 | 评估 | 依据 |
|------|------|------|
| **技术可行性** | ✅ 高 | GPUStack 已验证架构；llama.cpp/prima.cpp 技术成熟 |
| **Windows 可行性** | ✅ 高 | WSL2 + Docker 方案成熟，性能达原生 90%+ |
| **分布式可行性** | ⚠️ 中高 | RPC 内存池化成熟；流水线并行对网络要求高 |
| **生产可行性** | ✅ 中高 | 需完善容错/监控，架构设计已考虑 |

### 12.3 核心可行性保障

1. **借鉴 GPUStack 验证过的架构**：控制平面/数据平面分离、事件驱动调谐、可插拔后端、OpenAI 兼容 API——这些设计已在大规模生产环境验证
2. **技术栈成熟**：FastAPI/React/PostgreSQL/llama.cpp 均为生产级技术
3. **渐进式开发**：MVP→分布式→算力扩展→生产化，每阶段可独立交付价值
4. **明确技术边界**：清楚认知 RPC vs 流水线并行的差异，避免错误承诺

---

## 13. 交付清单

### 13.1 软件交付

- [ ] CPUSTACK Server（控制平面，Docker 镜像）
- [ ] CPUSTACK Worker（数据平面，Docker 镜像）
- [ ] CPUSTACK UI（管理界面，React 前端）
- [ ] docker-compose.yml（单机部署）
- [ ] K8s Helm Chart（集群部署，可选）

### 13.2 文档交付

- [ ] 架构设计文档（本文档持续更新）
- [ ] API 文档（FastAPI 自动生成 OpenAPI）
- [ ] 部署指南（WSL2 + Docker 配置）
- [ ] 使用手册（模型部署/调用教程）
- [ ] 测试报告（性能基准 + 优化分析）

### 13.3 源代码

- [ ] 后端源码（Python，含单元测试）
- [ ] 前端源码（TypeScript）
- [ ] Dockerfile + 配置文件
- [ ] 数据库迁移脚本

---

## 附录：关键技术参考

- **GPUStack 架构**：https://github.com/gpustack/gpustack
- **llama.cpp RPC**：内存池化分布式推理
- **prima.cpp**：流水线并行 CPU 算力扩展
- **GGUF 量化**：CPU 推理首选格式，Q4_K_M 甜点
- **WSL2**：Windows 上 Linux 容器最优路径
- **AVX-512/AMX**：CPU 推理性能分水岭

---

> **下一步**：本规划方案确认后，将从阶段 0（基础设施搭建）开始实施开发。
