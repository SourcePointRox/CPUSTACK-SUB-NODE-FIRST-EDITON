"""Worker 注册与心跳管理。

注册流程：POST /v2/worker-registration → 获取 worker_uuid + api_key
心跳流程：周期采集状态 → POST /v2/worker-sync
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone

import httpx

from cpustack.config import settings
from cpustack.detector.collector import collect_worker_status

logger = logging.getLogger(__name__)


class WorkerManager:
    """管理 Worker 与 Server 的注册和心跳通信。"""

    def __init__(self):
        self._worker_uuid: str | None = None
        self._api_key: str | None = None
        self._worker_id: int | None = None
        self._registered_ip: str | None = None  # 注册到 Server 时上报的 IP
        self._heartbeat_task: asyncio.Task | None = None
        # 动态覆盖：被主节点一键接管后，用覆盖值代替 settings 中的默认配置
        self._server_url_override: str | None = None
        self._token_override: str | None = None
        # 心跳状态跟踪（供子节点状态页显示）
        self._last_heartbeat_ok: bool = False
        self._last_heartbeat_at: datetime | None = None
        self._heartbeat_failures: int = 0

    @property
    def effective_server_url(self) -> str:
        """当前生效的主节点地址（覆盖值优先）。"""
        return self._server_url_override or settings.server_url

    @property
    def effective_token(self) -> str:
        """当前生效的集群 token（覆盖值优先）。"""
        return self._token_override or settings.worker_token

    @property
    def worker_uuid(self) -> str | None:
        return self._worker_uuid

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def worker_id(self) -> int | None:
        return self._worker_id

    @property
    def last_heartbeat_ok(self) -> bool:
        """最近一次心跳是否成功。"""
        return self._last_heartbeat_ok

    @property
    def last_heartbeat_at(self) -> datetime | None:
        """最近一次心跳时间。"""
        return self._last_heartbeat_at

    @property
    def heartbeat_failures(self) -> int:
        """连续心跳失败次数。"""
        return self._heartbeat_failures

    async def register(
        self,
        server_url_override: str | None = None,
        token_override: str | None = None,
    ) -> bool:
        """向 Server 注册。

        Args:
            server_url_override: 主节点地址覆盖（一键接管时由主节点推送）
            token_override: 集群 token 覆盖
        """
        # 记录覆盖值，后续心跳也使用
        if server_url_override:
            self._server_url_override = server_url_override
        if token_override:
            self._token_override = token_override

        server_url = self.effective_server_url
        token = self.effective_token
        worker_name = settings.worker_name or socket.gethostname()
        local_ip = self._get_local_ip()
        self._registered_ip = local_ip

        logger.info(
            "Worker 注册中: %s (%s:%d) -> %s",
            worker_name, local_ip, settings.worker_port, server_url,
        )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{server_url}/v2/worker-registration",
                    json={
                        "name": worker_name,
                        "token": token,
                        "ip": local_ip,
                        "port": settings.worker_port,
                    },
                )
                if resp.status_code != 200:
                    logger.error("Worker 注册失败: %s %s", resp.status_code, resp.text)
                    return False

                data = resp.json()
                self._worker_uuid = data["worker_uuid"]
                self._api_key = data["api_key"]
                self._worker_id = data["worker_id"]

                # 持久化凭证（后续重启可复用）；失败不影响注册成功
                # Windows 上 data_dir 可能不可写，但内存凭证已赋值，心跳可正常上报
                try:
                    self._save_credentials()
                except Exception:
                    logger.warning("保存 Worker 凭证失败，不影响注册", exc_info=True)

                logger.info(
                    "Worker 注册成功: id=%d uuid=%s", self._worker_id, self._worker_uuid
                )
                return True

        except Exception:
            logger.exception("Worker 注册异常")
            return False

    def _save_credentials(self) -> None:
        """持久化 Worker 凭证。"""
        from pathlib import Path

        cred_file = settings.data_path / "worker_credentials.json"
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        import json

        cred_file.write_text(
            json.dumps(
                {
                    "worker_uuid": self._worker_uuid,
                    "api_key": self._api_key,
                    "worker_id": self._worker_id,
                }
            ),
            encoding="utf-8",
        )

    def _load_credentials(self) -> bool:
        """加载已保存的凭证。"""
        from pathlib import Path
        import json

        cred_file = settings.data_path / "worker_credentials.json"
        if not cred_file.exists():
            return False

        try:
            data = json.loads(cred_file.read_text(encoding="utf-8"))
            self._worker_uuid = data["worker_uuid"]
            self._api_key = data["api_key"]
            self._worker_id = data["worker_id"]
            return True
        except Exception:
            logger.debug("加载凭证失败")
            return False

    async def sync_status(self) -> bool:
        """上报资源状态到 Server。"""
        if not self._worker_uuid or not self._api_key:
            return False

        status = collect_worker_status()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.effective_server_url}/v2/worker-sync",
                    json={
                        "worker_uuid": self._worker_uuid,
                        "api_key": self._api_key,
                        "status": status,
                    },
                )
                if resp.status_code == 200:
                    return True
                logger.warning("状态同步失败: %s", resp.status_code)
                return False
        except Exception:
            logger.debug("状态同步异常")
            return False

    async def start_heartbeat(self, interval: int = 15) -> None:
        """启动心跳循环。"""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

    async def _heartbeat_loop(self, interval: int) -> None:
        """心跳主循环：失败时指数退避，连续失败超过阈值触发重新注册。"""
        consecutive_failures = 0
        max_backoff = 60
        re_register_threshold = 5

        while True:
            try:
                ok = await self.sync_status()
                if ok:
                    consecutive_failures = 0
                    self._last_heartbeat_ok = True
                    self._last_heartbeat_at = datetime.now(timezone.utc)
                    self._heartbeat_failures = 0
                else:
                    consecutive_failures += 1
                    self._last_heartbeat_ok = False
                    self._heartbeat_failures = consecutive_failures
                    backoff = min(interval * (2 ** consecutive_failures), max_backoff)
                    logger.warning(
                        "心跳失败（连续 %d 次），%ds 后重试",
                        consecutive_failures,
                        int(backoff),
                    )
                    if consecutive_failures >= re_register_threshold:
                        logger.warning(
                            "连续 %d 次心跳失败，尝试重新注册...",
                            consecutive_failures,
                        )
                        try:
                            await self.register()
                            consecutive_failures = 0
                        except Exception:
                            logger.exception("重新注册失败")
                    await asyncio.sleep(backoff)
                    continue
            except Exception:
                consecutive_failures += 1
                logger.exception("心跳循环异常")
                await asyncio.sleep(min(interval * 2, 30))
                continue
            await asyncio.sleep(interval)

    def _get_local_ip(self) -> str:
        """获取本机局域网 IP（过滤 loopback/link-local/代理 TUN 等虚拟网卡）。

        多网卡环境下（如安装了 Clash/代理 TUN），默认路由出口可能指向虚拟网卡
        (198.18.0.0/15)，导致上报的 IP 对局域网内其他节点不可达。
        枚举所有网卡地址，优先返回真实局域网 IP。
        """
        candidates: list[str] = []

        # 1. 用 psutil 枚举所有网卡 IPv4（最全面）
        try:
            import psutil

            for _, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and a.address:
                        if a.address not in candidates:
                            candidates.append(a.address)
        except Exception:
            pass

        # 2. 主机名解析
        try:
            for _, _, _, _, sockaddr in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET
            ):
                ip = sockaddr[0]
                if ip and ip not in candidates:
                    candidates.append(ip)
        except Exception:
            pass

        # 3. 默认路由出口 IP（可能因代理 TUN 返回虚拟 IP，作为兜底候选）
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and ip not in candidates:
                candidates.append(ip)
        except Exception:
            pass

        def _is_real_lan(ip: str) -> bool:
            if not ip:
                return False
            if ip.startswith(("127.", "169.254.", "0.")):
                return False
            # 198.18.0.0/15: IANA 保留网络基准测试段，常见于代理 TUN（Clash 等）
            if ip.startswith("198.18.") or ip.startswith("198.19."):
                return False
            return True

        for ip in candidates:
            if _is_real_lan(ip):
                return ip
        return candidates[0] if candidates else "127.0.0.1"
