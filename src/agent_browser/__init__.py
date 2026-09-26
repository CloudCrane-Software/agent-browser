# coding: utf-8
"""agent-browser — 原子浏览器能力包（WO-0008；FakeDriver MVP，驱动可插拔）."""
from .driver import (
    DRIVER_METHODS,
    BrowserDriver,
    DriverError,
    FakeDriver,
    PageSpec,
    is_browser_driver,
)
from .sessions import BrowserSession, SessionManager
from .task import (
    ACTION_KINDS,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_DRIVER_ERROR,
    STATUS_INVALID_ACTION,
    STATUS_MAX_ACTIONS,
    STATUS_TIMEOUT,
    Action,
    AuditTrail,
    BrowserTask,
    TaskResult,
    host_allowed,
    run_task,
)

__version__ = "0.1.0"

__all__ = [
    "DRIVER_METHODS", "BrowserDriver", "DriverError", "FakeDriver", "PageSpec",
    "is_browser_driver",
    "BrowserSession", "SessionManager",
    "ACTION_KINDS", "STATUS_BLOCKED", "STATUS_COMPLETED", "STATUS_DRIVER_ERROR",
    "STATUS_INVALID_ACTION", "STATUS_MAX_ACTIONS", "STATUS_TIMEOUT",
    "Action", "AuditTrail", "BrowserTask", "TaskResult", "host_allowed",
    "run_task",
    "__version__",
]
