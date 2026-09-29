# coding: utf-8
"""搜索提供者：``SearchProvider`` 协议 + DDG HTML 版 + Bing 兜底.

设计要点（自研判断，依据见各行注释）：

- **协议**：任何具备 ``name`` + ``search(query, limit)`` 的对象即 SearchProvider
  （结构与 :mod:`agent_browser.driver` 的 ``is_browser_driver`` 同法，runtime_checkable）；
- **DDG HTML 版**（``html.duckduckgo.com/html/``）：无需 API key，结果带
  标题+URL+摘要；链接是 ``//duckduckgo.com/l/?uddg=<urlencoded>`` 跳转壳，
  必须解包 ``uddg`` 参数才是真实 URL；
- **Bing 兜底**：DDG 被限速/结构变更/零结果时转 ``www.bing.com/search``
  （BP §5 实测：cn.bing.com 无 cookie 会话 ``site:`` 限定有效）；
- **质量红线**：结果结构化（url/title/snippet）+ 去重 + 每查询 ≤10 条；
- **限速礼仪**（BP §5 检索清单：串行+429 退避）：provider 级 ``min_interval_s``
  串行节流；被 block（403/验证码标记）抛 :class:`SearchQueryError` 交给上层兜底链。

**SearXNG 评估（本轮不接，决策依据）**：CNB Git-Platform 现实下无搜索 key；
自托管 SearXNG 容器可聚合多引擎且免 key，但需要：srv-1 常驻容器（治理面机器
新增一个常驻服务，涉及 §5 服务面变更走 PR）+ 引擎上游（Google/Bing 对数据中心
IP 反爬，SearXNG 公共实例普遍挂验证码，自托管在 srv-1 机房 IP 上命中率未验证）+
运维负担（版本跟进/实例被滥用风险需加访问控制）。**结论：本轮不引入**；
[待 owner] 若后续搜索量上来且 DDG/Bing 兜底不稳，再评估 srv-1 自托管
（容器化、仅 tailnet 内网监听、带 ACL）。本仓实现已按 provider 协议留缝，
SearXNG 未来实现同一协议即可接入，不改调用方。
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Protocol, runtime_checkable

from bs4 import BeautifulSoup

__all__ = [
    "MAX_RESULTS_PER_QUERY",
    "SearchProvider",
    "SearchResult",
    "SearchResponse",
    "SearchQueryError",
    "DdgHtmlSearch",
    "BingSearch",
    "is_search_provider",
    "search",
]

MAX_RESULTS_PER_QUERY = 10          # 质量红线：每查询 ≤10 条

# 搜索源白名单（服务面内置；fetch/搜索端点只放行这些 provider name）
SEARCH_PROVIDER_ALLOWLIST = ("duckduckgo", "bing")

_BLOCK_MARKERS = (
    "anomaly",             # DDG anomaly detection 页
    "captcha",             # 通用验证码页标记
    "unusual traffic",     # Google 系措辞（Bing 偶发复用）
    "unusual activity",    # Bing 措辞
)


class SearchQueryError(RuntimeError):
    """单个搜索源失败（网络/限速/验证码/结构变更/零结果）。"""


@dataclass(frozen=True)
class SearchResult:
    """结构化结果（质量红线：URL+标题+摘要，缺一不可）。"""

    url: str
    title: str
    snippet: str
    position: int = 0


@dataclass
class SearchResponse:
    """一次 search() 的聚合输出：用了哪个源、去重/截断账目。"""

    query: str
    provider: str
    results: List[SearchResult] = field(default_factory=list)
    raw_count: int = 0          # 去重前
    dropped_duplicates: int = 0
    truncated: bool = False
    fallback_from: tuple = ()   # 依次失败过的 provider name


@runtime_checkable
class SearchProvider(Protocol):
    """搜索提供者协议：name + search(query, limit)。"""

    name: str

    def search(self, query: str, limit: int = MAX_RESULTS_PER_QUERY) -> List[SearchResult]:
        ...


def is_search_provider(obj) -> bool:
    """结构化检查：有 name 且 search 可调用即视为 SearchProvider。"""
    if not getattr(obj, "name", None):
        return False
    return callable(getattr(obj, "search", None))


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def unwrap_ddg_href(href: str) -> str:
    """解包 DDG 跳转壳 ``//duckduckgo.com/l/?uddg=<urlencoded>&rut=...``。

    非 DDG 壳（真实直链）原样返回。
    """
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in (parsed.hostname or "") and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


class _HttpSearchBase:
    """公共骨架：节流 + HTTP 判错 + 子类解析钩子。"""

    name = "base"

    def __init__(self, client, endpoint, min_interval_s: float = 0.0):
        self._client = client            # httpx.Client 兼容对象（测试用 MockTransport）
        self._endpoint = endpoint
        self._min_interval_s = float(min_interval_s)
        self._last_call = 0.0

    def _throttle(self):
        if self._min_interval_s <= 0:
            return
        now = time.monotonic()
        wait = self._last_call + self._min_interval_s - now
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _fetch_text(self, *, method: str, url: str, **kwargs) -> str:
        self._throttle()
        resp = getattr(self._client, method)(url, **kwargs)
        if resp.status_code != 200:
            raise SearchQueryError(
                "%s: HTTP %d from %s" % (self.name, resp.status_code, url))
        text = resp.text or ""
        low = text[:4096].lower()
        if any(marker in low for marker in _BLOCK_MARKERS):
            raise SearchQueryError(
                "%s: blocked/captcha page at %s" % (self.name, url))
        return text

    def search(self, query: str, limit: int = MAX_RESULTS_PER_QUERY) -> List[SearchResult]:
        query = (query or "").strip()
        if not query:
            raise SearchQueryError("%s: empty query" % self.name)
        limit = max(1, min(int(limit), MAX_RESULTS_PER_QUERY))
        results = self._fetch_and_parse(query)
        if not results:
            raise SearchQueryError("%s: zero results for %r" % (self.name, query))
        return results[:limit]

    def _fetch_and_parse(self, query: str) -> List[SearchResult]:
        raise NotImplementedError


class DdgHtmlSearch(_HttpSearchBase):
    """DuckDuckGo HTML 版（无需 key）。POST ``q`` 到 html endpoint。"""

    name = "duckduckgo"

    def __init__(self, client, endpoint="https://html.duckduckgo.com/html/",
                 min_interval_s: float = 0.0):
        super().__init__(client, endpoint, min_interval_s)

    def _fetch_and_parse(self, query: str) -> List[SearchResult]:
        html = self._fetch_text(method="post", url=self._endpoint,
                                data={"q": query})
        soup = _soup(html)
        results: List[SearchResult] = []
        blocks = soup.select("div.result") or soup.select("div.web-result")
        for block in blocks:
            if "result--ad" in (block.get("class") or []):
                continue                     # 广告位不进结果
            anchor = block.select_one("a.result__a") or block.select_one("h2 a")
            if anchor is None:
                continue
            title = _clean(anchor.get_text())
            url = unwrap_ddg_href(anchor.get("href", ""))
            snippet_node = (block.select_one(".result__snippet")
                            or block.select_one(".result__extras__snippet"))
            snippet = _clean(snippet_node.get_text()) if snippet_node else ""
            if not url or not title:
                continue
            results.append(SearchResult(url=url, title=title,
                                        snippet=snippet,
                                        position=len(results) + 1))
        return results


class BingSearch(_HttpSearchBase):
    """Bing 网页版兜底（无需 key）。GET ``q``。"""

    name = "bing"

    def __init__(self, client, endpoint="https://www.bing.com/search",
                 min_interval_s: float = 0.0):
        super().__init__(client, endpoint, min_interval_s)

    def _fetch_and_parse(self, query: str) -> List[SearchResult]:
        html = self._fetch_text(method="get", url=self._endpoint,
                                params={"q": query})
        soup = _soup(html)
        results: List[SearchResult] = []
        for block in soup.select("li.b_algo"):
            anchor = block.select_one("h2 a")
            if anchor is None:
                continue
            title = _clean(anchor.get_text())
            url = (anchor.get("href") or "").strip()
            snippet_node = (block.select_one(".b_caption p")
                            or block.select_one("p.b_algoSlug")
                            or block.select_one(".b_caption"))
            snippet = _clean(snippet_node.get_text()) if snippet_node else ""
            if not url or not title:
                continue
            results.append(SearchResult(url=url, title=title,
                                        snippet=snippet,
                                        position=len(results) + 1))
        return results


def normalize_url(url: str) -> str:
    """去重键：去 fragment、host 小写、去尾斜杠（仅根路径）。"""
    parsed = urllib.parse.urlsplit((url or "").strip())
    if not parsed.scheme and not parsed.netloc:
        return url
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    elif path == "/":
        path = ""
    return "%s://%s%s%s" % (parsed.scheme.lower(), host, path,
                            ("?" + parsed.query) if parsed.query else "")


def dedup_results(results: Iterable[SearchResult]):
    """按 normalize_url 去重，保序；返回 (去重后列表, 丢弃数)。"""
    seen = set()
    kept, dropped = [], 0
    for r in results:
        key = normalize_url(r.url)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(r)
    return kept, dropped


def search(query: str, providers=None, limit: int = MAX_RESULTS_PER_QUERY,
           client=None) -> SearchResponse:
    """搜索主入口：按序试 provider，失败（异常/零结果）即兜底下一个。

    - ``providers`` 缺省 = [DDG, Bing]（各自新建默认 client，需传 ``client`` 时
      providers 也应自带；直接传 providers 即可）；
    - 返回 :class:`SearchResponse`；全部源失败 → 抛最后一个 :class:`SearchQueryError`。
    """
    if providers is None:
        if client is None:
            import httpx
            client = httpx.Client(
                headers={"User-Agent": _default_ua()}, timeout=15.0,
                follow_redirects=True)
        providers = [DdgHtmlSearch(client), BingSearch(client)]
    providers = list(providers)
    if not providers:
        raise SearchQueryError("no search providers configured")
    limit = max(1, min(int(limit), MAX_RESULTS_PER_QUERY))

    last_error: Optional[Exception] = None
    failed = []
    for provider in providers:
        if not is_search_provider(provider):
            raise SearchQueryError(
                "not a SearchProvider: %r" % getattr(provider, "name", provider))
        try:
            raw = provider.search(query, limit=limit)
        except SearchQueryError as exc:
            last_error = exc
            failed.append(provider.name)
            continue
        except Exception as exc:  # noqa: BLE001 — 网络层异常统一视为该源失败
            last_error = SearchQueryError("%s: %s" % (provider.name, exc))
            failed.append(provider.name)
            continue
        kept, dropped = dedup_results(raw)
        truncated = len(kept) > limit
        return SearchResponse(
            query=query, provider=provider.name, results=kept[:limit],
            raw_count=len(raw), dropped_duplicates=dropped,
            truncated=truncated, fallback_from=tuple(failed))
    raise last_error or SearchQueryError("all search providers failed")


def _default_ua() -> str:
    # 延迟 import 避免循环依赖（fetch 也定义 UA 常量）
    from .fetch import FETCH_UA
    return FETCH_UA
