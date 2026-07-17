# CPUSTACK

> CPU 分布式 AI 模型部署与推理平台

CPUSTACK 是一个受 [GPUStack](https://github.com/gpustack/gpustack) 启发的开源项目，专注于 **CPU 集群**上的大语言模型（LLM）部署与推理。它将局域网内任意一台普通 PC（工作站、旧服务器、办公电脑）聚合为统一的推理资源池，通过 [llama.cpp](https://github.com/ggerganov/llama.cpp) 提供 OpenAI 兼容 API。

## 核心特性

- **分布式 CPU 推理**：4 种后端适配不同场景
  - `llama_cpp_standalone`：单机推理（小模型）
  - `llama_cpp_rpc`：RPC 内存池化，跨节点聚合内存运行大模型
  - `prima_cpp`：流水线并行，按层切片到多节点加速
  - `data_parallel`：数据并行，多副本提升吞吐
- **局域网节点自动发现**：UDP 广播扫描子节点，一键生成注册命令
- **OpenAI 兼容 API**：`/v1/chat/completions`、`/v1/completions`、`/v1/models`
- **Token 用量计量**：按模型 / 用户 / API Key 维度统计，支持流式与非流式
- **本地知识库**：文档切分 + BM25 关键词检索（纯 Python，开箱即用）
- **模型目录**：预置 YAML 目录，一键拉取并部署（支持 HuggingFace 国内镜像）
- **调度策略**：SPREAD / BINPACK、指令集优先级（AVX-512 > AVX2）、网络感知
- **生产化**：JWT + API Key 双认证、RBAC、Prometheus 指标、自动重启与故障迁移

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     主节点 (Master / Server)                  │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │  FastAPI    │  │  调度器       │  │  控制器             │  │
│  │  Web UI/API │  │  (Filter+     │  │  (Worker/Model/     │  │
│  │  /v1 /v2    │  │   Placement)  │  │   Instance)        │  │
│  └─────────────┘  └──────────────┘  └────────────────────┘  │
│         │ UDP 广播发现            ▲ 心跳/状态上报             │
└─────────┼─────────────────────────┼─────────────────────────┘
          │                         │
   ┌──────┴──────┐           ┌──────┴──────┐
   ▼             ▼           ▼             ▼
┌──────┐    ┌──────┐    ┌──────┐    ┌──────┐
│子节点1│    │子节点2│    │子节点3│    │子节点N│
│Worker│    │Worker│    │Worker│    │Worker│
│推理   │    │推理   │    │推理   │    │推理   │
└──────┘    └──────┘    └──────┘    └──────┘
```

- **主节点（Server）**：控制平面，提供 Web UI 与 API，负责调度、控制器调谐、负载均衡
- **子节点（Worker）**：数据平面，运行推理后端进程，向主节点注册并周期上报资源状态

## 环境要求

- **Python** ≥ 3.11
- **llama.cpp** 二进制（`llama-server`、`rpc-server`）—— 仅在实际推理时需要，下载模型与启动 UI 不需要
- **操作系统**：Windows / Linux / macOS（本仓库以 Windows 为主，提供 `start.bat`）

### Python 依赖安装

```bash
cd cpustack/backend
pip install -e .
# 或使用国内镜像加速
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> SQLite 本地开发模式需要额外安装 `aiosqlite`：`pip install aiosqlite`

## 快速开始

### 一、主节点启动

主节点是整个集群的控制中心，提供 Web 管理界面与 OpenAI 兼容 API。

#### 方式 A：Windows 本地一键启动（推荐入门）

仓库内置 `start.bat`，使用 SQLite + 内嵌 Worker 单机模式，免 PostgreSQL：

```bat
cd cpustack\backend
start.bat
```

该脚本会：
1. 读取 `backend\.env` 配置（SQLite 数据库、8080 端口、国内 HF 镜像）
2. 初始化数据库表
3. 启动 Server + 内嵌 Worker（`cpustack both` 命令）

启动后访问 **http://127.0.0.1:8080**，默认管理员账号：
- 用户名：`admin`
- 密码：`cpustack`

#### 方式 B：命令行启动（仅 Server，不含 Worker）

适合主节点不参与推理、仅做调度的部署：

```bash
cd cpustack/backend

# 1. 初始化数据库
python init_db.py

# 2. 启动 Server
python -m cpustack.cli serve --host 0.0.0.0 --port 8080
```

#### 方式 C：Docker Compose 启动（生产环境，PostgreSQL）

```bash
cd cpustack
docker compose up -d
```

将启动 PostgreSQL + Server + 本机 Worker，详见 `docker-compose.yml`。

### 二、子节点启动

子节点是执行推理的工作节点。子节点**主动注册**到主节点，需保证与主节点网络可达，且 `WORKER_TOKEN` 与主节点一致。

#### 准备配置

在子节点机器上，确保已安装 Python 依赖（同主节点），并配置以下环境变量（可通过 `.env` 文件或命令行设置）：

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `CPUSTACK_SERVER_URL` | 主节点地址 | `http://192.168.1.100:8080` |
| `CPUSTACK_WORKER_TOKEN` | 集群共享密钥（须与主节点一致） | `cpustack-cluster-token` |
| `CPUSTACK_WORKER_NAME` | 节点名称（留空则用主机名） | `node-02` |
| `CPUSTACK_WORKER_PORT` | 节点通信端口 | `30080` |

#### 启动命令

**Windows（CMD）：**
```bat
set CPUSTACK_SERVER_URL=http://192.168.1.100:8080
set CPUSTACK_WORKER_TOKEN=cpustack-cluster-token
set CPUSTACK_WORKER_NAME=node-02
python -m cpustack.cli worker
```

**Windows（PowerShell）：**
```powershell
$env:CPUSTACK_SERVER_URL="http://192.168.1.100:8080"
$env:CPUSTACK_WORKER_TOKEN="cpustack-cluster-token"
$env:CPUSTACK_WORKER_NAME="node-02"
python -m cpustack.cli worker
```

**Linux / macOS：**
```bash
export CPUSTACK_SERVER_URL=http://192.168.1.100:8080
export CPUSTACK_WORKER_TOKEN=cpustack-cluster-token
export CPUSTACK_WORKER_NAME=node-02
python -m cpustack.cli worker
```

启动后子节点会：
1. 向主节点 `/v2/worker-registration` 注册（Token 握手）
2. 获取 `worker_uuid` + `api_key` 并持久化到本地（重启免重复注册）
3. 周期上报 CPU、内存、指令集等资源状态（心跳）
4. 监听 UDP 30090 端口，响应主节点的局域网扫描
5. 轮询主节点领取推理任务并启动推理后端

#### 通过 Web UI 一键发现并注册子节点

主节点 Web 界面的「节点」页面提供「扫描局域网」按钮：

1. 点击「扫描局域网」，主节点向 UDP 广播地址发送探测包
2. 所有运行中的子节点会响应，显示在列表中（含 IP、CPU、内存）
3. 点击「注册」即可生成对应子节点的启动命令，复制到目标子节点执行

### 三、部署模型并调用

1. 登录 Web UI → 「模型」页面 → 浏览预置目录 → 一键拉取
2. 模型文件会从 HuggingFace 国内镜像下载到 `data/cache/`（**首次需要联网，模型文件不随仓库分发**）
3. 调度器自动分配实例到可用节点，状态变为 `running` 后即可调用

调用示例（OpenAI 兼容 API）：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <你的-API-Key-或-JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-3b",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

API Key 在 Web UI「API 密钥」页面创建（`sk-` 前缀）。

## 配置说明

所有配置通过环境变量（前缀 `CPUSTACK_`）或 YAML 文件管理，优先级：**环境变量 > YAML > 默认值**。完整示例见 [`cpustack/.env.example`](cpustack/.env.example)。

关键配置项：

| 配置项 | 默认值 | 说明 |
|-------|--------|------|
| `CPUSTACK_PORT` | 80 | 主节点服务端口（本地开发建议 8080） |
| `CPUSTACK_DB_URL` | PostgreSQL | 数据库连接串（本地可用 SQLite） |
| `CPUSTACK_WORKER_TOKEN` | `cpustack-cluster-token` | 集群共享密钥 |
| `CPUSTACK_HUGGINGFACE_MIRROR` | `https://hf-mirror.com` | HF 国内镜像 |
| `CPUSTACK_DISCOVERY_PORT` | 30090 | 局域网发现 UDP 端口 |
| `CPUSTACK_SERVICE_PORT_RANGE_START/END` | 40000-41000 | 推理后端端口范围 |
| `CPUSTACK_CORS_ORIGINS` | `["*"]` | CORS 白名单（生产应收紧） |

### 端口约定

| 端口 | 用途 |
|------|------|
| 8080（或 80） | 主节点 Web UI + API |
| 30080 | 子节点通信端口 |
| 30090 | 局域网发现 UDP |
| 40000-41000 | 推理后端服务端口 |
| 50000 + worker_id | RPC / 流水线节点间通信 |

## API 概览

| 路径 | 说明 |
|------|------|
| `GET /healthz` `GET /readyz` | 健康与就绪检查 |
| `POST /v2/auth/login` | 登录获取 JWT |
| `GET /v2/dashboard` | 概览统计 |
| `GET /v2/workers` | 节点列表 |
| `GET /v2/models` `POST /v2/models` | 模型 CRUD |
| `GET /v2/models/catalog` `POST /v2/models/pull` | 模型目录与一键拉取 |
| `GET /v2/models/instances` | 模型实例列表 |
| `GET /v2/discovery/scan` | 扫描局域网子节点 |
| `GET /v2/tokens/summary` `GET /v2/tokens/total` | Token 用量统计 |
| `GET/POST /v2/knowledge-bases` | 知识库管理 |
| `POST /v2/knowledge-bases/{id}/search` | 知识库检索 |
| `POST /v1/chat/completions` | OpenAI 兼容对话 |
| `POST /v1/completions` | OpenAI 兼容补全 |
| `GET /metrics` | Prometheus 指标 |

交互式 API 文档：`http://<主节点>/docs`

## 项目结构

```
CPUSTACK-设计/
├── cpustack/
│   ├── backend/                # 后端（Python / FastAPI）
│   │   ├── cpustack/
│   │   │   ├── cli.py          # CLI 入口：serve / worker / both
│   │   │   ├── config.py       # 配置管理
│   │   │   ├── bus.py          # 事件总线
│   │   │   ├── db.py           # 数据库引擎
│   │   │   ├── catalog/        # 模型目录（YAML）
│   │   │   ├── detector/       # 硬件检测（CPU/内存/指令集）
│   │   │   ├── schemas/        # 数据模型（SQLModel）
│   │   │   ├── server/         # 控制平面
│   │   │   │   ├── app.py      # FastAPI 应用
│   │   │   │   ├── routes/     # 路由（auth/workers/models/openai/...）
│   │   │   │   ├── scheduler/  # 调度器（Filter + Placement）
│   │   │   │   ├── controllers/# 控制器（Worker/Model/Instance 调谐）
│   │   │   │   ├── gateway/    # 负载均衡
│   │   │   │   ├── token_service.py    # Token 计量
│   │   │   │   └── knowledge_service.py # 知识库 BM25 检索
│   │   │   └── worker/         # 数据平面
│   │   │       ├── worker.py   # Worker 编排
│   │   │       ├── worker_manager.py    # 注册与心跳
│   │   │       ├── serve_manager.py     # 推理进程生命周期
│   │   │       ├── downloader.py        # 模型下载
│   │   │       ├── discovery_listener.py# 局域网发现监听
│   │   │       └── backends/   # 推理后端（llama.cpp / RPC / 流水线）
│   │   ├── data/ui/            # 前端构建产物（由 frontend 构建后拷贝）
│   │   ├── .env                # 本地开发配置（不入库）
│   │   ├── init_db.py          # 数据库初始化脚本
│   │   ├── start.bat           # Windows 一键启动
│   │   └── pyproject.toml
│   ├── frontend/               # 前端（React + Vite + Ant Design Pro）
│   │   ├── src/
│   │   │   ├── pages/          # 页面（Dashboard/Workers/Models/Usage/Knowledge/...）
│   │   │   ├── layouts/        # 布局
│   │   │   └── services/       # API 调用
│   │   └── package.json
│   ├── docker-compose.yml
│   └── .env.example
└── README.md
```

## 前端构建（可选）

仓库已附带构建好的前端（`backend/data/ui/`）。如需修改前端并重新构建：

```bash
cd cpustack/frontend
npm install
npm run build
# 将 dist/ 内容拷贝到 backend/data/ui/
```

## 测试

```bash
cd cpustack/backend
python test_stage3.py    # 调度与负载均衡测试（49 项）
python test_stage4.py    # 生产化与软锁测试（38 项）
python test_stage5.py    # 算法优化与目录测试（64 项）
```

## 常见问题

**Q：模型文件在哪？为什么仓库里没有？**
A：模型文件体积大（0.9-5GB），不随仓库分发。在 Web UI 拉取模型时会自动从 HuggingFace 国内镜像下载到 `data/cache/`。

**Q：子节点注册失败怎么办？**
A：检查：① 子节点能否访问主节点 `http://<主节点IP>:8080/healthz`；② 两端 `CPUSTACK_WORKER_TOKEN` 是否一致；③ 防火墙是否放行 8080、30080、30090 端口。

**Q：没有 llama-server 二进制能启动吗？**
A：可以启动主节点和子节点进程，Web UI 与调度功能正常，但实际推理实例会因找不到二进制而进入 `error` 状态。需从 [llama.cpp releases](https://github.com/ggerganov/llama.cpp/releases) 下载并加入 PATH。

**Q：单机模式和多节点模式如何选择？**
A：模型内存 < 单机可用内存时用单机（`llama_cpp_standalone`）；超过单机内存时用 RPC 池化（`llama_cpp_rpc`）或流水线并行（`prima_cpp`）跨节点部署。

## 技术栈

- **后端**：Python 3.11+、FastAPI、SQLModel、SQLAlchemy、APScheduler、httpx
- **前端**：React 18、TypeScript、Vite、Ant Design Pro、ECharts
- **推理**：llama.cpp（llama-server / rpc-server / prima-server）
- **数据库**：PostgreSQL（生产）/ SQLite（本地开发）

## License

本项目仅供学习与研究使用。
