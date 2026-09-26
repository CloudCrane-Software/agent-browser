# coding: utf-8
"""pytest 共享夹具：src 路径 + 步进时钟 + 常用页面/任务构造器."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_browser import BrowserTask, FakeDriver, PageSpec  # noqa: E402

HOME = "https://example.com/"
SUB = "https://api.example.com/v1"

HOME_PAGE = PageSpec(elements=("input#q", "h1", "a.next"),
                     texts={"h1": "Example"})


class StepClock:
    """每次调用前进 step 秒（run_task 每个循环迭代恰好调用一次 now_fn）。"""

    def __init__(self, step=0.001):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def make_driver(pages=None):
    return FakeDriver(pages=pages or {HOME: HOME_PAGE})


def base_task(actions=None, **kw):
    defaults = dict(
        start_url=HOME,
        domain_allowlist=["example.com"],
        max_actions=10,
        timeout_s=30.0,
        tenant_id="t1",
        session_id="bsess-test",
    )
    defaults.update(kw)
    return BrowserTask(actions=list(actions or []), **defaults)
