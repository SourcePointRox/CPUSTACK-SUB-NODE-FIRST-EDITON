"""Worker 轻量 HTTP 服务：子节点状态预览 + 主节点一键接管注册。

监听 worker_port（默认 30080），暴露端点：
- GET  /                    子节点状态预览页（图形化负载 + 连接状态）
- GET  /internal/status     本机负载 + 连接状态 JSON（供预览页轮询）
- GET  /internal/health      健康检查（供主节点探测可达性）
- POST /internal/register    主节点推送 server_url + token，触发本节点重新注册并入池

设计要点：
- 极简 FastAPI 应用，不依赖数据库
- 嵌入式 uvicorn 运行在 Worker 的 asyncio 事件循环中
- 子节点预览页为自包含 HTML（无外部依赖），实时展示 CPU/内存负载与主节点连接状态
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from cpustack.worker.worker_manager import WorkerManager

logger = logging.getLogger(__name__)


class RegisterTriggerRequest(BaseModel):
    """主节点推送的注册触发请求。"""

    server_url: str  # 主节点的外部可达地址，如 http://192.168.1.100:8081
    worker_token: str  # 集群共享密钥
    name: str | None = None  # 可选：主节点指定的节点名称


class RegisterTriggerResponse(BaseModel):
    """注册触发结果。"""

    ok: bool
    worker_id: int | None = None
    worker_uuid: str | None = None
    message: str = ""


_STATUS_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CPUSTACK 子计算节点</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117; color: #c9d1d9; min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border-bottom: 1px solid #30363d; padding: 20px 32px;
    display: flex; align-items: center; gap: 16px;
  }
  .logo { width: 40px; height: 40px; flex-shrink: 0; }
  .header h1 { font-size: 22px; font-weight: 600; color: #58a6ff; }
  .header .ver { font-size: 13px; color: #8b949e; margin-left: 8px; }
  .header .role-badge {
    margin-left: auto; padding: 4px 12px; border-radius: 12px;
    font-size: 12px; font-weight: 600; background: #1f6feb22; color: #58a6ff;
    border: 1px solid #1f6feb44;
  }
  .container { max-width: 960px; margin: 0 auto; padding: 24px 20px; }
  .conn-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 24px; margin-bottom: 20px; display: flex; align-items: center; gap: 20px;
  }
  .conn-indicator {
    width: 16px; height: 16px; border-radius: 50%; flex-shrink: 0;
    background: #f85149; transition: background 0.3s;
  }
  .conn-indicator.ok {
    background: #3fb950; box-shadow: 0 0 12px #3fb95066; animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.5; } }
  .conn-info { flex: 1; }
  .conn-info .label { font-size: 13px; color: #8b949e; margin-bottom: 4px; }
  .conn-info .value { font-size: 16px; font-weight: 600; }
  .conn-info .value.ok { color: #3fb950; }
  .conn-info .value.err { color: #f85149; }
  .conn-info .sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
  .gauge-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 24px; display: flex; flex-direction: column; align-items: center;
  }
  .gauge-card h3 { font-size: 14px; color: #8b949e; margin-bottom: 16px; font-weight: 500; }
  .gauge { position: relative; width: 160px; height: 160px; }
  .gauge svg { transform: rotate(-90deg); }
  .gauge .track { fill: none; stroke: #21262d; stroke-width: 12; }
  .gauge .bar { fill: none; stroke-width: 12; stroke-linecap: round; transition: stroke-dashoffset 0.6s ease, stroke 0.3s; }
  .gauge .text {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    text-align: center;
  }
  .gauge .text .pct { font-size: 32px; font-weight: 700; }
  .gauge .text .unit { font-size: 12px; color: #8b949e; }
  .gauge-detail { margin-top: 12px; font-size: 13px; color: #8b949e; text-align: center; }
  .info-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 24px; margin-bottom: 20px;
  }
  .info-card h3 { font-size: 14px; color: #8b949e; margin-bottom: 16px; font-weight: 500; }
  .info-row {
    display: flex; justify-content: space-between; padding: 8px 0;
    border-bottom: 1px solid #21262d; font-size: 14px;
  }
  .info-row:last-child { border-bottom: none; }
  .info-row .k { color: #8b949e; }
  .info-row .v { color: #c9d1d9; font-weight: 500; }
  .spark-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 12px;
    padding: 24px; margin-bottom: 20px;
  }
  .spark-card h3 { font-size: 14px; color: #8b949e; margin-bottom: 12px; font-weight: 500; }
  .spark-wrap { display: flex; gap: 24px; }
  .spark-item { flex: 1; }
  .spark-item .label { font-size: 12px; color: #8b949e; margin-bottom: 6px; }
  .spark { width: 100%; height: 60px; }
  .footer { text-align: center; padding: 20px; font-size: 12px; color: #484f58; }
  .loading { text-align: center; padding: 40px; color: #8b949e; }
</style>
</head>
<body>
<div class="header">
  <svg class="logo" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <rect x="4" y="4" width="16" height="16" rx="2" stroke="#58a6ff" stroke-width="2"/>
    <rect x="9" y="9" width="6" height="6" rx="1" fill="#58a6ff"/>
    <path d="M2 9h2M2 15h2M20 9h2M20 15h2M9 2v2M15 2v2M9 20v2M15 20v2" stroke="#58a6ff" stroke-width="2" stroke-linecap="round"/>
  </svg>
  <h1>CPUSTACK <span class="ver" id="version">v1.0.0</span></h1>
  <span class="role-badge">子计算节点</span>
</div>

<div class="container">
  <div class="conn-card">
    <div class="conn-indicator" id="conn-dot"></div>
    <div class="conn-info">
      <div class="label">与主计算节点的连接状态</div>
      <div class="value err" id="conn-status">未连接</div>
      <div class="sub" id="conn-detail">等待主节点接管...</div>
    </div>
  </div>

  <div class="grid">
    <div class="gauge-card">
      <h3>CPU 使用率</h3>
      <div class="gauge">
        <svg width="160" height="160" viewBox="0 0 160 160">
          <circle class="track" cx="80" cy="80" r="68"/>
          <circle class="bar" id="cpu-bar" cx="80" cy="80" r="68"
            stroke="#58a6ff" stroke-dasharray="427" stroke-dashoffset="427"/>
        </svg>
        <div class="text">
          <div class="pct" id="cpu-pct">--</div>
          <div class="unit">CPU</div>
        </div>
      </div>
      <div class="gauge-detail" id="cpu-detail">-- 核心</div>
    </div>

    <div class="gauge-card">
      <h3>内存使用率</h3>
      <div class="gauge">
        <svg width="160" height="160" viewBox="0 0 160 160">
          <circle class="track" cx="80" cy="80" r="68"/>
          <circle class="bar" id="mem-bar" cx="80" cy="80" r="68"
            stroke="#3fb950" stroke-dasharray="427" stroke-dashoffset="427"/>
        </svg>
        <div class="text">
          <div class="pct" id="mem-pct">--</div>
          <div class="unit">内存</div>
        </div>
      </div>
      <div class="gauge-detail" id="mem-detail">-- GB / -- GB</div>
    </div>
  </div>

  <div class="spark-card">
    <h3>实时负载趋势（最近 60 秒）</h3>
    <div class="spark-wrap">
      <div class="spark-item">
        <div class="label">CPU %</div>
        <canvas class="spark" id="cpu-spark" width="400" height="60"></canvas>
      </div>
      <div class="spark-item">
        <div class="label">内存 %</div>
        <canvas class="spark" id="mem-spark" width="400" height="60"></canvas>
      </div>
    </div>
  </div>

  <div class="info-card">
    <h3>节点信息</h3>
    <div class="info-row"><span class="k">节点名称</span><span class="v" id="node-name">--</span></div>
    <div class="info-row"><span class="k">Worker ID</span><span class="v" id="worker-id">--</span></div>
    <div class="info-row"><span class="k">Worker UUID</span><span class="v" id="worker-uuid">--</span></div>
    <div class="info-row"><span class="k">本机 IP</span><span class="v" id="local-ip">--</span></div>
    <div class="info-row"><span class="k">主节点地址</span><span class="v" id="master-url">--</span></div>
    <div class="info-row"><span class="k">CPU 型号</span><span class="v" id="cpu-model">--</span></div>
    <div class="info-row"><span class="k">操作系统</span><span class="v" id="os-info">--</span></div>
    <div class="info-row"><span class="k">磁盘可用</span><span class="v" id="disk-info">--</span></div>
    <div class="info-row"><span class="k">最近心跳</span><span class="v" id="last-hb">--</span></div>
    <div class="info-row"><span class="k">连续失败</span><span class="v" id="fail-count">--</span></div>
  </div>

  <div class="footer">CPUSTACK 子计算节点状态预览 · 数据每 2 秒自动刷新</div>
</div>

<script>
const CIRC = 2 * Math.PI * 68; // 427.26
const MAX_HISTORY = 60;
let cpuHistory = [];
let memHistory = [];

function setGauge(barId, pctId, pct) {
  const offset = CIRC * (1 - pct / 100);
  const bar = document.getElementById(barId);
  bar.style.strokeDashoffset = offset;
  if (pct > 80) bar.style.stroke = '#f85149';
  else if (pct > 60) bar.style.stroke = '#d29922';
  else if (barId === 'cpu-bar') bar.style.stroke = '#58a6ff';
  else bar.style.stroke = '#3fb950';
  document.getElementById(pctId).textContent = pct.toFixed(1);
}

function fmtBytes(mb) {
  if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
  return mb.toFixed(0) + ' MB';
}

function fmtTime(iso) {
  if (!iso) return '无';
  const d = new Date(iso);
  return d.toLocaleTimeString('zh-CN');
}

function drawSpark(canvasId, history, color) {
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (history.length < 2) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < history.length; i++) {
    const x = (i / (MAX_HISTORY - 1)) * w;
    const y = h - (history[i] / 100) * h;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.fillStyle = color + '22';
  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fill();
}

async function refresh() {
  try {
    const resp = await fetch('/internal/status');
    const d = await resp.json();

    document.getElementById('version').textContent = 'v' + (d.version || '1.0.0');

    // 连接状态
    const dot = document.getElementById('conn-dot');
    const status = document.getElementById('conn-status');
    const detail = document.getElementById('conn-detail');
    if (d.connected) {
      dot.classList.add('ok');
      status.textContent = '已连接';
      status.className = 'value ok';
      detail.textContent = '主节点: ' + d.master_url + ' · 心跳正常';
    } else {
      dot.classList.remove('ok');
      status.textContent = d.registered ? '连接中断' : '未连接';
      status.className = 'value err';
      detail.textContent = d.registered
        ? '心跳失败 ' + d.heartbeat_failures + ' 次，正在重试...'
        : '等待主节点接管...';
    }

    // CPU
    const cpuPct = d.cpu_utilization || 0;
    setGauge('cpu-bar', 'cpu-pct', cpuPct);
    document.getElementById('cpu-detail').textContent =
      d.cpu_cores + ' 核心 · ' + (d.cpu_model || '未知');

    // 内存
    const memUsed = d.memory_total - d.memory_available;
    const memPct = d.memory_total > 0 ? (memUsed / d.memory_total) * 100 : 0;
    setGauge('mem-bar', 'mem-pct', memPct);
    document.getElementById('mem-detail').textContent =
      fmtBytes(memUsed) + ' / ' + fmtBytes(d.memory_total);

    // 历史
    cpuHistory.push(cpuPct);
    memHistory.push(memPct);
    if (cpuHistory.length > MAX_HISTORY) cpuHistory.shift();
    if (memHistory.length > MAX_HISTORY) memHistory.shift();
    drawSpark('cpu-spark', cpuHistory, '#58a6ff');
    drawSpark('mem-spark', memHistory, '#3fb950');

    // 节点信息
    document.getElementById('node-name').textContent = d.worker_name || '--';
    document.getElementById('worker-id').textContent = d.worker_id ?? '--';
    document.getElementById('worker-uuid').textContent = d.worker_uuid || '--';
    document.getElementById('local-ip').textContent = d.local_ip || '--';
    document.getElementById('master-url').textContent = d.master_url || '--';
    document.getElementById('cpu-model').textContent = d.cpu_model || '--';
    document.getElementById('os-info').textContent = (d.os || '--') + ' ' + (d.kernel || '');
    document.getElementById('disk-info').textContent =
      fmtBytes(d.disk_available) + ' 可用 / ' + fmtBytes(d.disk_total) + ' 总计';
    document.getElementById('last-hb').textContent = fmtTime(d.last_heartbeat_at);
    document.getElementById('fail-count').textContent = d.heartbeat_failures ?? 0;
  } catch (e) {
    console.error('刷新失败', e);
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


def create_worker_http_app(worker_manager: "WorkerManager") -> FastAPI:
    """构造 Worker 内部 HTTP 应用。

    Args:
        worker_manager: 当前 Worker 的 WorkerManager 实例

    Returns:
        FastAPI 应用（含状态预览页 + /internal/* 端点）
    """
    app = FastAPI(
        title="CPUSTACK Worker",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/", response_class=HTMLResponse)
    async def status_page() -> str:
        """子节点状态预览页：图形化负载 + 连接状态。"""
        return _STATUS_PAGE_HTML

    @app.get("/internal/status")
    async def status() -> dict:
        """返回本机负载 + 连接状态 JSON（供预览页轮询）。"""
        from cpustack.config import settings
        from cpustack.detector.collector import collect_worker_status
        from cpustack import __version__

        sys_status = collect_worker_status()
        wm = worker_manager
        registered = wm.worker_uuid is not None

        return {
            "version": __version__,
            "worker_name": settings.worker_name or socket.gethostname(),
            "worker_id": wm.worker_id,
            "worker_uuid": wm.worker_uuid,
            "local_ip": wm._registered_ip or "",
            "master_url": wm.effective_server_url,
            "registered": registered,
            "connected": wm.last_heartbeat_ok,
            "last_heartbeat_at": wm.last_heartbeat_at.isoformat()
            if wm.last_heartbeat_at
            else None,
            "heartbeat_failures": wm.heartbeat_failures,
            # 系统负载
            "cpu_model": sys_status.get("cpu_model", ""),
            "cpu_cores": sys_status.get("cpu_cores", 0),
            "cpu_utilization": sys_status.get("cpu_utilization", 0),
            "memory_total": sys_status.get("memory_total", 0),
            "memory_available": sys_status.get("memory_available", 0),
            "swap_total": sys_status.get("swap_total", 0),
            "swap_used": sys_status.get("swap_used", 0),
            "disk_total": sys_status.get("disk_total", 0),
            "disk_available": sys_status.get("disk_available", 0),
            "os": sys_status.get("os", ""),
            "kernel": sys_status.get("kernel", ""),
        }

    @app.get("/internal/health")
    async def health() -> dict:
        """健康检查：返回本节点基本信息，供主节点探测可达性。"""
        return {
            "status": "ok",
            "worker_uuid": worker_manager.worker_uuid,
            "worker_id": worker_manager.worker_id,
        }

    @app.post("/internal/register", response_model=RegisterTriggerResponse)
    async def trigger_register(req: RegisterTriggerRequest) -> RegisterTriggerResponse:
        """主节点一键接管注册：推送新的 server_url + token，触发本节点重新注册。

        流程：
        1. 清除本地旧凭证（避免复用错误的 uuid/api_key）
        2. 用新的 server_url + token 调用主节点 /v2/worker-registration
        3. 注册成功后，后续心跳自动用新地址上报，节点并入算力池
        """
        logger.info(
            "收到主节点注册触发: server_url=%s, name=%s",
            req.server_url,
            req.name,
        )

        # 清除旧凭证，强制完整重注册
        try:
            from cpustack.config import settings
            cred_file = settings.data_path / "worker_credentials.json"
            if cred_file.exists():
                cred_file.unlink()
        except Exception:
            logger.debug("清除旧凭证文件失败", exc_info=True)

        # 同步更新 settings 中的 server_url 和 worker_token，
        # 这样即使某些模块直接读取 settings.server_url（而非 effective_server_url），
        # 一键接管后也能指向正确的主节点地址。
        try:
            from cpustack.config import settings
            settings.server_url = req.server_url
            settings.worker_token = req.worker_token
            if req.name:
                settings.worker_name = req.name
        except Exception:
            logger.debug("更新 settings 失败", exc_info=True)

        # 执行重新注册（使用传入的 server_url + token 覆盖）
        try:
            ok = await worker_manager.register(
                server_url_override=req.server_url,
                token_override=req.worker_token,
            )
        except Exception:
            logger.exception("注册触发执行异常")
            ok = False

        # register() 可能因凭证持久化失败返回 False，但内存凭证已赋值（注册实际成功）
        # 此时 worker_uuid 一定有值，主节点可据此在数据库中确认节点已注册
        if ok or worker_manager.worker_uuid:
            return RegisterTriggerResponse(
                ok=True,
                worker_id=worker_manager.worker_id,
                worker_uuid=worker_manager.worker_uuid,
                message="注册成功，已并入算力池",
            )
        return RegisterTriggerResponse(
            ok=False, message="注册失败，请检查主节点地址与 token"
        )

    return app


async def start_worker_http(
    worker_manager: "WorkerManager",
    port: int | None = None,
) -> asyncio.Task | None:
    """启动 Worker 内部 HTTP 服务（嵌入式 uvicorn）。

    Args:
        worker_manager: Worker 的 WorkerManager 实例
        port: 监听端口，默认取 settings.worker_port

    Returns:
        后台运行的服务 task；启动失败返回 None
    """
    import uvicorn

    from cpustack.config import settings

    if port is None:
        port = settings.worker_port

    app = create_worker_http_app(worker_manager)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # 允许被取消时优雅退出
    server.config.load()
    server.lifespan = "off"

    async def _run() -> None:
        try:
            await server.serve()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Worker HTTP 服务异常退出")

    task = asyncio.create_task(_run())
    logger.info("Worker HTTP 服务已启动 (端口 %d)，含状态预览页 + 一键接管注册", port)
    return task
