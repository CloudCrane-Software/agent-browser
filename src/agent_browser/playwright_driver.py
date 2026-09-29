# coding: utf-8
"""实弹驱动：``PlaywrightDriver`` —— :class:`BrowserDriver` 协议的真实实现.

蓝图依据（FINAL_agent_browser_blueprint.md，下称 BP）：

- **引擎**：Playwright（Apache-2.0）+ chrome-headless-shell；playwright v1.49+
  ``headless=True`` 默认即 headless-shell（BP §2.1，比完整 Chrome 省 ~40% 内存）；
- **launch 参数基线**：照抄 BP §2.5 flags（``--no-sandbox`` …
  ``--disable-dev-shm-usage``）——见 :data:`DEFAULT_LAUNCH_ARGS`；
  线1 的 ``launch-profile.json`` 若存在则优先生效（查找顺序：构造参数 > 环境变量
  ``AB_LAUNCH_PROFILE`` > /opt/gpumachine/agent-browser/launch-profile.json；
  **截至本提交线1 未交付该文件**，故默认即 BP 基线，注释保留 profile 路径指针）；
- **extract 走 BP L1 结构化优先**（BP §2.3）：``locator.aria_snapshot()``（YAML
  ARIA 树，带 [ref=eN] 动作句柄）→ 解析 YAML 转 markdown 树；PyYAML 缺失时原样
  返回 YAML 文本（仍是结构化树）；快照不可用才降级 ``text_content()``；
- **僵尸治理**（BP §2.5，``driver.close()`` 只关标签不杀进程的教训）：
  :meth:`PlaywrightDriver.close` 做 context.close() + browser.close() +
  playwright.stop() 三层双保险；进程树兜底杀在看门狗层（sessions.BrowserPool）；
- **懒加载**：本模块 import 时**不**导入 playwright（缺包不破坏 FakeDriver 测试）；
  首个动作才 ``_import_playwright()``，缺包时抛出带安装指引的 :class:`DriverError`。

线程亲和性：playwright sync API 要求所有调用与 ``start()`` 同线程；跨线程使用方
（如 server.py）须把单会话动作固定到单线程执行器，见 server.py 的 per-session
executor。本类自身不加锁。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .driver import DriverError

__all__ = [
    "PlaywrightDriver",
    "DEFAULT_LAUNCH_ARGS",
    "DEFAULT_PROFILE_PATH",
    "load_launch_profile",
    "aria_yaml_to_markdown",
]

# BP §2.5 flags 基线（OpenClaw 沙箱浏览器默认参数，一手文档可抄作业）。
# 注：BP 基线中的 --headless=new 不在此列——playwright 用 launch(headless=True)
# 表达同一语义（v1.49+ 即 headless-shell），显式传 --headless 反而冲突。
# 补充项 --process-per-site / --disable-site-isolation 属条件启用，不放默认。
DEFAULT_LAUNCH_ARGS = [
    "--no-sandbox",                    # 容器边界接管隔离（BP §2.5）
    "--disable-setuid-sandbox",
    "--disable-3d-apis",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--renderer-process-limit=2",
    "--no-zygote",
    "--disable-dev-shm-usage",         # /dev/shm 64MB 坑（BP §4.1 四坑之二）
    "--password-store=basic",          # 登录态谱系：Linux profile 迁移同款（BP §2.2）
    "--use-mock-keychain",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--no-first-run",
]

# 线1 交付物落点（约定）：launch-profile.json = {"args": [...], "timeout_ms": N,
# "wait_until": "..."}。线1 未完成时本路径不存在 → 落回 DEFAULT_LAUNCH_ARGS。
DEFAULT_PROFILE_PATH = "/opt/gpumachine/agent-browser/launch-profile.json"

_REF_SELECTOR = re.compile(r"^\[ref=([A-Za-z0-9_.-]+)\]$")


def load_launch_profile(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """读线1 的 launch-profile.json；不存在/损坏 → None（调用方落回默认 flags）.

    查找顺序：显式 path > env AB_LAUNCH_PROFILE > DEFAULT_PROFILE_PATH。
    返回 dict 形如 {"args": [...], "timeout_ms": 30000, "wait_until": "..."}。
    """
    candidates = [path, os.environ.get("AB_LAUNCH_PROFILE"), DEFAULT_PROFILE_PATH]
    for cand in candidates:
        if not cand:
            continue
        try:
            with open(cand, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue                                  # 不存在/坏 JSON → 下一候选
        if isinstance(data, dict):
            return data
    return None


def _resolve_selector(selector: str) -> str:
    """aria ref 句柄与常规选择器统一归一.

    ``[ref=e12]`` → ``aria-ref=e12``（playwright aria snapshot 回指句柄；
    引擎侧语法以部署期实测为准 [待]）；其余原样透传（CSS/XPath/text 引擎）。
    """
    sel = (selector or "").strip()
    if not sel:
        return "body"
    m = _REF_SELECTOR.match(sel)
    if m:
        return "aria-ref=%s" % m.group(1)
    return sel


def aria_yaml_to_markdown(yaml_text: str) -> str:
    """BP L1 结构化：aria_snapshot 的 YAML 树 → markdown 缩进树.

    PyYAML 缺失或解析失败时**原样返回 YAML 文本**（YAML 本身即结构化树，
    含 [ref=eN] 句柄）——绝不为省依赖引入硬依赖。
    """
    try:
        import yaml  # 可选依赖：缺则透传原文
    except ImportError:
        return yaml_text
    try:
        tree = yaml.safe_load(yaml_text)
    except Exception:                                 # noqa: BLE001 — 坏 YAML 透传
        return yaml_text
    if tree is None:
        return ""
    lines: List[str] = []

    def _emit(value, depth):
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    lines.append("  " * depth + "- %s:" % key)
                    _emit(child, depth + 1)
                else:
                    lines.append("  " * depth + "- %s: %s" % (key, _scalar(child)))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    _emit(item, depth)
                else:
                    lines.append("  " * depth + "- %s" % _scalar(item))
        else:
            lines.append("  " * depth + "- %s" % _scalar(value))

    def _scalar(v):
        return str(v).strip() if v is not None else ""

    _emit(tree, 0)
    return "\n".join(lines)


class PlaywrightDriver:
    """真实浏览器驱动（playwright + chrome-headless-shell）.

    与 :class:`~agent_browser.driver.FakeDriver` 同协议：goto 返回重定向后最终
    URL、click/type 返回 dict、extract 返回 str、screenshot 返回 bytes、close
    幂等；``closed`` 属性与审计 ``action_log``（只记 (kind, selector)，不记输入
    文本）语义一致。
    """

    driver_name = "playwright"

    def __init__(self, launch_profile: Optional[Dict[str, Any]] = None,
                 user_data_dir: Optional[str] = None,
                 default_timeout_ms: int = 30000,
                 wait_until: str = "domcontentloaded",
                 playwright_factory=None):
        # launch_profile=None → 此刻按查找序读线1 profile；读不到落回 BP 基线
        self.launch_profile: Dict[str, Any] = dict(launch_profile) if launch_profile \
            else (load_launch_profile() or {})
        self.user_data_dir = user_data_dir            # 池化隔离：每会话独立目录
        self.default_timeout_ms = int(default_timeout_ms)
        self.wait_until = wait_until
        self._playwright_factory = playwright_factory  # 测试缝：注入 fake playwright
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self.closed = False
        self.close_errors: List[str] = []              # 关闭阶段非致命失败留痕
        self.action_log: list = []                     # (kind, selector)，不含文本

    # ---------------------------------------------------------------- 生命周期

    def _import_playwright(self):
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError as exc:
            raise DriverError(
                "playwright 未安装：先 `pip install 'playwright>=1.49'`，"
                "再 `playwright install chromium-headless-shell`（部署目标："
                "GPU 机 venv，BP §2.1）；原错误：%s" % exc) from exc
        import playwright
        return playwright

    def _launch_args(self) -> List[str]:
        args = self.launch_profile.get("args")
        return list(args) if isinstance(args, list) and args else list(DEFAULT_LAUNCH_ARGS)

    def start(self) -> "PlaywrightDriver":
        if self.closed:
            raise DriverError("driver is closed")
        if self._page is not None:
            return self
        try:
            if self._playwright_factory is not None:  # 测试缝：免真 playwright
                factory = self._playwright_factory
            else:
                factory = self._import_playwright().sync_playwright
            self._pw = factory().start()
            chromium = self._pw.chromium
            args = self._launch_args()
            if self.user_data_dir:
                # 池化路径：persistent context 自带 user-data-dir 隔离（cookie/
                # storage 全隔离），context 即宿主，browser=None（BP §2.2 谱系）
                self._context = chromium.launch_persistent_context(
                    self.user_data_dir, headless=True, args=args)
                self._browser = None
            else:
                self._browser = chromium.launch(headless=True, args=args)
                self._context = self._browser.new_context()
            self._context.set_default_timeout(self.default_timeout_ms)
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
        except DriverError:
            self._teardown()
            raise
        except Exception as exc:                      # noqa: BLE001 — 归一为驱动错误
            self._teardown()
            raise DriverError("browser launch failed: %s" % exc) from exc
        return self

    def _ensure_started(self):
        if self.closed:
            raise DriverError("driver is closed")
        if self._page is None:
            self.start()

    def close(self) -> None:
        """双保险关闭（BP §2.5）：context → browser → playwright.stop().

        幂等；各层失败不抛（尽力关闭语义），错误留 :attr:`close_errors`；
        残余进程由 BrowserPool 看门狗按 user-data-dir 标记兜底收割。
        """
        if self.closed:
            return
        self.closed = True
        self._teardown()

    def _teardown(self):
        for name, closer in (
                ("context", lambda: self._context is not None and self._context.close()),
                ("browser", lambda: self._browser is not None and self._browser.close()),
                ("playwright", lambda: self._pw is not None and self._pw.stop())):
            try:
                closer()
            except Exception as exc:                  # noqa: BLE001 — 尽力关闭
                self.close_errors.append("%s: %s" % (name, exc))
        self._page = None
        self._context = None
        self._browser = None
        self._pw = None

    # ---------------------------------------------------------------- 协议六动作

    def goto(self, url: str) -> str:
        self._ensure_started()
        try:
            self._page.goto(url, wait_until=self.wait_until,
                            timeout=self.default_timeout_ms)
        except Exception as exc:                      # noqa: BLE001
            raise DriverError("goto failed: %s" % exc) from exc
        self.action_log.append(("goto", url))
        return self._page.url                         # 重定向后的最终 URL

    def click(self, selector: str) -> Dict[str, Any]:
        self._ensure_started()
        loc = self._page.locator(_resolve_selector(selector))
        try:
            loc.click(timeout=self.default_timeout_ms)
        except Exception as exc:                      # noqa: BLE001
            raise DriverError("click failed on %s: %s" % (selector, exc)) from exc
        self.action_log.append(("click", selector))
        return {"clicked": selector}

    def type(self, selector: str, text: str) -> Dict[str, Any]:
        self._ensure_started()
        loc = self._page.locator(_resolve_selector(selector))
        try:
            loc.fill(text, timeout=self.default_timeout_ms)
        except Exception as exc:                      # noqa: BLE001
            raise DriverError("type failed on %s: %s" % (selector, exc)) from exc
        self.action_log.append(("type", selector))    # 刻意不记 text（审计红线）
        return {"typed_len": len(text), "selector": selector}

    def extract(self, selector: str = "") -> str:
        """BP L1 结构化优先：aria_snapshot → markdown；失败降级 text_content."""
        self._ensure_started()
        loc = self._page.locator(_resolve_selector(selector))
        try:
            snapshot = loc.aria_snapshot()
        except Exception:                             # noqa: BLE001 — L1 不可用即降级
            snapshot = None
        if snapshot:
            self.action_log.append(("extract", selector))
            return aria_yaml_to_markdown(snapshot)
        try:
            text = loc.text_content(timeout=self.default_timeout_ms)
        except Exception as exc:                      # noqa: BLE001
            raise DriverError("extract failed on %s: %s" % (selector, exc)) from exc
        self.action_log.append(("extract", selector))
        return text or ""

    def screenshot(self) -> bytes:
        self._ensure_started()
        try:
            data = self._page.screenshot()
        except Exception as exc:                      # noqa: BLE001
            raise DriverError("screenshot failed: %s" % exc) from exc
        self.action_log.append(("screenshot", ""))
        return data

    # ---------------------------------------------------------------- 兼容缝

    @property
    def current_url(self) -> str:
        """与 FakeDriver 同名只读（有页面时返回真实 URL）。"""
        if self._page is None:
            return ""
        return self._page.url
