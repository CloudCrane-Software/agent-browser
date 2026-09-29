# coding: utf-8
"""搜索/抓取服务面：stdlib HTTP 服务——POST /search + POST /fetch + GET /health.

定位（工单线3）：线1/线2 的浏览器能力 server.py 尚未在仓内出现（2026-09-30
时点实况），本模块先以**零依赖（纯 stdlib http.server）**形态落仓，端点契约
与线2 对齐：线2 的 server.py 出现后，把 ``handle_search``/``handle_fetch``
两个函数原样并入即可（[待] 并单，见交付报告）。

红线映射：

- **allowlist 对 fetch 生效**：``fetch_allowlist`` 为空 = fail-closed 拒绝一切
  fetch（与 task.py run_task 的空 allowlist 拒启同构）；
- **搜索源白名单内置**：只放行 :data:`.search.SEARCH_PROVIDER_ALLOWLIST`
  （duckduckgo/bing）；请求指定 provider 不在白名单 → 403，不落探测请求；
- 默认只绑 127.0.0.1（服务不暴露公网；跨机访问走隧道，同 CDP 纪律）；
- 审计：每请求一行审计事件（端点/来源/查询或 URL/状态/耗时），查询与 URL
  可记（非密钥非输入正文），响应只带结构化结果。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional

from .fetch import FETCH_UA, FetchBlocked, FetchEngine
from .extract import extract_main
from .search import (
    MAX_RESULTS_PER_QUERY,
    SEARCH_PROVIDER_ALLOWLIST,
    BingSearch,
    DdgHtmlSearch,
    SearchProvider,
    search,
)

__all__ = ["SearchFetchService", "serve", "mount_search_fetch", "MAX_HTML_ECHO",
           "DEFAULT_FETCH_ALLOWLIST"]

MAX_HTML_ECHO = 200_000        # /fetch 响应里 html 字段截断上限（防巨型响应）

DEFAULT_FETCH_ALLOWLIST = (
    "openanolis.cn", "duckduckgo.com", "bing.com",
)


class SearchFetchService:
    """端点处理核心（与 HTTP 帧解耦，便于测试与并入线2 server.py）。"""

    def __init__(self, search_client=None, fetch_client=None,
                 fetch_allowlist: Optional[List[str]] = None,
                 respect_robots: bool = True,
                 render_fn: Optional[Callable] = None,
                 default_limit: int = MAX_RESULTS_PER_QUERY):
        self._search_client = search_client      # None = search() 内建默认
        self._fetch_client = fetch_client
        self._fetch_allowlist = list(fetch_allowlist) if fetch_allowlist is not None else []
        self._respect_robots = respect_robots
        self._render_fn = render_fn
        self.default_limit = default_limit
        self.audit: List[dict] = []
        self._audit_lock = threading.Lock()
        self._provider_factories = {
            "duckduckgo": DdgHtmlSearch,
            "bing": BingSearch,
        }

    # ---------------------------------------------------------------- 端点

    def handle_search(self, payload: dict) -> tuple:
        """POST /search → (http_status, body_dict)。"""
        query = str(payload.get("query") or "").strip()
        if not query:
            return 400, {"error": "query required"}
        wanted = str(payload.get("provider") or "").strip().lower()
        if wanted and wanted not in SEARCH_PROVIDER_ALLOWLIST:
            # 白名单内置：不在名单的搜索源直接拒，不外发请求
            return 403, {"error": "provider not allowed",
                         "allowed": list(SEARCH_PROVIDER_ALLOWLIST)}
        limit = _clamp_limit(payload.get("limit", self.default_limit))

        providers = self._build_providers(wanted)
        started = time.monotonic()
        try:
            resp = search(query, providers=providers, limit=limit)
        except Exception as exc:  # noqa: BLE001 — 全部源失败 → 502
            self._audit_event("search", target=query, ok=False, detail=str(exc))
            return 502, {"error": "all search providers failed", "detail": str(exc)}
        elapsed = time.monotonic() - started
        self._audit_event("search", target=query, ok=True,
                          provider=resp.provider, results=len(resp.results))
        body = {
            "query": query,
            "provider": resp.provider,
            "count": len(resp.results),
            "raw_count": resp.raw_count,
            "dropped_duplicates": resp.dropped_duplicates,
            "truncated": resp.truncated,
            "elapsed": round(elapsed, 3),
            "fallback_from": list(resp.fallback_from),
            "results": [{"position": i + 1, "url": r.url, "title": r.title,
                         "snippet": r.snippet}
                        for i, r in enumerate(resp.results)],
        }
        return 200, body

    def handle_fetch(self, payload: dict) -> tuple:
        """POST /fetch → (http_status, body_dict)。"""
        url = str(payload.get("url") or "").strip()
        if not url:
            return 400, {"error": "url required"}
        if not self._fetch_allowlist:
            # fail-closed：未配 allowlist 的服务实例不抓任何公网页
            return 403, {"error": "fetch allowlist not configured (fail-closed)"}
        engine = FetchEngine(client=self._fetch_client,
                             render_fn=self._render_fn,
                             fetch_allowlist=self._fetch_allowlist,
                             respect_robots=self._respect_robots)
        started = time.monotonic()
        try:
            result = engine.fetch(url)
        except FetchBlocked as exc:
            self._audit_event("fetch", target=url, ok=False, blocked=True,
                              detail=str(exc))
            return 403, {"error": "blocked", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 — 意外异常 → 500，不崩服务
            self._audit_event("fetch", target=url, ok=False, detail=str(exc))
            return 500, {"error": "fetch failed", "detail": str(exc)}
        elapsed = time.monotonic() - started
        self._audit_event("fetch", target=url, ok=(result.status == "OK"),
                          status=result.status, stack=result.stack)
        article = extract_main(result.html) if result.html else None
        body = {
            "url": url,
            "status": result.status,
            "stack": result.stack,
            "http_status": result.http_status,
            "final_url": result.final_url,
            "content_type": result.content_type,
            "needs_render": result.needs_render,
            "needs_render_reason": result.needs_render_reason,
            "reason": result.reason,
            "elapsed": round(elapsed, 3),
            "html_len": len(result.html),
            "html": result.html[:MAX_HTML_ECHO],
            "text": (article.text if article else ""),
            "text_node": (article.node_path if article else ""),
        }
        return 200, body

    def handle_health(self) -> tuple:
        providers = sorted(self._provider_factories)
        return 200, {"ok": True, "search_providers": providers,
                     "fetch_allowlist_size": len(self._fetch_allowlist),
                     "respect_robots": self._respect_robots}

    # ---------------------------------------------------------------- 内部

    def _build_providers(self, wanted: str) -> List[SearchProvider]:
        names = [wanted] if wanted else list(SEARCH_PROVIDER_ALLOWLIST)
        client = self._ensure_search_client()
        out = []
        for name in names:
            factory = self._provider_factories.get(name)
            if factory is not None:
                out.append(factory(client))
        return out

    def _ensure_search_client(self):
        if self._search_client is None:
            import httpx
            self._search_client = httpx.Client(
                headers={"User-Agent": FETCH_UA}, timeout=15.0,
                follow_redirects=True)
        return self._search_client

    def _audit_event(self, endpoint: str, **fields):
        event = {"ts": time.time(), "endpoint": endpoint}
        event.update(fields)
        with self._audit_lock:
            self.audit.append(event)


def _clamp_limit(value) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = MAX_RESULTS_PER_QUERY
    return max(1, min(limit, MAX_RESULTS_PER_QUERY))


def mount_search_fetch(service, **kw) -> SearchFetchService:
    """把 /search + /fetch 挂到线2 BrowserService 实例上（并入缝）.

    线2 ``server.py`` 的 do_POST 对 ``/search`` ``/fetch`` 路径委托给
    ``service.search_fetch``（由本函数设置）。fetch_allowlist 缺省用
    :data:`DEFAULT_FETCH_ALLOWLIST`（搜索源+openanolis）；显式传
    ``fetch_allowlist=[]`` 即 fail-closed 关闭 fetch。
    """
    if kw.get("fetch_allowlist", None) is None and "fetch_allowlist" not in kw:
        kw["fetch_allowlist"] = list(DEFAULT_FETCH_ALLOWLIST)
    svc = SearchFetchService(**kw)
    service.search_fetch = svc
    return svc


def make_handler(service: SearchFetchService):
    """把 SearchFetchService 包成 BaseHTTPRequestHandler 类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-browser-search/0.1"

        def _send(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None

        def do_GET(self):  # noqa: N802（http.server 命名）
            if self.path == "/health":
                code, body = service.handle_health()
                self._send(code, body)
                return
            self._send(404, {"error": "not found (POST /search, POST /fetch, GET /health)"})

        def do_POST(self):  # noqa: N802
            payload = self._read_json()
            if payload is None or not isinstance(payload, dict):
                self._send(400, {"error": "invalid JSON body"})
                return
            if self.path == "/search":
                code, body = service.handle_search(payload)
            elif self.path == "/fetch":
                code, body = service.handle_fetch(payload)
            else:
                code, body = 404, {"error": "unknown endpoint"}
            self._send(code, body)

        def log_message(self, fmt, *args):  # 安静模式：不打 stderr（审计在 service）
            pass

    return Handler


def serve(host="127.0.0.1", port=0, service: Optional[SearchFetchService] = None,
          **service_kw) -> tuple:
    """起服务。返回 (httpd, port)。port=0 由内核分配（测试用）。"""
    if service is None:
        service = SearchFetchService(**service_kw)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    return httpd, httpd.server_address[1]
