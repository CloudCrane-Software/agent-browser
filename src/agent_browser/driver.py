# coding: utf-8
"""可插拔浏览器驱动：``BrowserDriver`` 协议 + 内存 ``FakeDriver``.

- 协议六动作：goto / click / type / extract / screenshot / close；
- 真实驱动（如 playwright）未来实现同一协议即可接入——本仓当前**只有**
  FakeDriver（内存实现，测试/语义演示用），不驱动任何真实浏览器；
- ``FakeDriver.type`` 把输入写入 ``storage``（会话命名空间，见 sessions.py），
  且动作日志只记 ``(kind, selector)``，**不记输入文本**（与审计红线一致）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol, runtime_checkable

__all__ = ["BrowserDriver", "DriverError", "FakeDriver", "PageSpec",
           "DRIVER_METHODS", "is_browser_driver"]

DRIVER_METHODS = ("goto", "click", "type", "extract", "screenshot", "close")


class DriverError(RuntimeError):
    """驱动层失败（元素不存在 / 已关闭 / 无当前页面等）。"""


@runtime_checkable
class BrowserDriver(Protocol):
    """浏览器驱动协议——playwright 等真实驱动的接入缝（TODO，见 README）。"""

    def goto(self, url: str) -> str:  # 返回最终 URL（重定向后）
        ...

    def click(self, selector: str) -> Dict[str, Any]:
        ...

    def type(self, selector: str, text: str) -> Dict[str, Any]:
        ...

    def extract(self, selector: str) -> str:
        ...

    def screenshot(self) -> bytes:
        ...

    def close(self) -> None:
        ...


def is_browser_driver(obj) -> bool:
    """结构化检查：具备协议六方法即视为 BrowserDriver。"""
    return all(callable(getattr(obj, name, None)) for name in DRIVER_METHODS)


@dataclass
class PageSpec:
    """FakeDriver 的页面定义：可用元素选择器 + 元素文本。"""
    elements: tuple = ()
    texts: Dict[str, str] = field(default_factory=dict)


class FakeDriver:
    """内存驱动：无网络、无截图、无真实浏览器；语义与协议一致."""

    def __init__(self, pages=None, storage=None):
        self.pages: Dict[str, PageSpec] = dict(pages or {})
        self.storage: Dict[str, str] = storage if storage is not None else {}
        self.current_url: str = ""
        self.closed: bool = False
        self.action_log: list = []           # (kind, selector_or_url)，不含输入文本

    # ---------------------------------------------------------------- 协议

    def goto(self, url: str) -> str:
        if self.closed:
            raise DriverError("driver is closed")
        self.current_url = url
        self.action_log.append(("goto", url))
        return url

    def click(self, selector: str) -> Dict[str, Any]:
        self._require_element(selector)
        self.action_log.append(("click", selector))
        return {"clicked": selector}

    def type(self, selector: str, text: str) -> Dict[str, Any]:
        self._require_element(selector)
        self.action_log.append(("type", selector))   # 刻意不记 text
        self.storage[self._key(selector)] = text
        return {"typed_len": len(text), "selector": selector}

    def extract(self, selector: str) -> str:
        self._require_element(selector)
        self.action_log.append(("extract", selector))
        page = self.pages.get(self.current_url)
        if page is not None and selector in page.texts:
            return page.texts[selector]
        return self.storage.get(self._key(selector), "")

    def screenshot(self) -> bytes:
        self._ensure_open()
        self.action_log.append(("screenshot", ""))
        return b"FAKE-PNG-BYTES"             # 内存占位，永不落盘/上传

    def close(self) -> None:
        self.closed = True

    # ---------------------------------------------------------------- 内部

    def _key(self, selector: str) -> str:
        return "%s|%s" % (self.current_url, selector)

    def _ensure_open(self):
        if self.closed:
            raise DriverError("driver is closed")
        if not self.current_url:
            raise DriverError("no current page (goto first)")

    def _require_element(self, selector: str):
        self._ensure_open()
        page = self.pages.get(self.current_url)
        if page is None:
            raise DriverError("no page spec for %s" % self.current_url)
        if selector not in page.elements:
            raise DriverError("element not found: %s" % selector)
