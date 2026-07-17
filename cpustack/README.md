# CPUSTACK

> CPU 分布式 AI 模型部署与推理平台

**当前版本：v1.0.0**

CPUSTACK 是一个基于纯 CPU 的分布式 AI 模型部署与推理平台。通过主节点（Master）与多个子节点（Slave/Worker）协同工作，将大语言模型推理任务分散到局域网内多台普通 PC 的 CPU 上运行，无需 GPU 即可构建私有化 AI 推理集群。平台提供 OpenAI 兼容 API、Web 管理界面、模型目录一键拉取、TOKEN 用量计量与本地知识库等完整能力，适合企业内网、实验室、边缘计算等无 GPU 环境使用。

---

## 目录

- [功能特性](#功能特性)
- [架构说明](#架构说明)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [子节点状态预览](#子节点状态预览)
- [模型部署](#模型部署)
- [API 使用](#api-使用)
- [更新日志](#更新日志)
- [常见问题](#常见问题)

---

## 功能特性

- **纯 CPU 推理**：基于 `llama.cpp` 与自研 `prima_cpp` 后端，无需 GPU 即可运行量化大模型（GGUF）。
- **分布式集群**：主节点统一调度，多子节点池化算力，支持单节点优先、跨节点数据并行两种调度策略。
- **局域网自动发现**：通过 UDP 广播自动扫描局域网内子节点，一键接管（adopt）加入集群。
- **OpenAI 兼容 API**：提供 `/v1/chat/completions` 等标准接口，可直接对接现有客户端与工具链。
- **模型目录**：YAML 配置的模型目录，支持一键拉取 HuggingFace 模型并自动缓存。
- **TOKEN 用量计量**：按 API Key 统计请求数、Token 消耗，支持用量分析与配额管理。
- **本地知识库**：内置知识库 CRUD 与 BM25 检索，支持文档切片与检索增强。
- **Web 管理界面**：提供仪表盘、节点管理、实例管理、模型管理、API Key、用量统计等可视化管理。
- **子节点状态预览**：子节点 30080 端口提供图形化 CPU/内存负载与主节点连接状态预览页。
- **心跳与自愈**：心跳失败指数退避重试，断线后自动重新注册，保障集群稳定。

---

## 架构说明

CPUSTACK 采用 **主节点（Master）+ 子节点（Slave/Worker）** 的主从架构：

```
                         ┌─────────────────────────────────────┐
                         │          主计算节点 (Master)          │
                         │  ┌─────────────────────────────┐    │
                         │  │  控制平面 (Server, :8081)    │    │
                         │  │  - REST API / Web UI         │    │
                         │  │  - 调度器 (Scheduler)        │    │
                         │  │  - 发现服务 (Discovery:30090) │    │
                         │  │  - 数据库 / 知识库           │    │
                         │  └──────────────┬──────────────┘    │
                         │  ┌──────────────┴──────────────┐    │
                         │  │  内嵌 Worker (单机推理)       │    │
                         │  └─────────────────────────────┘    │
                         └───────────┬──────────────┬──────────┘
                                     │ 注册/心跳     │ UDP 广播发现
                                     │              │
              ┌──────────────────────┴──────────────┴───────────────────────┐
              │                       │                                      │
   ┌──────────▼─────────┐  ┌──────────▼─────────┐              ┌───────────▼─────────┐
   │  子节点 A (Worker)  │  │  子节点 B (Worker)  │    ...       │  子节点 N (Worker)   │
   │  - 状态预览 :30080  │  │  - 状态预览 :30080  │              │  - 状态预览 :30080   │
   │  - 推理后端 :40000+ │  │  - 推理后端 :40000+ │              │  - 推理后端 :40000+  │
   │  - 发现监听 :30090  │  │  - 发现监听 :30090  │              │  - 发现监听 :30090   │
   └────────────────────┘  └────────────────────┘              └─────────────────────┘
```

- **主节点**：运行控制平面（Server）与内嵌 Worker，对外提供 API、Web 界面与调度能力；负责子节点发现、注册鉴权、任务调度与状态监控。
- **子节点**：仅运行 Worker，向主节点注册并周期上报心跳；接收主节点下发的部署/推理任务，在本地 CPU 上运行推理后端。
- **通信链路**：子节点通过 HTTP 向主节点注册并心跳；主节点通过 UDP 广播在局域网发现子节点，通过 HTTP 下发任务；推理流量通过网关负载均衡到具体 Worker。

---

## 目录结构

```
cpustack/
├── master/                  # 主节点部署配置（复制整套代码到主节点机器后使用）
│   ├── .env.example         # 主节点环境变量示例
│   ├── start.bat            # Windows 启动脚本
│   └── start.sh             # Linux 启动脚本
├── slave/                   # 子节点部署配置（复制整套代码到子节点机器后使用）
│   ├── .env.example         # 子节点环境变量示例（精简版）
│   ├── start.bat            # Windows 启动脚本（带参数交互）
│   └── start.sh             # Linux 启动脚本
├── backend/                 # 后端服务（主子节点共用同一份代码）
│   ├── cpustack/            # Python 包
│   │   ├── cli.py           # 命令行入口（both / server / worker）
│   │   ├── config.py        # 配置加载（环境变量 > YAML > 默认值）
│   │   ├── server/          # 控制平面（路由、调度、网关、发现、知识库）
│   │   ├── worker/          # 数据平面（Worker、推理后端、下载器、服务管理）
│   │   ├── catalog/         # 模型目录 YAML 与服务
│   │   └── detector/        # CPU/内存/指令集检测
│   ├── alembic/             # 数据库迁移
│   ├── data/                # 运行时数据（数据库、模型缓存、UI 静态资源）
│   └── pyproject.toml       # Python 依赖
├── frontend/                # Web 管理界面（React + Vite + TypeScript）
│   ├── src/
│   │   ├── pages/           # 仪表盘、节点、实例、模型、API Key、用量等页面
│   │   └── version.ts       # 前端版本号（与后端保持一致）
│   └── package.json
├── .env.example             # 通用环境变量示例
├── docker-compose.yml       # Docker 部署编排
├── README.md                # 本文档
└── CHANGELOG.md             # 更新日志
```

> **说明**：`master/` 与 `slave/` 仅包含部署配置与启动脚本，实际运行依赖同级的 `backend/` 目录。部署时请将整个 `cpustack/` 目录拷贝到目标机器，然后进入对应角色目录启动。

---

## 快速开始

### 环境要求

- **Python**：3.12 及以上
- **Node.js**：18 及以上（仅构建前端时需要；后端已内置构建好的静态资源，开箱即用）
- **操作系统**：Windows 10/11、Linux（Ubuntu 20.04+ / CentOS 8+ 推荐）、macOS
- **网络**：主子节点需在同一局域网，且 8081（主节点）、30080（Worker）、30090（发现）、40000-41000（推理服务）端口可互通

### 主节点部署

1. **获取代码**

   ```bash
   git clone <仓库地址> cpustack
   cd cpustack
   ```

2. **安装后端依赖**

   ```bash
   cd backend
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux/macOS
   source .venv/bin/activate
   pip install -e .
   cd ..
   ```

3. **配置环境变量**

   ```bash
   # Windows
   copy master\.env.example master\.env
   # Linux/macOS
   cp master/.env.example master/.env
   ```

   编辑 `master/.env`，重点修改：
   - `CPUSTACK_SECRET_KEY`：设置为 32 字节以上的随机字符串
   - `CPUSTACK_WORKER_TOKEN`：集群共享密钥（子节点必须一致）
   - `CPUSTACK_SERVER_URL`：改为本机实际局域网 IP（如 `http://192.168.1.240:8081`）

4. **初始化数据库**（首次部署）

   ```bash
   cd backend
   python init_db.py
   cd ..
   ```

5. **启动主节点**

   ```bash
   # Windows
   master\start.bat
   # Linux/macOS
   chmod +x master/start.sh && ./master/start.sh
   ```

   启动后访问 `http://主节点IP:8081` 进入 Web 管理界面（默认管理员账号见 `init_db.py`）。

### 子节点部署

1. **获取代码**（与主节点相同，拷贝整个 `cpustack/` 目录到子节点机器）

2. **安装后端依赖**（与主节点相同，`pip install -e .`）

3. **配置环境变量**

   ```bash
   # Windows
   copy slave\.env.example slave\.env
   # Linux/macOS
   cp slave/.env.example slave/.env
   ```

   编辑 `slave/.env`，重点修改：
   - `CPUSTACK_WORKER_TOKEN`：必须与主节点一致
   - `CPUSTACK_SERVER_URL`：替换为主节点实际 IP，如 `http://192.168.1.240:8081`

4. **启动子节点**

   ```bash
   # Windows（带参数直接启动，或交互式输入）
   slave\start.bat 192.168.1.240 8081 cpustack-cluster-token
   # Linux/macOS
   chmod +x slave/start.sh && ./slave/start.sh 192.168.1.240 8081 cpustack-cluster-token
   ```

   启动后子节点会自动向主节点注册，并在 `http://子节点IP:30080` 提供状态预览页。

### 子节点添加方式（局域网扫描 + 一键接管）

除手动配置 `.env` 启动外，CPUSTACK 还支持更便捷的子节点加入流程：

1. **局域网自动扫描**：主节点的发现服务通过 UDP 广播（端口 30090）周期性扫描局域网，Web 界面「节点管理」页面会列出所有被发现的子节点候选。
2. **一键接管**：在 Web 界面点击「接管」按钮，主节点调用 `POST /v2/discovery/adopt` 将目标子节点纳入集群，自动下发 Token 与主节点地址配置。
3. **接管校验**：接管完成后子节点会自动注册并上报心跳，节点状态变为「在线」即表示加入成功。

---

## 配置说明

配置优先级：**环境变量 > YAML 配置文件 > 默认值**。所有环境变量均以 `CPUSTACK_` 为前缀。

### 主节点配置（master/.env）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `CPUSTACK_DEBUG` | `true` | 调试模式，开启后输出详细日志 |
| `CPUSTACK_DATA_DIR` | `./data` | 数据目录（数据库、缓存、知识库） |
| `CPUSTACK_HOST` | `0.0.0.0` | 主节点监听地址 |
| `CPUSTACK_PORT` | `8081` | 主节点监听端口 |
| `CPUSTACK_SECRET_KEY` | - | JWT 签名密钥，生产环境必须修改 |
| `CPUSTACK_DB_URL` | `sqlite+aiosqlite:///./data/cpustack.db` | 异步数据库连接串 |
| `CPUSTACK_DB_URL_SYNC` | `sqlite:///./data/cpustack.db` | 同步数据库连接串（迁移用） |
| `CPUSTACK_WORKER_TOKEN` | `cpustack-cluster-token` | 集群共享 Token，主子节点必须一致 |
| `CPUSTACK_SERVER_URL` | `http://127.0.0.1:8081` | 主节点对外访问地址 |
| `CPUSTACK_WORKER_PORT` | `30080` | Worker HTTP 端口（状态预览） |
| `CPUSTACK_SCHEDULER_INTERVAL_SECONDS` | `30` | 调度器扫描间隔（秒） |
| `CPUSTACK_WORKER_HEARTBEAT_TIMEOUT_SECONDS` | `120` | 心跳超时时间（秒） |
| `CPUSTACK_MODEL_CACHE_DIR` | `./data/cache` | 模型缓存目录 |
| `CPUSTACK_HUGGINGFACE_MIRROR` | `https://hf-mirror.com` | HuggingFace 镜像地址 |
| `CPUSTACK_SERVICE_PORT_RANGE_START` | `40000` | 推理服务端口范围起点 |
| `CPUSTACK_SERVICE_PORT_RANGE_END` | `41000` | 推理服务端口范围终点 |
| `CPUSTACK_DISCOVERY_PORT` | `30090` | 子节点发现服务端口 |
| `CPUSTACK_DISCOVERY_SCAN_TIMEOUT` | `5` | 局域网扫描超时（秒） |

### 子节点配置（slave/.env）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `CPUSTACK_DEBUG` | `false` | 调试模式 |
| `CPUSTACK_DATA_DIR` | `./data` | 数据目录 |
| `CPUSTACK_LOG_LEVEL` | `INFO` | 日志级别 |
| `CPUSTACK_WORKER_TOKEN` | `cpustack-cluster-token` | 集群 Token，须与主节点一致 |
| `CPUSTACK_SERVER_URL` | `http://主节点IP:8081` | 主节点地址，必须替换为实际 IP |
| `CPUSTACK_WORKER_NAME` | （空） | Worker 名称，留空则用主机名 |
| `CPUSTACK_WORKER_PORT` | `30080` | Worker HTTP 端口（状态预览） |
| `CPUSTACK_MODEL_CACHE_DIR` | `./data/cache` | 模型缓存目录 |
| `CPUSTACK_HUGGINGFACE_MIRROR` | `https://hf-mirror.com` | HuggingFace 镜像地址 |
| `CPUSTACK_DISCOVERY_PORT` | `30090` | 子节点发现服务端口 |

---

## 子节点状态预览

每个子节点的 Worker 会在 `CPUSTACK_WORKER_PORT`（默认 30080）端口提供一个**图形化状态预览页**，无需登录即可访问：

- **访问地址**：`http://子节点IP:30080`
- **展示内容**：
  - CPU 使用率（图形化负载条，按核心展示）
  - 内存使用率（已用/总量）
  - 主节点连接状态（已连接/断开、最近心跳时间）
  - 当前 Worker 名称与注册状态
  - 本地运行的推理实例列表

该页面便于运维人员快速定位子节点健康状态。若显示「主节点未连接」，请检查 `CPUSTACK_SERVER_URL` 配置与网络连通性。

---

## 模型部署

CPUSTACK 通过**模型目录（Model Catalog）**管理可部署模型，配置文件位于 `backend/cpustack/catalog/model_catalog.yaml`。

### 从模型目录拉取模型

1. **查看模型目录**：在 Web 界面「模型管理」页面查看 `model_catalog.yaml` 中预置的模型列表（如 Qwen3、Qwen2.5 等 GGUF 量化模型）。
2. **一键拉取**：点击「拉取」按钮，或调用 `POST /v2/models/pull`，主节点会从 HuggingFace（或配置的镜像）下载模型权重到 `CPUSTACK_MODEL_CACHE_DIR`。
3. **部署实例**：模型下载完成后，在「实例管理」创建推理实例，调度器会自动选择合适的 Worker（单节点优先，资源不足时跨节点池化）部署推理后端。

### 命令行拉取

```bash
cd backend
python pull_qwen3.py        # 示例：拉取 Qwen3 模型
python register_model.py    # 示例：注册模型到目录
```

> **提示**：国内网络建议保留 `CPUSTACK_HUGGINGFACE_MIRROR=https://hf-mirror.com` 以加速下载。

---

## API 使用

CPUSTACK 提供两类 API：**OpenAI 兼容推理 API** 与 **管理 API**。所有 API 默认挂在主节点 `http://主节点IP:8081` 下。

### OpenAI 兼容 API

可直接对接 OpenAI SDK、LobeChat、NextChat 等客户端。

```bash
curl http://192.168.1.240:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API Key>" \
  -d '{
    "model": "qwen3-32b",
    "messages": [{"role": "user", "content": "你好，介绍一下你自己"}],
    "stream": false
  }'
```

主要端点：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 对话补全（支持流式） |
| `/v1/completions` | POST | 文本补全 |
| `/v1/models` | GET | 已部署模型列表 |
| `/v1/embeddings` | POST | 文本向量化（知识库检索用） |

### 管理 API

管理 API 用于集群运维，需使用管理员 Token 或 JWT 鉴权。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v2/auth/login` | POST | 管理员登录，获取 JWT |
| `/v2/workers` | GET | Worker 列表与状态 |
| `/v2/instances` | GET/POST | 推理实例查询与创建 |
| `/v2/models` | GET | 模型目录与已缓存模型 |
| `/v2/models/pull` | POST | 一键拉取模型 |
| `/v2/discovery/scan` | POST | 触发局域网子节点扫描 |
| `/v2/discovery/adopt` | POST | 一键接管子节点 |
| `/v2/tokens` | GET/POST | API Key 管理 |
| `/v2/usage` | GET | TOKEN 用量统计 |
| `/v2/knowledge` | GET/POST | 知识库 CRUD |

> 详细接口字段请参考后端路由源码 `backend/cpustack/server/routes/`。

---

## 更新日志

详见 [CHANGELOG.md](./CHANGELOG.md)。

---

## 常见问题

### Q1：子节点启动后一直显示「未注册」或主节点看不到子节点？

**可能原因与排查**：
- `CPUSTACK_WORKER_TOKEN` 与主节点不一致 → 核对两端 Token。
- `CPUSTACK_SERVER_URL` 指向 `127.0.0.1` 或错误 IP → 子节点必须填写主节点实际局域网 IP。
- 防火墙拦截 8081 端口 → 放行主节点 8081 端口。
- 子节点机器有多块网卡，自报了虚拟 IP → 已在 v1.0.0 修复 TUN 网卡 IP 误报问题，请升级到最新版本。

### Q2：模型下载失败，提示 HuggingFace 连接超时？

- 确认 `CPUSTACK_HUGGINGFACE_MIRROR=https://hf-mirror.com` 已正确配置。
- v1.0.0 已修复镜像端点设置时机错误导致下载失败的问题，请确保使用最新版本。
- 如使用代理，请确认代理对 `hf-mirror.com` 可达。

### Q3：创建 API Key 报 500 错误？

v1.0.0 已修复因 `expires_at`/`created_at` 字段缺失导致的 500 错误，请升级到最新版本。

### Q4：qwen3-32b 模型部署后加载失败？

v1.0.0 已修复模型文件名不匹配问题（`Qwen3-32B` → `Qwen_Qwen3-32B`）。请重新拉取模型或更新模型目录配置。

### Q5：大型模型（如 32B）单机跑不动怎么办？

CPUSTACK v1.0.0 优化了大型模型调度策略：单节点资源不足时自动启用跨节点池化（数据并行），将模型分片部署到多个子节点协同推理。请确保有足够数量的子节点在线，且总内存满足模型需求。

### Q6：RPC 推理时内存不足（OOM）？

v1.0.0 已优化 RPC 内存分配，预留 20% 内存用于 KV cache 与激活值。如仍 OOM，请：
- 减小 `n_ctx`（上下文长度）。
- 使用更激进的量化版本（如 Q3_K_M）。
- 增加子节点数量以分摊负载。

### Q7：主节点重启后子节点需要重新注册吗？

不需要。子节点心跳失败后会指数退避重试，主节点恢复后自动重新注册并恢复在线状态。但建议主节点使用持久化数据库（SQLite/PostgreSQL）以保留集群元数据。

### Q8：Windows 下 .bat 脚本中文乱码？

启动脚本已通过 `chcp 65001` 切换控制台到 UTF-8 编码。如仍乱码，请确认脚本文件以 UTF-8 编码保存，且 Windows 终端字体支持中文。

---

© 2026 CPUSTACK. CPU 分布式 AI 模型部署与推理平台。
