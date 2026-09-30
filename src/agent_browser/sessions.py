# coding: utf-8
"""会话隔离与池化：session_id → 独立 cookie/存储命名空间；BrowserPool 进程级池.

``SessionManager``（FakeDriver 语义固化层）：

- 每个 ``BrowserSession`` 持有独立 ``storage`` dict 与独立 driver；
- 驱动工厂接收该会话的 storage（FakeDriver 把 type 的输入写进 storage）；
- destroy = driver.close() + 从管理器摘除（不可复用，防串会话）。

``BrowserPool``（真实驱动池，BP §2.5 规模化层的最小自研件）：

- 借出/归还语义：``acquire`` 占一个池槽并绑定**每会话独立 user-data-dir** 的
  persistent context（cookie/storage 进程级隔离）；``release`` 归还即销毁——
  context.close() + browser.close() 双保险 + 进程树兜底杀；
- 容量上限原子性（x2fix-R1）：acquire 检查通过即在锁内占预订占位
  （计入 len(entries)），驱动工厂锁外执行后回填，失败即回滚——池规模
  恒 ≤ max_size，并发 /session 突发不可突破（此前检查与占位分离）。
- 容量口径（BP §2.5）：32G/8C 单机经验值 20–40 并发，池默认 ``max_size=8``
  保守起步；
- 看门狗（BP §2.5 生产第一杀手=僵尸进程，实证 1.6 万孤儿 chrome 撞 pid_max）：
  周期扫描 cmdline 含本池 context 根目录且已变孤儿（ppid==1）的浏览器进程并
  SIGKILL——user-data-dir 路径是天然会话标记，无需 pid 簿记；另按空闲 TTL
  收割闲置会话。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .driver import FakeDriver

__all__ = ["BrowserSession", "SessionManager",
           "BrowserPool", "PoolExhausted", "PoolClosed"]


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


# ---------------------------------------------------------------------------
# BrowserPool：真实驱动的进程级会话池（借出/归还 + 看门狗收割）
# ---------------------------------------------------------------------------

class PoolExhausted(RuntimeError):
    """池已满且等待超时（或未配置等待）。"""


class PoolClosed(RuntimeError):
    """池已 shutdown 后再借用。"""


@dataclass
class _PoolEntry:
    session: BrowserSession
    user_data_dir: str
    created_monotonic: float = 0.0
    last_used_monotonic: float = 0.0


def _iter_matching_procs(marker):
    """扫描 cmdline 含 ``marker`` 的进程，返回 [(pid, ppid, cmdline)].

    POSIX 读 /proc；Windows 上返回 []（部署目标=GPU Linux；windev 只做 mock 测试）。
    """
    out = []
    if os.name == "nt" or not os.path.isdir("/proc"):
        return out
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace").replace("\x00", " ")
            if marker not in cmdline:
                continue
            with open("/proc/%s/stat" % name, "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            # comm 可能含空格/括号：取最后一个 ')' 之后的首字段即 ppid
            ppid = int(stat[stat.rindex(")") + 1:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.append((int(name), ppid, cmdline))
    return out


def _kill_pid(pid):
    """尽力 SIGKILL 单进程（POSIX）/taskkill 树杀（Windows，仅兜底）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class BrowserPool:
    """浏览器会话池：acquire 借出（独立 user-data-dir persistent context），
    release 归还（双保险关闭 + 进程树兜底杀 + 目录清焚）.

    ``driver_factory(user_data_dir)`` 返回带协议六方法的驱动（默认
    PlaywrightDriver）；``close_hook(driver)`` 注入线程亲和的优雅关闭路径
    （如 server 的 per-session executor），不给则直接 ``driver.close()``。
    """

    def __init__(self, driver_factory=None, max_size=8, context_root=None,
                 idle_ttl_s=None, watchdog_interval_s=60.0,
                 enable_watchdog=True, close_hook=None,
                 now_fn=time.monotonic, driver_name=None):
        self._factory = driver_factory or self._default_factory
        self.driver_name = driver_name or getattr(self._factory, "driver_name", None) \
            or ("playwright" if driver_factory is None else "custom")
        self.max_size = int(max_size)
        if self.max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.context_root = context_root or os.path.join(
            tempfile.gettempdir(), "agent-browser-contexts")
        self.idle_ttl_s = idle_ttl_s
        self.watchdog_interval_s = float(watchdog_interval_s)
        self.close_hook = close_hook
        self._now = now_fn
        self._lock = threading.Lock()
        self._slot = threading.Condition(self._lock)
        self._entries = {}                 # sid → _PoolEntry
        self.reaped_pids = []              # 看门狗收割留痕（审计证据）
        self.reaped_sessions = []          # TTL 收割留痕
        self._closed = False
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        if enable_watchdog:
            self.start_watchdog()

    @staticmethod
    def _default_factory(user_data_dir):
        # 延迟导入：未装 playwright 时借用才报错，不拖累 import agent_browser
        from .playwright_driver import PlaywrightDriver
        return PlaywrightDriver(user_data_dir=user_data_dir)

    # ------------------------------------------------------------- 借出 / 归还

    def acquire(self, tenant_id="default", timeout_s=30.0):
        """借出一个会话（池满时等待至多 timeout_s；None=不等待）。

        锁内预订（x2fix-R1）：检查通过即在 ``self._entries`` 占一个
        ``session=None`` 的预订占位（计入 ``len(self._entries)``），Chrome
        驱动工厂在锁外执行完毕后回填真身；工厂失败/池被并发关闭即回滚
        占位并唤醒等待者。修复前检查与占位分离（放锁后才在 :244 占位），
        N 个并发 acquire 可同时通过检查→并行拉起 N 个驱动，池实际规模
        远超 max_size（grok X2-R3，medium）。
        """
        deadline = None if timeout_s is None else self._now() + timeout_s
        with self._slot:
            while True:
                if self._closed:
                    raise PoolClosed("pool is shut down")
                if len(self._entries) < self.max_size:
                    sid = "bsess-%s" % uuid.uuid4().hex[:8]
                    while sid in self._entries:
                        sid = "bsess-%s" % uuid.uuid4().hex[:8]
                    user_data_dir = os.path.join(
                        self.context_root, "bsess-%s" % uuid.uuid4().hex[:12])
                    now = self._now()
                    # 预订：占位立即可见，后续并发检查同一把锁看到已占
                    self._entries[sid] = _PoolEntry(
                        session=None, user_data_dir=user_data_dir,
                        created_monotonic=now, last_used_monotonic=now)
                    break
                remaining = None if deadline is None else deadline - self._now()
                if remaining is not None and remaining <= 0:
                    raise PoolExhausted(
                        "pool exhausted: %d/%d sessions in use"
                        % (len(self._entries), self.max_size))
                self._slot.wait(remaining if remaining is not None else 0.1)
        os.makedirs(self.context_root, exist_ok=True)
        try:
            driver = self._factory(user_data_dir)
        except Exception:
            shutil.rmtree(user_data_dir, ignore_errors=True)
            with self._lock:
                self._entries.pop(sid, None)
                self._slot.notify_all()          # 回滚预订，唤醒等待的 acquire
            raise
        session = BrowserSession(
            session_id=sid,
            tenant_id=str(tenant_id or "default"),
            driver=driver,
            storage={},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            entry = self._entries.get(sid)
            if entry is None:
                # 构造期间池被关闭/收割：驱动已出生但无槽位——现关现焚，
                # 报 PoolClosed（shutdown() 先置 _closed 先清占位，堵住
                # shutdown 后出生会话泄漏——x2fix-R1 同轮关闭的竞态）
                self._shutdown_entry(_PoolEntry(
                    session=session, user_data_dir=user_data_dir))
                raise PoolClosed("pool is shut down")
            entry.session = session               # 回填真身（同 sid 同占位）
            entry.user_data_dir = user_data_dir
            entry.created_monotonic = self._now()
            entry.last_used_monotonic = entry.created_monotonic
        return session

    def touch(self, session_id):
        """动作成功后刷新会话活跃时间（防看门狗 TTL 误收）。"""
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is not None:
                entry.last_used_monotonic = self._now()

    def release(self, session_id):
        """归还并销毁：优雅关闭（hook/driver.close）→ 进程树兜底杀 → 目录清焚."""
        with self._lock:
            entry = self._entries.pop(session_id, None)
            self._slot.notify_all()               # 槽位释放，唤醒等待的 acquire
        if entry is None:
            raise KeyError(session_id)
        self._shutdown_entry(entry)
        return entry.session

    def get(self, session_id):
        with self._lock:
            entry = self._entries.get(session_id)
        if entry is None:
            raise KeyError(session_id)
        return entry.session

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def stats(self):
        with self._lock:
            return {
                "max_size": self.max_size,
                "active": len(self._entries),
                "context_root": self.context_root,
                "reaped_orphan_pids": len(self.reaped_pids),
                "reaped_idle_sessions": len(self.reaped_sessions),
                "watchdog": self._watchdog_thread is not None
                and not self._watchdog_stop.is_set(),
            }

    # ------------------------------------------------------------- 看门狗

    def start_watchdog(self):
        if self._watchdog_thread is not None:
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="agent-browser-watchdog", daemon=True)
        self._watchdog_thread.start()

    def stop_watchdog(self, timeout_s=5.0):
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
        self._watchdog_thread = None

    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(self.watchdog_interval_s):
            try:
                self.sweep()
            except Exception:                     # noqa: BLE001 — 看门狗永不带崩服务
                pass

    def sweep(self, now=None):
        """看门狗单轮：①孤儿进程收割 ②空闲 TTL 会话收割。返回收割 pid 数."""
        now = self._now() if now is None else now
        killed = self._reap_orphans()
        killed += self._reap_idle(now)
        return killed

    def _reap_orphans(self):
        """cmdline 含本池 context 根 且 ppid==1（父已死被 init 收养）→ SIGKILL."""
        if not os.path.isdir(self.context_root):
            return 0
        killed = 0
        for pid, ppid, _cmdline in _iter_matching_procs(self.context_root):
            if ppid != 1:
                continue
            if _kill_pid(pid):
                killed += 1
                self.reaped_pids.append(pid)
        return killed

    def _reap_idle(self, now):
        if self.idle_ttl_s is None:
            return 0
        stale = []
        with self._lock:
            for sid, entry in self._entries.items():
                if now - entry.last_used_monotonic > self.idle_ttl_s:
                    stale.append((sid, entry))
            for sid, _entry in stale:
                self._entries.pop(sid, None)
            if stale:
                self._slot.notify_all()
        for sid, entry in stale:
            self.reaped_sessions.append(sid)
            self._shutdown_entry(entry)
        return len(stale)

    # ------------------------------------------------------------- 关闭卫生

    def _shutdown_entry(self, entry):
        """BP §2.5 双保险 + 兜底：优雅关 → 进程树杀 → 目录清焚."""
        session = entry.session
        if session is None:
            # acquire 的预订占位（锁内预订、工厂锁外执行中）被并发
            # shutdown/TTL 收割：驱动尚未出生，只清目录；acquire 回填时会
            # 发现占位已不在，自行关驱动并报 PoolClosed（x2fix-R1）
            shutil.rmtree(entry.user_data_dir, ignore_errors=True)
            return
        driver = session.driver
        try:
            if self.close_hook is not None:
                self.close_hook(driver)
            else:
                close = getattr(driver, "close", None)
                if callable(close):
                    close()
        except Exception:                         # noqa: BLE001 — 优雅关闭失败不阻塞兜底
            pass
        # 兜底：本会话 user-data-dir 的残余浏览器进程（无论父存亡）全部收割
        try:
            for pid, _ppid, _cmdline in _iter_matching_procs(entry.user_data_dir):
                if _kill_pid(pid):
                    self.reaped_pids.append(pid)
        finally:
            if getattr(driver, "closed", None) is False:
                try:
                    driver.closed = True          # 强杀后置位，防后续动作误用
                except Exception:                 # noqa: BLE001
                    pass
            shutil.rmtree(entry.user_data_dir, ignore_errors=True)  # 用完即焚

    def close_all(self):
        """关闭全部会话（shutdown 用）。"""
        with self._lock:
            sids = list(self._entries)
            entries = [self._entries.pop(s) for s in sids]
            self._slot.notify_all()
        for entry in entries:
            self._shutdown_entry(entry)
        return len(entries)

    def shutdown(self):
        """停看门狗 + 关闭全部会话；之后 acquire 抛 PoolClosed。

        ``_closed`` 与全部占位在**同一把锁内**先置先清（x2fix-R1）：
        此前 stop_watchdog→close_all→另起锁置 _closed 的顺序，给
        「shutdown 进行中 acquire 检查通过→工厂锁外执行→回填」留了
        窗口，产出 shutdown 后出生、看门狗已停的孤儿会话。
        """
        self.stop_watchdog()
        with self._lock:
            self._closed = True
            sids = list(self._entries)
            entries = [self._entries.pop(s) for s in sids]
            self._slot.notify_all()
        for entry in entries:
            self._shutdown_entry(entry)
