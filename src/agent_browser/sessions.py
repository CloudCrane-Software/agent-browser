# coding: utf-8
"""会话隔离：session_id → 独立 cookie/存储命名空间.

真实驱动的 cookie 隔离未接入（见 README TODO）；本模块先以 ``storage``
命名空间 + 每会话独立驱动实例**固化隔离语义**：

- 每个 ``BrowserSession`` 持有独立 ``storage`` dict 与独立 driver；
- 驱动工厂接收该会话的 storage（FakeDriver 把 type 的输入写进 storage）；
- destroy = driver.close() + 从管理器摘除（不可复用，防串会话）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .driver import FakeDriver

__all__ = ["BrowserSession", "SessionManager"]


@dataclass
class BrowserSession:
    session_id: str
    tenant_id: str
    driver: object
    storage: dict = field(default_factory=dict)
    created_at: str = ""

    @property
    def closed(self):
        return bool(getattr(self.driver, "closed", False))


class SessionManager:
    """会话生命周期：create / get / destroy / list（tenant_id 只随会话记录与审计）."""

    def __init__(self, driver_factory=None):
        if driver_factory is None:
            driver_factory = lambda storage: FakeDriver(storage=storage)  # noqa: E731
        self._factory = driver_factory
        self._sessions = {}

    def create(self, tenant_id="default"):
        storage = {}                                  # 每会话独立命名空间
        driver = self._factory(storage)
        sid = "bsess-%s" % uuid.uuid4().hex[:8]
        while sid in self._sessions:                  # 极小概率碰撞，重生成
            sid = "bsess-%s" % uuid.uuid4().hex[:8]
        session = BrowserSession(
            session_id=sid,
            tenant_id=str(tenant_id or "default"),
            driver=driver,
            storage=storage,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._sessions[sid] = session
        return session

    def get(self, session_id):
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def destroy(self, session_id):
        session = self.get(session_id)
        close = getattr(session.driver, "close", None)
        if callable(close):
            close()
        del self._sessions[session_id]
        return session

    def list(self, tenant_id=None):
        sessions = self._sessions.values()
        if tenant_id is not None:
            sessions = [s for s in sessions if s.tenant_id == tenant_id]
        return sorted(sessions, key=lambda s: s.session_id)

    def __len__(self):
        return len(self._sessions)
