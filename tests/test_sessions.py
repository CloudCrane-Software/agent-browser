# coding: utf-8
"""会话隔离测试：独立 storage 命名空间 / 生命周期 / 与 run_task 组合."""
from __future__ import annotations

import pytest
from conftest import HOME, HOME_PAGE, base_task, make_driver

from agent_browser import Action, FakeDriver, SessionManager, run_task


def test_sessions_have_distinct_storage_namespaces():
    mgr = SessionManager()
    a = mgr.create(tenant_id="t1")
    b = mgr.create(tenant_id="t1")
    assert a.storage is not b.storage
    assert a.driver is not b.driver


def test_type_in_session_a_not_visible_in_session_b():
    mgr = SessionManager(driver_factory=lambda storage: FakeDriver(
        pages={HOME: HOME_PAGE}, storage=storage))
    a = mgr.create()
    b = mgr.create()
    a.driver.goto(HOME)
    b.driver.goto(HOME)
    a.driver.type("input#q", "tenant-a-secret-note")
    assert a.driver.extract("input#q") == "tenant-a-secret-note"
    assert b.driver.extract("input#q") == ""        # B 看不到 A 的命名空间


def test_destroy_closes_driver_and_removes_session():
    mgr = SessionManager()
    s = mgr.create()
    sid = s.session_id
    mgr.destroy(sid)
    assert s.closed is True
    assert len(mgr) == 0
    with pytest.raises(KeyError):
        mgr.get(sid)


def test_get_unknown_session_raises():
    mgr = SessionManager()
    with pytest.raises(KeyError):
        mgr.get("bsess-nope")


def test_session_ids_unique_and_tenant_recorded():
    mgr = SessionManager()
    sessions = [mgr.create(tenant_id="t%d" % i) for i in range(3)]
    ids = [s.session_id for s in sessions]
    assert len(set(ids)) == 3
    assert all(s.tenant_id.startswith("t") for s in sessions)
    assert [s.session_id for s in mgr.list(tenant_id="t1")] == [ids[1]]


def test_destroy_is_final_no_reuse():
    mgr = SessionManager()
    s = mgr.create()
    mgr.destroy(s.session_id)
    s2 = mgr.create()                                # 新 id，不复用旧对象
    assert s2 is not s


def test_run_task_on_session_driver_and_audit_binding():
    mgr = SessionManager(driver_factory=lambda storage: FakeDriver(
        pages={HOME: HOME_PAGE}, storage=storage))
    session = mgr.create(tenant_id="t9")
    task = base_task(
        session_id=session.session_id,
        tenant_id="t9",
        actions=[Action("type", "input#q", "hello"),
                 Action("extract", "input#q")],
    )
    result = run_task(session.driver, task)
    assert result.status == "COMPLETED"
    assert result.data["input#q"] == "hello"         # 写进本会话命名空间后可读回
    assert session.storage[HOME + "|input#q"] == "hello"
    for ev in result.audit:
        assert ev.get("session_id") in ("bsess-test", session.session_id)
    action_events = [e for e in result.audit if e["type"] == "action"]
    assert action_events[-1]["session_id"] == session.session_id


def test_session_default_driver_factory_is_fake():
    mgr = SessionManager()
    s = mgr.create()
    assert isinstance(s.driver, FakeDriver)
    assert s.created_at
