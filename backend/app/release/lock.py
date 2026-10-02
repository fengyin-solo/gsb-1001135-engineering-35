"""编排锁：整个平台同一时刻只允许一个发布者推进发布闸门。

锁带租约（lease），持有者通过心跳续租；检查连接断开后租约到期，
同一批次可从未完成项复位（resume），重复的锁请求会被拒绝。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.release.errors import LockBusyError

LEASE_SECONDS = 60


class OrchestratorLock:
    def __init__(self, lease_seconds: int = LEASE_SECONDS) -> None:
        self._lease_seconds = lease_seconds
        self._holder_batch: str | None = None
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._guard = threading.RLock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _expired(self) -> bool:
        return self._expires_at is None or self._now() >= self._expires_at

    def acquire(self, batch_no: str) -> str:
        """获取编排锁；被其他批次持有时拒绝。同批次重入直接续租（复位续跑）。"""
        with self._guard:
            if self._holder_batch and self._holder_batch != batch_no and not self._expired():
                raise LockBusyError(
                    f"编排锁由批次 {self._holder_batch} 持有，同一时刻只允许一个发布者"
                )
            token = uuid.uuid4().hex
            self._holder_batch = batch_no
            self._token = token
            self._expires_at = self._now() + timedelta(seconds=self._lease_seconds)
            return token

    def heartbeat(self, batch_no: str, token: str) -> None:
        """持有者续租；连接断开期间无法心跳，租约到期后可被同批次复位。"""
        with self._guard:
            self._ensure_holder(batch_no, token)
            self._expires_at = self._now() + timedelta(seconds=self._lease_seconds)

    def release(self, batch_no: str, token: str) -> None:
        with self._guard:
            self._ensure_holder(batch_no, token)
            self._holder_batch = None
            self._token = None
            self._expires_at = None

    def holder(self) -> dict[str, object] | None:
        with self._guard:
            if self._holder_batch and not self._expired():
                return {
                    "batch_no": self._holder_batch,
                    "expires_at": self._expires_at.isoformat(),
                }
            if self._holder_batch and self._expired():
                # 租约过期视为连接断开：清掉悬挂持有者，允许复位
                stale = self._holder_batch
                self._holder_batch = None
                self._token = None
                self._expires_at = None
                return {"batch_no": stale, "expires_at": None, "stale": True}
            return None

    def _ensure_holder(self, batch_no: str, token: str) -> None:
        if self._holder_batch != batch_no or self._token != token or self._expired():
            raise LockBusyError("编排锁凭证无效或租约已过期，请从未完成项复位后继续")
