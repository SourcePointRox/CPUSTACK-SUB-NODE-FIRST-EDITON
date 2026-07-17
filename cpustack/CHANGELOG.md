# 更新日志

本文件记录 CPUSTACK 项目的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

---

## v1.1.0 (2026-07-17)

### 新增

- 新增：主节点仪表盘 UI 美化（渐变 Hero 横幅、彩色图标卡片、渐变面积折线图、30 秒自动刷新）
- 新增：登录页视觉升级（浮动光斑动画、渐变 Logo、圆角按钮）
- 新增：全局样式系统（页面标题渐变文字、卡片悬停动画、暗色模式适配）
- 新增：整体性能测试脚本与基准报告

### 修复

- 修复：`model_instances.state` 字段大小写不一致导致 `/v2/models/instances` 接口 500 错误（数据库存储小写枚举值 `scheduled`，SQLAlchemy 期望大写枚举名 `SCHEDULED`）

### 优化

- 优化：Dashboard 统计卡片改为彩色渐变图标 + 悬停浮起效果
- 优化：ECharts 折线图改为渐变面积填充 + 隐藏数据点标记 + 虚线网格
- 优化：ProLayout 主题令牌（圆角 8px、主色 #1668dc）
- 优化：暗色模式下的卡片、标题、统计值颜色适配

### 性能测试结果（v1.1.0）

| 测试项 | 平均延迟 | P50 | P95 | 评价 |
|--------|---------|-----|-----|------|
| GET /healthz | 4.8 ms | 3.2 ms | 19.9 ms | 优秀 |
| GET /v2/version | 2.6 ms | - | - | 优秀 |
| GET /v2/models | 14.7 ms | - | - | 优秀 |
| GET /v2/models/instances | 9.7 ms | - | - | 优秀 |
| GET /v2/models/catalog | 9.3 ms | - | - | 优秀 |
| GET /v2/workers | 16.1 ms | 15.5 ms | 26.9 ms | 优秀 |
| GET /v2/dashboard | 31.4 ms | 21.0 ms | 88.9 ms | 良好 |
| GET /v2/auth/api-keys | 10.8 ms | - | - | 优秀 |
| GET /v2/tokens/summary | 11.0 ms | - | - | 优秀 |
| GET /v2/knowledge-bases | 19.2 ms | - | - | 优秀 |
| POST /v2/auth/login | 73-106 ms | - | - | 良好（bcrypt） |
| 并发登录（4线程×12） | ~13-17 s | - | - | 受限（SQLite 单写 + bcrypt） |

> **结论**：管理 API 单请求延迟 10-30ms，性能优秀。并发登录受 SQLite 单写锁与 bcrypt 哈希（~100ms/次）限制，生产环境建议改用 PostgreSQL。

---

## v1.0.0 (2026-07-17)

首次正式版本发布。

### 新增

- 首次正式版本发布
- 新增：版本号显示（主界面左上角 LOGO 后）
- 新增：子计算节点状态预览界面（CPU/内存图形化负载 + 主节点连接状态）
- 新增：局域网子节点自动扫描（UDP 广播）
- 新增：一键接管子节点（`POST /v2/discovery/adopt`）
- 新增：模型目录 YAML 配置与一键拉取
- 新增：TOKEN 用量计量与统计
- 新增：本地知识库（CRUD + BM25 检索）

### 修复

- 修复：HuggingFace 镜像端点设置时机错误导致模型下载失败
- 修复：qwen3-32b 模型文件名不匹配（`Qwen3-32B` → `Qwen_Qwen3-32B`）
- 修复：API Key 创建 500 错误（缺少 `expires_at`/`created_at` 字段）
- 修复：子节点 ServeManager 使用错误的 server_url 导致轮询失败
- 修复：子节点自报虚拟 TUN 网卡 IP（198.18.0.0/15）
- 修复：一键接管时 `_save_credentials` 异常导致注册失败误报

### 优化

- 优化：RPC 内存分配预留 20%（KV cache + 激活）
- 优化：心跳失败指数退避 + 自动重新注册
- 优化：大型模型调度策略（单节点优先，不足时跨节点池化）
