# coding: utf-8
"""agent-browser — 原子浏览器能力包（FakeDriver + 实弹 PlaywrightDriver + 池化）."""
from .driver import (
    DRIVER_METHODS,
    BrowserDriver,
    DriverError,
    FakeDriver,
    PageSpec,
    is_browser_driver,
)
from .playwright_driver import (
    DEFAULT_LAUNCH_ARGS,
    DEFAULT_PROFILE_PATH,
    PlaywrightDriver,
    aria_yaml_to_markdown,
    load_launch_profile,
)
from .sessions import (
    BrowserPool,
    BrowserSession,
    PoolClosed,
    PoolExhausted,
    SessionManager,
)
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

__version__ = "0.2.0"

__all__ = [
    "DRIVER_METHODS", "BrowserDriver", "DriverError", "FakeDriver", "PageSpec",
    "is_browser_driver",
    "DEFAULT_LAUNCH_ARGS", "DEFAULT_PROFILE_PATH", "PlaywrightDriver",
    "aria_yaml_to_markdown", "load_launch_profile",
    "BrowserPool", "BrowserSession", "PoolClosed", "PoolExhausted",
    "SessionManager",
    "ACTION_KINDS", "STATUS_BLOCKED", "STATUS_COMPLETED", "STATUS_DRIVER_ERROR",
    "STATUS_INVALID_ACTION", "STATUS_MAX_ACTIONS", "STATUS_TIMEOUT",
    "Action", "AuditTrail", "BrowserTask", "TaskResult", "host_allowed",
    "run_task",
    "__version__",
]
