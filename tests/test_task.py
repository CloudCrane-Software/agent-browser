# coding: utf-8
"""任务执行测试：allowlist 硬拦 / 双熔断 / 审计卫生 / 数据采集."""
from __future__ import annotations

import pytest
from conftest import HOME, SUB, StepClock, base_task, make_driver

from agent_browser import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_DRIVER_ERROR,
    STATUS_INVALID_ACTION,
    STATUS_MAX_ACTIONS,
    STATUS_TIMEOUT,
    Action,
    AuditTrail,
    FakeDriver,
    host_allowed,
    run_task,
)


# ---------------------------------------------------------------- allowlist

def test_completed_with_allowed_goto():
    d = make_driver()
    result = run_task(d, base_task())
    assert result.status == STATUS_COMPLETED
    assert result.actions_executed == 1
    assert d.current_url == HOME
    assert result.audit[0]["type"] == "action"


def test_cross_domain_goto_blocked_hard():
    d = make_driver()
    result = run_task(d, base_task(start_url="https://evil.io/"))
    assert result.status == STATUS_BLOCKED
    assert result.actions_executed == 0
    assert d.current_url == ""                      # 导航未发生
    blocks = result.audit and [e for e in result.audit if e["type"] == "block"]
    assert blocks and blocks[0]["reason"] == "ALLOWLIST_VIOLATION"
    assert blocks[0]["target"] == "https://evil.io/"


def test_subdomain_of_allowed_domain_ok():
    result = run_task(make_driver(), base_task(start_url=SUB))
    assert result.status == STATUS_COMPLETED


def test_lookalike_domains_blocked():
    for url in ("https://notexample.com/", "https://example.com.evil.io/"):
        result = run_task(make_driver(), base_task(start_url=url))
        assert result.status == STATUS_BLOCKED, url


def test_non_http_scheme_blocked():
    result = run_task(make_driver(), base_task(start_url="ftp://example.com/file"))
    assert result.status == STATUS_BLOCKED


def test_redirect_to_disallowed_host_blocked():
    class RedirectingDriver(FakeDriver):
        def goto(self, url):
            super().goto(url)
            self.current_url = "https://evil.io/"   # 模拟真实驱动被重定向
            return self.current_url

    result = run_task(RedirectingDriver(), base_task())
    assert result.status == STATUS_BLOCKED
    blocks = [e for e in result.audit if e["type"] == "block"]
    assert blocks[0]["reason"] == "ALLOWLIST_VIOLATION_REDIRECT"


def test_host_allowed_unit():
    assert host_allowed("https://Example.com/x", ["example.com"])
    assert host_allowed("https://a.b.example.com/", ["example.com"])
    assert not host_allowed("https://notexample.com/", ["example.com"])
    assert not host_allowed("javascript:alert(1)", ["example.com"])
    assert not host_allowed("https://", ["example.com"])


def test_empty_allowlist_fails_closed():
    with pytest.raises(ValueError, match="fail-closed"):
        run_task(make_driver(), base_task(domain_allowlist=[]))


# ---------------------------------------------------------------- 熔断

def test_max_actions_breaker():
    d = make_driver()
    actions = [Action("goto", SUB) for _ in range(5)]
    result = run_task(d, base_task(actions=actions, max_actions=3))
    assert result.status == STATUS_MAX_ACTIONS
    assert result.actions_executed == 3
    assert len(d.action_log) == 3                   # 后续动作未执行
    maxes = [e for e in result.audit if e["type"] == "max_actions"]
    assert maxes and maxes[0]["max_actions"] == 3


def test_timeout_breaker():
    d = make_driver()
    actions = [Action("goto", SUB) for _ in range(6)]
    # StepClock 每次调用 +1s：隐式首跳后每迭代 elapsed=1,2,3,... → timeout_s=3 时执行 3 个动作
    result = run_task(d, base_task(actions=actions, timeout_s=3.0),
                      now_fn=StepClock(step=1.0))
    assert result.status == STATUS_TIMEOUT
    assert result.actions_executed == 3
    timeouts = [e for e in result.audit if e["type"] == "timeout"]
    assert timeouts and timeouts[0]["reason"] == "TIMEOUT_BUDGET_EXHAUSTED"


def test_start_url_counts_toward_action_budget():
    d = make_driver()
    result = run_task(d, base_task(actions=[Action("extract", "h1")], max_actions=1))
    assert result.status == STATUS_MAX_ACTIONS
    assert result.actions_executed == 1             # 只跑了隐式首跳
    assert d.action_log == [("goto", HOME)]


# ---------------------------------------------------------------- 审计卫生

def test_per_action_audit_fields_and_seq():
    actions = [Action("extract", "h1"), Action("click", "a.next")]
    result = run_task(make_driver(), base_task(actions=actions))
    action_events = [e for e in result.audit if e["type"] == "action"]
    assert len(action_events) == 3                  # 隐式首跳 + 2 个动作
    assert [e["seq"] for e in result.audit] == list(range(1, len(result.audit) + 1))
    for ev in action_events:
        assert ev["tenant_id"] == "t1"
        assert ev["session_id"] == "bsess-test"
        assert ev["ok"] is True
        assert ev["kind"] in ("goto", "extract", "click")


def test_type_value_never_in_audit_or_action_log():
    d = make_driver()
    actions = [Action("type", "input#q", "SECRET-TEXT-VALUE-XYZZY")]
    result = run_task(d, base_task(actions=actions))
    assert result.status == STATUS_COMPLETED
    dumped = repr(result.audit)
    assert "SECRET-TEXT-VALUE-XYZZY" not in dumped  # 输入文本不进审计
    type_events = [e for e in result.audit if e["kind"] == "type"]
    assert type_events[0]["value_len"] == len("SECRET-TEXT-VALUE-XYZZY")
    assert ("type", "input#q") in d.action_log      # 动作日志也只有 (kind, selector)
    assert d.storage[HOME + "|input#q"] == "SECRET-TEXT-VALUE-XYZZY"  # 值只落在会话 storage


# ---------------------------------------------------------------- 失败与数据

def test_unknown_action_kind_invalid():
    result = run_task(make_driver(), base_task(actions=[Action("scroll", ".x")]))
    assert result.status == STATUS_INVALID_ACTION
    errs = [e for e in result.audit if e["type"] == "error"]
    assert errs[0]["reason"] == "INVALID_ACTION"


def test_driver_error_caught_and_audited():
    result = run_task(make_driver(), base_task(actions=[Action("click", ".missing")]))
    assert result.status == STATUS_DRIVER_ERROR
    errs = [e for e in result.audit if e["type"] == "error"]
    assert errs[0]["reason"] == "DRIVER_ERROR"
    assert "element not found" in errs[0]["detail"]
    assert "element not found: .missing" in result.error


def test_extract_collects_data():
    actions = [Action("extract", "h1")]
    result = run_task(make_driver(), base_task(actions=actions))
    assert result.status == STATUS_COMPLETED
    assert result.data["h1"] == "Example"


def test_screenshot_meta_only_no_bytes_leak():
    result = run_task(make_driver(), base_task(actions=[Action("screenshot")]))
    assert result.status == STATUS_COMPLETED
    assert result.data["screenshot_bytes_len"] == len(b"FAKE-PNG-BYTES")
    assert "FAKE" not in repr(result.data)          # 字节本体不出任务结果


def test_audit_trail_is_append_only_and_default():
    trail = AuditTrail()
    ev = trail.append("action", kind="goto", ok=True)
    assert ev["seq"] == 1 and "ts" in ev
    assert len(trail.events()) == 1
    assert trail.by_type("action") == (ev,)
