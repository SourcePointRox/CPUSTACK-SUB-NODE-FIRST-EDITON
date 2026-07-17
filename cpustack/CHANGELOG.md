# 更新日志

本文件记录 CPUSTACK 项目的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/)。

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
