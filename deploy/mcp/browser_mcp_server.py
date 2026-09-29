#!/usr/bin/env python3
# coding: utf-8
"""browser-mcp · CloudCrane 线4：agent-browser 能力 → jiuwenswarm MCP 工具面挂载.

把线2 无头浏览器服务（agent-browser HTTP API，tailnet-only）与线3 搜索/抓取
端点包装成 MCP tools，供 jiuwenswarm agent 直接调用。

设计纪律（工单线4 口径）：
- **零重复实现**：本文件不含任何浏览器/搜索/抓取逻辑，只做 HTTP 翻译——
  每个工具就是一个端点调用 + 错误透传；allowlist fail-closed、重定向落点
  复检、审计、type 只回长度等语义全部由线2/线3 服务端负责并已各自测试。
- **错误不吞**：非 2xx 一律 ToolError 透传服务端错误体
  （ALLOWLIST_VIOLATION / POOL_UNAVAILABLE / TIMEOUT / ALLOWLIST_VIOLATION_REDIRECT
  …），让 agent 看到真实原因，绝不静默重试。
- **降级不冒充**：/search、/fetch 来自线3（PR #3 待合并），线2 服务物理并入前
  这两个端点返回 404——browser_search/browser_fetch 此时如实报 NOT_DEPLOYED，
  绝不返回假结果。

挂载（jiuwenswarm ``config.yaml`` → ``mcp.servers``，transport=stdio，
消费方 jiuwenswarm/agents/swarm/assembly.py 经 build_mcp_server_config 装载）：

    mcp:
      servers:
        - name: browser
          enabled: true
          transport: stdio
          command: /opt/gpumachine/jiuwenswarm/venv/bin/python
          args: ["/opt/gpumachine/agent-browser/mcp/browser_mcp_server.py"]
          env:
            AB_BROWSER_URL: "http://100.64.0.7:8125"

依赖：fastmcp（jiuwenswarm venv 实测 2.14.7 在位）；HTTP 客户端全 stdlib。
运行面：jiuwenswarm-app（root）按 stdio 拉起本进程 → 转调 GPU 机 tailnet 地址
上的 agent-browser 服务（100.64.0.7:8125，经 tailnet 环回可达）。

Higress 判断（工单口径：Higress 不接，依据如下）：浏览器不是模型流量——
本工具面在执行面本地（stdio 由 AgentServer 进程拉起），上游是 GPU 机
tailnet 上的浏览器服务，全链路无一次 LLM API 调用；Higress 是体系内
**唯一模型入口**（模型路由唯一决策点），浏览器工具面不构成第二个模型
端点，也不该经模型网关绕行。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

try:                                    # fastmcp 2.x 的图片包装类型
    from fastmcp.utilities.types import Image
except ImportError:                     # pragma: no cover — 版本漂移兜底
    Image = None                        # type: ignore[assignment]

BROWSER_URL = os.environ.get("AB_BROWSER_URL", "http://100.64.0.7:8125").rstrip("/")
HTTP_TIMEOUT_S = float(os.environ.get("AB_MCP_HTTP_TIMEOUT_S", "60"))

# 线3 search/fetch 未并入运行服务时的如实降级语（不冒充成功）
_NOT_DEPLOYED = (
    "NOT_DEPLOYED: /search /fetch 端点来自线3（agent-browser PR #3），"
    "尚未物理并入运行中的 agent-browser 服务——本端点暂不可用。"
    "可改走真浏览器路径：browser_open(allowlist) → browser_goto → browser_extract。"
)

mcp = FastMCP(name="browser")


# ---------------------------------------------------------------------------
# HTTP 翻译层（唯一的实现面：一个函数 + 错误透传）
# ---------------------------------------------------------------------------

def _call(method: str, path: str, payload: dict | None = None):
    """调 agent-browser HTTP API；返回 (status, json_dict 或 png_bytes)。

    非 2xx → ToolError 透传服务端错误体；连接失败/超时 → ToolError 带服务地址。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
        if payload is not None else None
    req = urllib.request.Request(BROWSER_URL + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "")
            if ctype.startswith("image/"):
                return resp.status, raw
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            detail = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            detail = body.decode("utf-8", "replace")[:500]
        raise ToolError("agent-browser %s %s → HTTP %s: %s"
                        % (method, path, exc.code, detail)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ToolError("agent-browser 服务不可达（%s）：%r——"
                        "确认 GPU 机 agent-browser.service 在跑且经 tailnet 可达"
                        % (BROWSER_URL, reason)) from None


# 会话 id 白名单：服务端签发形如 bsess-<hex>；此处防 URL 路径注入
# （Mimosa finding:141e60d28 SSRF hardening——id 只进固定前缀后的 path 段）
_SID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _need(session_id: str, name: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ToolError("%s required（先 browser_open 创建会话）" % name)
    sid = session_id.strip()
    if not _SID_OK.match(sid):
        raise ToolError("illegal %s（只允许 [A-Za-z0-9._-]，"
                        "应以 browser_open 返回值为准）" % name)
    return sid


# ---------------------------------------------------------------------------
# 会话生命周期
# ---------------------------------------------------------------------------

@mcp.tool
def browser_health() -> dict:
    """探活 agent-browser 服务：返回健康与浏览器池状态（不占用会话配额）。"""
    _status, body = _call("GET", "/health")
    return body


@mcp.tool
def browser_open(allowlist: list[str], url: str = "") -> dict:
    """创建浏览器会话，返回 {"session_id": ...}。

    allowlist 必填且非空：可访问的域名列表（精确或子域后缀匹配，仅 http/https，
    例如 ["example.com", "docs.python.org"]）——服务端 fail-closed，重定向落点
    也复检。url 可选：创建后立刻首跳（首跳被 allowlist 拦会话即销毁）。
    会话占用池配额（上限 8），用完务必 browser_close 归还。
    """
    if not isinstance(allowlist, list) or not allowlist or \
            not all(isinstance(a, str) and a.strip() for a in allowlist):
        raise ToolError("allowlist 必须是非空域名列表（fail-closed，"
                        "例如 [\"example.com\"]）")
    payload: dict[str, Any] = {"allowlist": allowlist}
    if isinstance(url, str) and url.strip():
        payload["url"] = url.strip()
    _status, body = _call("POST", "/session", payload)
    return body


@mcp.tool
def browser_close(session_id: str) -> dict:
    """关闭并归还浏览器会话（每次 browser_open 配对调用一次）。"""
    _status, body = _call("POST",
                          "/session/%s/close" % _need(session_id, "session_id"), {})
    return body


# ---------------------------------------------------------------------------
# 页面动作（转调线2 六动作）
# ---------------------------------------------------------------------------

@mcp.tool
def browser_goto(session_id: str, url: str) -> dict:
    """导航到 url（须在会话 allowlist 内，含重定向落点），返回 {"url": 最终URL}。"""
    if not isinstance(url, str) or not url.strip():
        raise ToolError("url required")
    _status, body = _call("POST", "/session/%s/goto" % _need(session_id, "session_id"),
                          {"url": url.strip()})
    return body


@mcp.tool
def browser_click(session_id: str, selector: str) -> dict:
    """点击元素。selector 支持 CSS / Playwright 定位语法（含 [ref=eN] aria 句柄），返回 {"clicked": selector}。"""
    if not isinstance(selector, str) or not selector.strip():
        raise ToolError("selector required")
    _status, body = _call("POST", "/session/%s/click" % _need(session_id, "session_id"),
                          {"selector": selector.strip()})
    return body


@mcp.tool
def browser_type(session_id: str, selector: str, text: str) -> dict:
    """向输入元素键入 text。返回 {"typed_len": n, "selector"}——文本不回显不进审计（仓红线）。"""
    if not isinstance(selector, str) or not selector.strip():
        raise ToolError("selector required")
    if not isinstance(text, str):
        raise ToolError("text must be a string")
    _status, body = _call("POST", "/session/%s/type" % _need(session_id, "session_id"),
                          {"selector": selector.strip(), "text": text})
    return body


@mcp.tool
def browser_extract(session_id: str, selector: str = "") -> dict:
    """提取页面结构化内容（BP L1：aria_snapshot YAML→markdown，保留 [ref=eN] 动作句柄；失败降级纯文本），返回 {"text": ...}。

    selector 可选（空=整页）；先 browser_extract 再按返回里的 [ref=eN] 去(browser_click|browser_type) 是推荐动线。
    """
    if selector is not None and not isinstance(selector, str):
        raise ToolError("selector must be a string")
    _status, body = _call("POST", "/session/%s/extract" % _need(session_id, "session_id"),
                          {"selector": selector or ""})
    return body


@mcp.tool
def browser_screenshot(session_id: str) -> Image:
    """对当前页面截图，返回 PNG 图片（视觉兜底路径，BP L3）。"""
    _status, body = _call("POST",
                          "/session/%s/screenshot" % _need(session_id, "session_id"), {})
    if isinstance(body, (bytes, bytearray)) and Image is not None:
        return Image(data=bytes(body), format="png")
    if isinstance(body, (bytes, bytearray)):
        raise ToolError("screenshot ok（%d bytes PNG）但当前 fastmcp 无 Image 包装"
                        % len(body))
    raise ToolError("screenshot 响应异常：%r" % (body,))


# ---------------------------------------------------------------------------
# 搜索/抓取（转调线3 /search /fetch ——未并入前如实降级）
# ---------------------------------------------------------------------------

@mcp.tool
def browser_search(query: str, provider: str = "", limit: int = 0) -> dict:
    """网页搜索（线3：DDG HTML 源 + Bing 兜底链，≤10 条/查询，归一化去重）。

    provider 可选（"duckduckgo"|"bing"，缺省按兜底链）；limit 可选 1-10。
    返回 {provider, count, results:[{position,url,title,snippet}], ...}。
    """
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query required")
    payload: dict[str, Any] = {"query": query.strip()}
    if provider:
        payload["provider"] = str(provider).strip().lower()
    if limit:
        payload["limit"] = int(limit)
    try:
        _status, body = _call("POST", "/search", payload)
    except ToolError as exc:
        if "HTTP 404" in str(exc):
            raise ToolError(_NOT_DEPLOYED) from None
        raise
    return body


@mcp.tool
def browser_fetch(url: str) -> dict:
    """抓取网页正文（线3 两栈路由：httpx 直抓优先，SPA 壳/异常码转渲染缝），返回 {text, html(截断), status, ...}。

    服务端 fetch allowlist fail-closed：未配置白名单的实例全拒（403）；
    robots 默认开启。渲染栈（NEEDS_RENDER）接入前 SPA 壳页面请改走
    browser_open + browser_goto + browser_extract 真浏览器路径。
    """
    if not isinstance(url, str) or not url.strip():
        raise ToolError("url required")
    try:
        _status, body = _call("POST", "/fetch", {"url": url.strip()})
    except ToolError as exc:
        if "HTTP 404" in str(exc):
            raise ToolError(_NOT_DEPLOYED) from None
        raise
    return body


def main() -> int:
    mcp.run()                           # stdio（jiuwenswarm MCP 默认传输）
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
