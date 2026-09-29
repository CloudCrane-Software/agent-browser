# coding: utf-8
"""PlaywrightDriver 协议层测试——注入 fake playwright 对象（零真浏览器）.

覆盖：懒加载与缺包错误 / 六动作语义 / BP flags 基线 / persistent context（池化
路径）/ L1 aria_snapshot→markdown 与 text_content 降级 / 双保险关闭幂等 /
与 run_task 的组合。
"""
from __future__ import annotations

import pytest
from conftest import HOME, base_task

import agent_browser.playwright_driver as pwd
from agent_browser import (
    DEFAULT_LAUNCH_ARGS,
    DriverError,
    PlaywrightDriver,
    is_browser_driver,
    run_task,
)


# ---------------------------------------------------------------- fake 表面

ARIA_YAML = '- heading "Example Domain" [level=1] [ref=e1]\n- paragraph: This domain is for use in illustrative examples'


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.calls = []
        self.aria = ARIA_YAML if selector == "body" else None
        self.text = "Example Domain"

    def click(self, timeout=None):
        self.calls.append(("click", timeout))

    def fill(self, text, timeout=None):
        self.calls.append(("fill", text, timeout))

    def aria_snapshot(self):
        if self.aria is None:
            raise RuntimeError("aria snapshot unsupported here")
        return self.aria

    def text_content(self, timeout=None):
        self.calls.append(("text_content", timeout))
        return self.text


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.goto_calls = []
        self.screenshot_calls = 0
        self._locators = {}

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url                                   # 无重定向：最终=目标
        return None

    def locator(self, selector):
        if selector not in self._locators:
            self._locators[selector] = FakeLocator(self, selector)
        return self._locators[selector]

    def screenshot(self):
        self.screenshot_calls += 1
        return b"PNG-BYTES-0" * 3


class FakeContext:
    def __init__(self, with_page=True):
        self.pages = [FakePage()] if with_page else []
        self.closed = False
        self.default_timeouts = []

    def set_default_timeout(self, ms):
        self.default_timeouts.append(ms)

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.context = FakeContext()
        self.closed = False

    def new_context(self):
        return self.context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launch_calls = []               # (headless, args)
        self.persistent_calls = []           # (user_data_dir, headless, args)
        self.browser = FakeBrowser()

    def launch(self, headless=None, args=None):
        self.launch_calls.append((headless, list(args or [])))
        return self.browser

    def launch_persistent_context(self, user_data_dir, headless=None, args=None):
        self.persistent_calls.append((user_data_dir, headless, list(args or [])))
        return FakeContext(with_page=False)   # persistent：初始无页 → new_page()

    def context_obj(self):
        return self.browser.context


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakePlaywrightHandle:
    """等价 sync_playwright() 返回物：.start() → FakePlaywright。"""

    def __init__(self):
        self.playwright = FakePlaywright()

    def start(self):
        return self.playwright


def make_driver(**kw):
    handle = FakePlaywrightHandle()
    driver = PlaywrightDriver(playwright_factory=lambda: handle, **kw)
    return driver, handle


# ---------------------------------------------------------------- 懒加载

def test_missing_playwright_raises_actionable_error(monkeypatch):
    def _boom(_self):
        raise ImportError("No module named 'playwright'")
    monkeypatch.setattr(pwd.PlaywrightDriver, "_import_playwright", _boom)
    driver = PlaywrightDriver()               # 不注入 factory → 走真实导入路径
    with pytest.raises(DriverError, match="playwright"):
        driver.goto(HOME)                     # 首个动作才触发懒加载
    assert driver._page is None


def test_import_is_lazy_no_playwright_at_module_import():
    # 本仓 import agent_browser 不允许连带 import playwright（零硬依赖）：
    # 模块级符号表里没有 playwright 绑定（只有函数内的局部 import）
    assert not hasattr(pwd, "playwright")


def test_protocol_conformance():
    driver, _ = make_driver()
    assert is_browser_driver(driver)
    assert PlaywrightDriver.driver_name == "playwright"


# ---------------------------------------------------------------- 启动与 flags

def test_launch_uses_bp_flags_baseline():
    driver, handle = make_driver()
    driver.goto(HOME)
    (headless, args), = handle.playwright.chromium.launch_calls
    assert headless is True
    for flag in ("--no-sandbox", "--disable-dev-shm-usage", "--no-zygote",
                 "--renderer-process-limit=2", "--password-store=basic",
                 "--use-mock-keychain", "--disable-extensions"):
        assert flag in args, flag
    assert set(args) == set(DEFAULT_LAUNCH_ARGS)      # 默认=BP §2.5 基线逐项


def test_launch_profile_overrides_args():
    driver, handle = make_driver(launch_profile={"args": ["--custom-flag=1"]})
    driver.goto(HOME)
    (_headless, args), = handle.playwright.chromium.launch_calls
    assert args == ["--custom-flag=1"]


def test_persistent_context_for_user_data_dir():
    driver, handle = make_driver(user_data_dir="/tmp/udd-test")
    driver.goto(HOME)
    calls = handle.playwright.chromium.persistent_calls
    assert len(calls) == 1 and calls[0][0] == "/tmp/udd-test"
    assert calls[0][1] is True and "--no-sandbox" in calls[0][2]
    assert handle.playwright.chromium.launch_calls == []   # 不走 launch()


def test_default_timeout_propagated():
    driver, handle = make_driver(default_timeout_ms=12345)
    driver.goto(HOME)
    ctx = handle.playwright.chromium.context_obj()
    assert ctx.default_timeouts == [12345]


# ---------------------------------------------------------------- 六动作

def test_goto_returns_final_url_and_records_wait_until():
    driver, handle = make_driver()
    final = driver.goto(HOME)
    assert final == HOME
    page = handle.playwright.chromium.context_obj().pages[0]
    assert page.goto_calls[0][1] == "domcontentloaded"
    assert driver.action_log == [("goto", HOME)]


def test_click_and_type_route_to_locator():
    driver, handle = make_driver()
    driver.goto(HOME)
    assert driver.click("h1") == {"clicked": "h1"}
    assert driver.type("input#q", "secret") == {"typed_len": 6,
                                                "selector": "input#q"}
    page = handle.playwright.chromium.context_obj().pages[0]
    assert page.locator("h1").calls == [("click", driver.default_timeout_ms)]
    # 输入文本不进动作日志（审计红线）
    assert ("type", "input#q") in driver.action_log
    assert all("secret" not in repr(log) for log in [driver.action_log])


def test_type_action_log_never_contains_text():
    driver, _ = make_driver()
    driver.goto(HOME)
    driver.type("input#q", "TOPSECRET-XYZ")
    assert "TOPSECRET-XYZ" not in repr(driver.action_log)


def test_extract_l1_aria_snapshot_to_markdown():
    driver, _ = make_driver()
    driver.goto(HOME)
    text = driver.extract("body")
    assert "heading" in text and "Example Domain" in text
    assert "[ref=e1]" in text                        # 动作句柄保留给上层 agent
    assert text.startswith("- heading")              # markdown 树形


def test_extract_falls_back_to_text_content_when_snapshot_unavailable():
    driver, handle = make_driver()
    driver.goto(HOME)
    text = driver.extract("h1")                      # FakeLocator: h1 无 aria 快照
    assert text == "Example Domain"
    page = handle.playwright.chromium.context_obj().pages[0]
    assert ("text_content", driver.default_timeout_ms) in page.locator("h1").calls


def test_extract_bad_yaml_passthrough(monkeypatch):
    monkeypatch.setattr(pwd, "aria_yaml_to_markdown",
                        lambda y: "::bad::" if "::" in y else y)
    driver, _ = make_driver()
    driver.goto(HOME)
    assert driver.extract("body") == ARIA_YAML       # 好 YAML → markdown 树
    # 坏 YAML：aria_yaml_to_markdown 内部解析失败原样透传
    assert pwd.aria_yaml_to_markdown("- a: [unclosed") == "- a: [unclosed"


def test_screenshot_returns_bytes():
    driver, handle = make_driver()
    driver.goto(HOME)
    data = driver.screenshot()
    assert isinstance(data, bytes) and data.startswith(b"PNG-BYTES")
    assert handle.playwright.chromium.context_obj().pages[0].screenshot_calls == 1


def test_aria_ref_selector_normalized():
    driver, handle = make_driver()
    driver.goto(HOME)
    driver.click("[ref=e12]")
    page = handle.playwright.chromium.context_obj().pages[0]
    assert "aria-ref=e12" in page._locators          # [ref=eN] → aria-ref=eN


# ---------------------------------------------------------------- 关闭语义

def test_close_double_insurance_and_idempotent():
    driver, handle = make_driver()
    driver.goto(HOME)
    ctx = handle.playwright.chromium.context_obj()
    driver.close()
    assert driver.closed is True
    assert ctx.closed is True                        # 第一保险：context.close
    assert handle.playwright.chromium.browser.closed is True  # 第二：browser.close
    assert handle.playwright.stopped is True         # 第三：playwright.stop
    driver.close()                                   # 幂等
    assert len(ctx.default_timeouts) == 1            # 无二次动作


def test_closed_driver_raises_on_any_action():
    driver, _ = make_driver()
    driver.close()
    for call in (lambda: driver.goto(HOME), lambda: driver.click("h1"),
                 lambda: driver.extract("body"), lambda: driver.screenshot()):
        with pytest.raises(DriverError, match="closed"):
            call()


def test_close_errors_collected_not_raised():
    driver, handle = make_driver()
    driver.goto(HOME)

    def _boom():
        raise RuntimeError("context already gone")
    handle.playwright.chromium.browser.close = _boom
    driver.close()                                   # 尽力关闭：不抛
    assert driver.closed is True
    assert any("browser" in e for e in driver.close_errors)


# ---------------------------------------------------------------- 与 task 组合

def test_run_task_on_playwright_driver_end_to_end():
    driver, _ = make_driver()
    task = base_task(actions=[])                     # 单 goto 首跳
    result = run_task(driver, task)
    assert result.status == "COMPLETED"
    assert driver.current_url == HOME


def test_goto_failure_wrapped_as_driver_error():
    driver, handle = make_driver()
    driver.goto(HOME)
    page = handle.playwright.chromium.context_obj().pages[0]

    def _boom(url, wait_until=None, timeout=None):
        raise TimeoutError("navigation exceeded")
    page.goto = _boom
    with pytest.raises(DriverError, match="goto failed"):
        driver.goto("https://example.com/slow")
