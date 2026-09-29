# coding: utf-8
"""BrowserPool 测试——借出/归还、max_size、user-data-dir 隔离、看门狗收割.

零真浏览器：driver_factory 注入 fake 驱动；孤儿进程扫描/杀 via monkeypatch。
"""
from __future__ import annotations

import threading
import time

import pytest

import agent_browser.sessions as sessions_mod
from agent_browser import BrowserPool, PoolClosed, PoolExhausted


class FakePoolDriver:
    driver_name = "fake-pool"

    def __init__(self, user_data_dir):
        self.user_data_dir = user_data_dir
        self.closed = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.closed = True

    # 协议六动作最小面（server 测试复用）
    def goto(self, url):
        if self.closed:
            raise RuntimeError("driver is closed")
        return url

    def click(self, selector):
        return {"clicked": selector}

    def type(self, selector, text):
        return {"typed_len": len(text), "selector": selector}

    def extract(self, selector=""):
        return "fake-text:%s" % selector

    def screenshot(self):
        return b"FAKE-PNG-BYTES"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_pool(**kw):
    clock = Clock()
    defaults = dict(driver_factory=FakePoolDriver, max_size=2,
                    enable_watchdog=False, now_fn=clock,
                    context_root=kw.pop("context_root", None))
    defaults.update(kw)
    return BrowserPool(**defaults), clock


# ---------------------------------------------------------------- 借还与容量

def test_acquire_release_within_max_size():
    pool, _ = make_pool(max_size=2)
    a = pool.acquire()
    b = pool.acquire()
    assert len(pool) == 2
    with pytest.raises(PoolExhausted):
        pool.acquire(timeout_s=0)                     # 不等待即满
    pool.release(a.session_id)
    c = pool.acquire(timeout_s=0)                     # 归还后槽位释放
    assert len(pool) == 2 and c is not a
    pool.shutdown()


def test_acquire_blocks_then_succeeds_after_release():
    pool, _ = make_pool(max_size=1)
    a = pool.acquire()

    def releaser():
        time.sleep(0.05)
        pool.release(a.session_id)

    threading.Thread(target=releaser).start()
    b = pool.acquire(timeout_s=5.0)                   # 阻塞等待归还
    assert b.session_id != a.session_id
    pool.shutdown()


def test_user_data_dir_isolated_per_session():
    pool, _ = make_pool(max_size=4)
    a = pool.acquire()
    b = pool.acquire()
    assert a.driver.user_data_dir != b.driver.user_data_dir
    assert a.storage is not b.storage                 # BrowserSession 直借出
    pool.shutdown()
    assert pool.stats()["active"] == 0                # close_all 后全清


def test_release_closes_driver_double_insurance_then_removes():
    pool, _ = make_pool(max_size=1)
    a = pool.acquire()
    sid = a.session_id
    pool.release(sid)
    assert a.driver.close_calls == 1 and a.driver.closed is True
    with pytest.raises(KeyError):
        pool.get(sid)
    pool.shutdown()


def test_close_hook_routes_graceful_close():
    pool, _ = make_pool(max_size=1)
    closed_via = []
    pool.close_hook = lambda driver: closed_via.append(driver.user_data_dir) \
        or driver.close()
    a = pool.acquire()
    pool.release(a.session_id)
    assert closed_via == [a.driver.user_data_dir]
    assert a.driver.closed is True
    pool.shutdown()


def test_invalid_max_size_rejected():
    with pytest.raises(ValueError):
        make_pool(max_size=0)


# ---------------------------------------------------------------- 看门狗

def test_sweep_reaps_orphan_procs_marked_by_context_root(tmp_path, monkeypatch):
    root = str(tmp_path)                              # 目录须真实存在（isdir 早退）
    pool, _ = make_pool(context_root=root)
    procs = [(111, 1, "chrome --user-data-dir=%s/bsess-abc" % root),   # 孤儿→杀
             (222, 999, "chrome --user-data-dir=%s/bsess-abc" % root), # 有父→留
             (333, 1, "chrome --user-data-dir=/elsewhere/x")]          # 非本池→留
    killed = []
    # stub 模拟真实语义：只返回 cmdline 含 marker 的进程
    monkeypatch.setattr(sessions_mod, "_iter_matching_procs",
                        lambda marker: [p for p in procs if marker in p[2]])
    monkeypatch.setattr(sessions_mod, "_kill_pid",
                        lambda pid: killed.append(pid) or True)
    assert pool.sweep() == 1
    assert killed == [111]
    assert pool.reaped_pids == [111]
    pool.shutdown()


def test_sweep_reaps_idle_sessions_beyond_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_mod, "_iter_matching_procs", lambda marker: [])
    pool, clock = make_pool(context_root=str(tmp_path / "c"), idle_ttl_s=10.0)
    a = pool.acquire()
    clock.advance(11.0)
    b = pool.acquire()
    pool.sweep()                                      # a 超时→收割，b 保留
    assert pool.reaped_sessions == [a.session_id]
    assert a.driver.closed is True and a.driver.close_calls == 1
    assert pool.get(b.session_id) is b
    with pytest.raises(KeyError):
        pool.get(a.session_id)
    pool.shutdown()


def test_watchdog_thread_lifecycle(tmp_path):
    pool, _ = make_pool(context_root=str(tmp_path / "c"),
                        watchdog_interval_s=0.05, enable_watchdog=True)
    assert pool.stats()["watchdog"] is True
    time.sleep(0.12)                                  # 至少跑过一轮
    pool.stop_watchdog()
    assert pool.stats()["watchdog"] is False
    pool.shutdown()


def test_shutdown_rejects_further_acquire(tmp_path):
    pool, _ = make_pool(context_root=str(tmp_path / "c"))
    pool.shutdown()
    with pytest.raises(PoolClosed):
        pool.acquire()


def test_shutdown_closes_all_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions_mod, "_iter_matching_procs", lambda marker: [])
    pool, _ = make_pool(context_root=str(tmp_path / "c"))
    a = pool.acquire()
    b = pool.acquire()
    pool.shutdown()
    assert a.driver.closed and b.driver.closed
    assert len(pool) == 0


def test_release_kills_leftover_procs_and_burns_dir(tmp_path, monkeypatch):
    """BP §2.5 兜底：优雅关闭后残余进程按 user-data-dir 收割 + 目录清焚."""
    root = tmp_path / "c"
    pool, _ = make_pool(context_root=str(root))
    a = pool.acquire()
    procs = [(777, 555, "chrome --user-data-dir=%s" % a.driver.user_data_dir)]
    killed = []
    monkeypatch.setattr(sessions_mod, "_iter_matching_procs",
                        lambda marker: procs if a.driver.user_data_dir in marker
                        else [])
    monkeypatch.setattr(sessions_mod, "_kill_pid",
                        lambda pid: killed.append(pid) or True)
    pool.release(a.session_id)
    assert killed == [777]                            # 无论父存亡，残余即杀
    assert a.driver.closed is True
