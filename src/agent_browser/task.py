# coding: utf-8
"""浏览器任务：动作循环 + allowlist 硬拦 + 双熔断（动作数/超时）+ 每动作审计.

红线（确定性系统决定权限，模型只提意图）：

- **allowlist 硬拦**：任何 goto 的目标 host 必须命中 ``domain_allowlist``
  （精确或子域）；越域 → 状态 BLOCKED，导航不发生，审计留 ``block`` 事件；
- **动作数熔断**：执行动作数（含隐式 start_url 首跳）达到 ``max_actions`` 即停；
- **超时熔断**：``timeout_s`` 到点即停；
- **每动作审计**：事件含 tenant_id / session_id / kind / target / ok；
  ``type`` 动作只记 ``value_len``，**输入文本不进审计**。
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

__all__ = [
    "ACTION_KINDS", "STATUS_COMPLETED", "STATUS_BLOCKED", "STATUS_TIMEOUT",
    "STATUS_MAX_ACTIONS", "STATUS_INVALID_ACTION", "STATUS_DRIVER_ERROR",
    "Action", "BrowserTask", "TaskResult", "AuditTrail", "run_task",
    "host_allowed",
]

ACTION_KINDS = ("goto", "click", "type", "extract", "screenshot")

STATUS_COMPLETED = "COMPLETED"
STATUS_BLOCKED = "BLOCKED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_MAX_ACTIONS = "MAX_ACTIONS_EXCEEDED"
STATUS_INVALID_ACTION = "INVALID_ACTION"
STATUS_DRIVER_ERROR = "DRIVER_ERROR"


# ---------------------------------------------------------------------------
# 对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""      # goto: URL；其余: 选择器
    value: str = ""       # type: 输入文本（只进驱动，不进审计）


@dataclass
class BrowserTask:
    start_url: str
    domain_allowlist: list
    max_actions: int = 20
    timeout_s: float = 30.0
    tenant_id: str = "default"
    session_id: str = ""
    actions: list = field(default_factory=list)


@dataclass
class TaskResult:
    status: str
    actions_executed: int
    data: dict
    error: str = ""
    elapsed: float = 0.0
    audit: tuple = ()


class AuditTrail:
    """轻量 append-only 审计（事件列表；ts 为 UTC 墙钟，seq 单调）。

    线程安全（x2fix-R1 加锁）：BrowserService 的 block/session.close/action
    事件从 HTTP 线程与会话执行器线程两路追加（越域复检段内即销毁），
    seq 唯一性与事件序在并发下成立；run_task 单线程路径语义不变。
    """

    def __init__(self):
        self._events = []
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, type, **fields):
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": str(type),
            }
            event.update(fields)
            self._events.append(event)
            return event

    def events(self):
        return tuple(self._events)

    def by_type(self, type):
        return tuple(e for e in self._events if e["type"] == type)


# ---------------------------------------------------------------------------
# allowlist
# ---------------------------------------------------------------------------

def host_allowed(url, allowlist):
    """URL 的 host 是否命中 allowlist（精确或子域；仅 http/https）。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for entry in allowlist:
        domain = str(entry).strip().lower().rstrip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def run_task(driver, task, now_fn=None, audit=None):
    """在给定驱动上执行任务。返回 :class:`TaskResult`（audit 事件附在 .audit）。"""
    if now_fn is None:
        now_fn = time.monotonic
    if audit is None:
        audit = AuditTrail()
    if not task.domain_allowlist:
        # fail-closed：没有 allowlist 的浏览器任务直接拒绝启动
        raise ValueError("domain_allowlist must not be empty (fail-closed)")

    pending = [Action(kind="goto", target=task.start_url)] + list(task.actions)
    started = now_fn()
    executed = 0
    data = {}
    error = ""

    def _finish(status):
        result = TaskResult(
            status=status, actions_executed=executed, data=data, error=error,
            elapsed=now_fn() - started, audit=audit.events(),
        )
        return result

    for action in pending:
        if now_fn() - started > task.timeout_s:
            audit.append("timeout", tenant_id=task.tenant_id, session_id=task.session_id,
                         kind=action.kind, target=action.target, ok=False,
                         reason="TIMEOUT_BUDGET_EXHAUSTED", elapsed=now_fn() - started)
            error = "timeout budget exhausted after %d action(s)" % executed
            return _finish(STATUS_TIMEOUT)
        if executed >= task.max_actions:
            audit.append("max_actions", tenant_id=task.tenant_id,
                         session_id=task.session_id, ok=False,
                         reason="ACTION_BUDGET_EXHAUSTED", max_actions=task.max_actions)
            error = "action budget exhausted (%d)" % task.max_actions
            return _finish(STATUS_MAX_ACTIONS)

        # allowlist 硬拦（导航类动作）
        if action.kind == "goto" and not host_allowed(action.target, task.domain_allowlist):
            audit.append("block", tenant_id=task.tenant_id, session_id=task.session_id,
                         kind="goto", target=action.target, ok=False,
                         reason="ALLOWLIST_VIOLATION")
            error = "URL blocked by allowlist: %s" % action.target
            return _finish(STATUS_BLOCKED)

        if action.kind not in ACTION_KINDS:
            audit.append("error", tenant_id=task.tenant_id, session_id=task.session_id,
                         kind=action.kind, target=action.target, ok=False,
                         reason="INVALID_ACTION")
            error = "unknown action kind: %s" % action.kind
            return _finish(STATUS_INVALID_ACTION)

        try:
            outcome = _execute(driver, action)
            # 真实驱动可能重定向：goto 的最终 URL 也要在 allowlist 内
            if action.kind == "goto" and outcome != action.target and \
                    not host_allowed(outcome, task.domain_allowlist):
                audit.append("block", tenant_id=task.tenant_id,
                             session_id=task.session_id, kind="goto", target=outcome,
                             ok=False, reason="ALLOWLIST_VIOLATION_REDIRECT")
                error = "redirect target blocked by allowlist: %s" % outcome
                return _finish(STATUS_BLOCKED)
        except Exception as exc:  # noqa: BLE001 — 任何驱动异常都熔断本任务
            audit.append("error", tenant_id=task.tenant_id, session_id=task.session_id,
                         kind=action.kind, target=action.target, ok=False,
                         reason="DRIVER_ERROR", detail=str(exc))
            error = "driver error on %s: %s" % (action.kind, exc)
            return _finish(STATUS_DRIVER_ERROR)

        executed += 1
        event_fields = dict(tenant_id=task.tenant_id, session_id=task.session_id,
                            kind=action.kind, target=action.target, ok=True)
        if action.kind == "type":
            event_fields["value_len"] = len(action.value)   # 值不进审计，只记长度
        audit.append("action", **event_fields)

        if action.kind == "extract":
            data[action.target] = outcome
        elif action.kind == "screenshot":
            # 截图字节留在任务进程内存（本仓不上传任何截图）；审计只记长度
            data["screenshot_bytes_len"] = len(outcome)

    return _finish(STATUS_COMPLETED)


def _execute(driver, action):
    if action.kind == "goto":
        return driver.goto(action.target)
    if action.kind == "click":
        return driver.click(action.target)
    if action.kind == "type":
        return driver.type(action.target, action.value)
    if action.kind == "extract":
        return driver.extract(action.target)
    if action.kind == "screenshot":
        return driver.screenshot()
    raise ValueError("unknown action kind: %s" % action.kind)
