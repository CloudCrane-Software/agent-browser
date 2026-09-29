# coding: utf-8
"""两栈抓取路由：轻量 HTTP 栈（httpx）先行，条件触发 JS 渲染栈（接缝注入）.

路由策略（自研判断，写明依据）：**先轻后重**——

- HTTP 栈：httpx 直抓，带浏览器 UA/超时/重定向跟随；快而省（无浏览器进程开销，
  单次请求 ~几十 ms vs 渲染栈 ~秒级 + 100MB 级内存，BP §2.5 内存经验值）；
- 转渲染判定（``classify_fetch``，满足其一即判）：403/401/429（反爬限速）、
  5xx 之外的 JS challenge 页特征、空 body、``<meta http-equiv=refresh>``
  （sleep 跳转壳）、已知 SPA 挂载特征（``id="root"``/``id="app"``/``id="__next"``
  /``data-reactroot``/``ng-app`` 且正文文本极薄——BP §5 实测 openanolis.cn 即
  SPA 登录壳）、非 HTML content-type 但期望 HTML；
- 渲染栈 = 线1/线2 的 playwright/chrome-headless-shell，本包**不依赖**：
  ``FetchEngine(render_fn=...)`` 接缝注入，签名 ``render_fn(url) -> (final_url,
  status, html)``；未注入时返回 ``status=NEEDS_RENDER`` 的 FetchResult（降级
  不删除，调用方可感知）——UNKNOWN≠PASS 的同构语义。

robots 尊重：``respect_robots=True``（默认）时先抓 ``/robots.txt``（httpx），
:mod:`urllib.robotparser` 判定 Disallow 即不抓目标（fail-closed：robots.txt
抓取失败视为不可判 → 跳过该站，[待]留观测）。开关存在是因为内网/自有站点
无 robots 或误配时不该被卡死——服务面按调用方声明开关。
"""
from __future__ import annotations

import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from ..task import host_allowed

__all__ = [
    "FETCH_UA", "ROBOTS_TIMEOUT_S", "NEEDS_RENDER_REASONS",
    "FetchResult", "FetchBlocked", "FetchEngine", "classify_fetch",
]

# 浏览器 UA（BP §2 隐身层口径：纯协议层被动指纹；不带 UA 的默认 python-requests
# 头是最高频拦截特征）
FETCH_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) HeadlessChrome/131.0.0.0 Safari/537.36")

ROBOTS_TIMEOUT_S = 5.0

# 转渲染原因常量（结构化输出，不猜）
NEEDS_RENDER_REASONS = (
    "HTTP_403", "HTTP_401", "HTTP_429", "HTTP_5XX",
    "EMPTY_BODY", "META_REFRESH", "SPA_SHELL", "NOT_HTML",
)

STATUS_OK = "OK"
STATUS_BLOCKED = "BLOCKED"              # allowlist/robots 拦截（未发目标请求）
STATUS_NEEDS_RENDER = "NEEDS_RENDER"    # HTTP 栈判否，需渲染栈
STATUS_ERROR = "ERROR"                  # 网络层失败

# SPA 挂载特征：现代前端壳的标志性节点 id/属性
_SPA_MARKERS = (
    ('id', 'root'), ('id', 'app'), ('id', '__next'),
    ('id', '__nuxt'), ('data-reactroot', None), ('ng-app', None),
)

# meta refresh（引号/属性序不敏感）
_META_REFRESH_RE = re.compile(r"<meta[^>]+http-equiv\s*=\s*[\"']refresh[\"']")
_META_URL_RE = re.compile(r"<meta[^>]+content\s*=\s*[\"'][^\"']*url\s*=")


@dataclass
class FetchResult:
    """抓取结果（结构化：状态/栈/原因/内容，内容只留文本与上限截断账目）。"""

    url: str
    status: str                       # OK / BLOCKED / NEEDS_RENDER / ERROR
    stack: str = "http"               # http / render
    http_status: int = 0
    final_url: str = ""
    content_type: str = ""
    html: str = ""                    # 原始 HTML（HTTP 栈成功时才有）
    needs_render: bool = False
    needs_render_reason: str = ""
    reason: str = ""                  # BLOCKED/ERROR 的原因
    elapsed: float = 0.0
    rendered_by: str = ""             # render_fn 自报名称（审计用）
    audit: tuple = field(default_factory=tuple)


class FetchBlocked(RuntimeError):
    """fetch 被 allowlist/robots 拦截（导航未发生，语义同 task.STATUS_BLOCKED）。"""


def classify_fetch(http_status: int, content_type: str, html: str,
                   expect_html: bool = True):
    """HTTP 栈结果分类：返回 (needs_render: bool, reason: str, ok: bool).

    - ``ok=True``  ：HTTP 200 + HTML + 内容充分 → 直出，无需渲染；
    - ``needs_render=True``：满足转渲染条件之一（reason 给出）；
    - 都不是（如 404）→ (False, "", False)，由调用方按 ERROR/NOT_FOUND 处理。
    """
    if http_status in (401, 403):
        return True, "HTTP_%d" % http_status, False
    if http_status == 429:
        return True, "HTTP_429", False
    if 500 <= http_status < 600:
        return True, "HTTP_5XX", False
    if http_status != 200:
        return False, "", False                     # 404 等：不转渲染，直接失败
    ctype = (content_type or "").split(";")[0].strip().lower()
    if expect_html and ctype and ctype not in ("text/html", "application/xhtml+xml"):
        return True, "NOT_HTML", False
    text = html or ""
    if len(text.strip()) < 256:
        return True, "EMPTY_BODY", False
    low = text.lower()
    if (_META_REFRESH_RE.search(low) or _META_URL_RE.search(low)):
        return True, "META_REFRESH", False
    if _looks_like_spa_shell(low):
        return True, "SPA_SHELL", False
    return False, "", True


def _looks_like_spa_shell(low_html: str) -> bool:
    """SPA 壳判定：有挂载点特征（引号不敏感）且 可见文本占比极薄（<8%）。"""
    has_marker = any(
        re.search(r"%s\s*=\s*[\"']%s[\"']" % (re.escape(attr), re.escape(val))
                  if val is not None
                  else r"%s\s*=" % re.escape(attr),
                  low_html)
        for attr, val in _SPA_MARKERS)
    if not has_marker:
        return False
    # 去 tag 取近似可见文本占比
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", low_html)
    text = re.sub(r"<[^>]+>", " ", text)
    visible = len("".join(text.split()))
    total = len(low_html)
    return total > 0 and (visible / total) < 0.08


def _robots_allowed(client, url: str, timeout_s: float = ROBOTS_TIMEOUT_S) -> Tuple[bool, str]:
    """robots.txt 判定。返回 (allowed, note)。

    fail-closed：robots.txt 不可得（网络错/非 200/解析失败）→ 视为不可判，
    返回 (False, note)——与 UNKNOWN≠PASS 同构，绝不默认放行。
    """
    parsed = urllib.parse.urlsplit(url)
    robots_url = "%s://%s/robots.txt" % (parsed.scheme, parsed.netloc)
    parser = urllib.robotparser.RobotFileParser()
    try:
        resp = client.get(robots_url)
        if resp.status_code != 200:
            return False, "robots.txt HTTP %d (fail-closed)" % resp.status_code
        body = resp.text or ""
        if not body.strip():
            return False, "robots.txt empty (fail-closed)"
        parser.parse(body.splitlines())
    except Exception as exc:  # noqa: BLE001
        return False, "robots.txt unreachable: %s" % exc
    # robotparser 用 UA 匹配 '*' 组即可（我们非真实浏览器，按 * 判最严）
    if not parser.can_fetch("*", url):
        return False, "robots.txt Disallow"
    return True, "robots.txt allow"


class FetchEngine:
    """两栈路由引擎：httpx 先行 → classify → (可选) render_fn 兜底.

    - ``fetch_allowlist``：非空时目标 host 必须命中（复用 task.host_allowed
      同一实现——决策点唯一：allowlist 语义只在 task.py 一处）；
    - ``respect_robots``：True 时先 robots 判定（fail-closed）；
    - ``render_fn``：渲染栈接缝，签名 ``render_fn(url) -> (final_url, status,
      html)``；None 时转渲染判定命中即返回 NEEDS_RENDER（降级不删除）。
    """

    def __init__(self, client=None, render_fn: Optional[Callable] = None,
                 fetch_allowlist: Optional[List[str]] = None,
                 respect_robots: bool = True, timeout_s: float = 15.0):
        if client is None:
            import httpx
            client = httpx.Client(
                headers={"User-Agent": FETCH_UA, "Accept-Language": "en;q=0.9,zh-CN;q=0.8"},
                timeout=timeout_s, follow_redirects=True)
        self._client = client
        self._render_fn = render_fn
        self._allowlist = list(fetch_allowlist or [])
        self._respect_robots = respect_robots
        self._timeout_s = timeout_s

    def fetch(self, url: str, expect_html: bool = True) -> FetchResult:
        started = time.monotonic()

        def _done(**kw):
            return FetchResult(url=url, elapsed=time.monotonic() - started, **kw)

        # 1) allowlist 硬拦（配置了才拦；服务面对公网 fetch 必须配，见 server.py）
        if self._allowlist and not host_allowed(url, self._allowlist):
            raise FetchBlocked("URL blocked by fetch allowlist: %s" % url)

        # 2) robots（可关；开着时 fail-closed）
        if self._respect_robots:
            allowed, note = _robots_allowed(self._client, url)
            if not allowed:
                raise FetchBlocked("robots: %s (%s)" % (url, note))

        # 3) HTTP 栈
        try:
            resp = self._client.get(url)
        except Exception as exc:  # noqa: BLE001
            if self._render_fn is not None:
                return self._render(url, started, note="http error: %s" % exc)
            return _done(status=STATUS_ERROR, reason="http error: %s" % exc)

        # 重定向落点也必须在 allowlist 内（与 task.run_task 的 REDIRECT 复检同口径）
        if self._allowlist and not host_allowed(str(resp.url), self._allowlist):
            raise FetchBlocked("redirect target blocked by fetch allowlist: %s"
                               % resp.url)

        needs_render, reason, ok = classify_fetch(
            resp.status_code,
            resp.headers.get("content-type", ""),
            getattr(resp, "text", "") or "",
            expect_html=expect_html)
        if ok:
            return _done(status=STATUS_OK, stack="http",
                         http_status=resp.status_code,
                         final_url=str(resp.url), content_type=resp.headers.get(
                             "content-type", ""),
                         html=resp.text or "")
        # 4) 转渲染判定命中（404 类 needs_render=False → 走 ERROR）
        if needs_render:
            if self._render_fn is not None:
                return self._render(url, started, note="reason=%s" % reason)
            return _done(status=STATUS_NEEDS_RENDER, stack="http",
                         http_status=resp.status_code,
                         final_url=str(resp.url),
                         content_type=resp.headers.get("content-type", ""),
                         needs_render=True, needs_render_reason=reason)
        return _done(status=STATUS_ERROR, stack="http",
                     http_status=resp.status_code, final_url=str(resp.url),
                     reason="HTTP %d not ok and not renderable" % resp.status_code)

    def _render(self, url: str, started: float, note: str = "") -> FetchResult:
        """渲染栈兜底：render_fn(url) -> (final_url, status:int, html)。"""
        try:
            final_url, status, html = self._render_fn(url)
        except Exception as exc:  # noqa: BLE001 — 渲染失败按 ERROR，不吞
            return FetchResult(url=url, status=STATUS_ERROR, stack="render",
                               reason="render_fn error: %s (%s)" % (exc, note),
                               elapsed=time.monotonic() - started)
        return FetchResult(url=url, status=STATUS_OK if status == 200 else STATUS_ERROR,
                           stack="render", http_status=int(status),
                           final_url=str(final_url), html=html or "",
                           rendered_by=getattr(self._render_fn, "__name__", "render_fn"),
                           elapsed=time.monotonic() - started)
