# coding: utf-8
"""线3 search 测试：DDG/Bing 解析器（固定 HTML fixture）+ mock httpx 兜底链.

全部走 httpx.MockTransport（无真实网络）；fixture 在 tests/fixtures/。
"""
from __future__ import annotations

import pathlib
import time

import httpx
import pytest

from agent_browser.search import (
    MAX_RESULTS_PER_QUERY,
    BingSearch,
    DdgHtmlSearch,
    SearchResult,
    SearchQueryError,
    is_search_provider,
    normalize_url,
    search,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
DDG_HTML = (FIXTURES / "ddg_anolis.html").read_text(encoding="utf-8")
BING_HTML = (FIXTURES / "bing_anolis.html").read_text(encoding="utf-8")

DDG_URL = "https://html.duckduckgo.com/html/"
BING_URL = "https://www.bing.com/search"


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler),
                        headers={"User-Agent": "pytest-ua"},
                        follow_redirects=True)


def ddg_client(ddg_text=DDG_HTML, status=200, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, str(request.url),
                          dict(request.url.params)))
        return httpx.Response(status, text=ddg_text)
    return make_client(handler)


# ---------------------------------------------------------------------------
# DDG 解析器（固定 fixture）
# ---------------------------------------------------------------------------

def test_ddg_parser_structured_and_unwraps_uddg():
    """质量红线：URL+标题+摘要结构化；uddg 跳转壳必须解包为真实 URL。"""
    provider = DdgHtmlSearch(ddg_client())
    results = provider.search("Anolis OS 23")
    assert len(results) == 4                       # 6 块 - 1 广告 - 1 无锚噪音
    first = results[0]
    assert first.url == "https://openanolis.cn/signature"   # 解包成功
    assert "Anolis OS 23 Signature" in first.title
    assert "ANCK 6.6 kernel" in first.snippet or "OpenAnolis" in first.snippet
    assert first.position == 1
    assert all(r.url.startswith("http") for r in results)
    assert all(r.title and r.snippet for r in results)
    direct = [r for r in results if "docs.example.com" in r.url]
    assert direct and direct[0].url == "https://docs.example.com/anolis-mirror"


def test_ddg_parser_skips_ads_and_noise():
    provider = DdgHtmlSearch(ddg_client())
    results = provider.search("Anolis OS 23")
    assert all("ads.example.com" not in r.url for r in results)   # 广告剔除
    assert all(r.title for r in results)                          # 噪音块剔除


def test_ddg_zero_results_raises():
    empty = "<html><body><div id='links'></div></body></html>"
    provider = DdgHtmlSearch(ddg_client(ddg_text=empty))
    with pytest.raises(SearchQueryError, match="zero results"):
        provider.search("nothing matches")


def test_ddg_blocked_page_raises():
    provider = DdgHtmlSearch(ddg_client(
        ddg_text="if this anomaly persists, solve the captcha"))
    with pytest.raises(SearchQueryError, match="blocked"):
        provider.search("Anolis OS 23")


def test_ddg_http_error_raises():
    provider = DdgHtmlSearch(ddg_client(status=403, ddg_text="denied"))
    with pytest.raises(SearchQueryError, match="HTTP 403"):
        provider.search("Anolis OS 23")


def test_ddg_posts_query_and_throttles():
    calls = []
    provider = DdgHtmlSearch(ddg_client(calls=calls), min_interval_s=0.2)
    t0 = time.monotonic()
    provider.search("Anolis OS 23")
    provider.search("OpenAnolis")
    # 容差 0.15：Windows monotonic 计时器分辨率 ~15ms，避免边界抖动
    assert time.monotonic() - t0 >= 0.15
    assert len(calls) == 2
    assert calls[0][0] == "POST" and calls[0][1] == DDG_URL


# ---------------------------------------------------------------------------
# Bing 解析器（固定 fixture）
# ---------------------------------------------------------------------------

def test_bing_parser_structured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Anolis OS 23"
        return httpx.Response(200, text=BING_HTML)
    provider = BingSearch(make_client(handler))
    results = provider.search("Anolis OS 23")
    assert len(results) == 3                        # 4 块 - 1 无锚噪音
    assert results[0].url == "https://openanolis.cn/signature"
    assert "ANCK 6.6" in results[0].snippet
    assert results[2].snippet                       # b_algoSlug 兜底选段


# ---------------------------------------------------------------------------
# 兜底链 / 去重 / 截断
# ---------------------------------------------------------------------------

class StubProvider:
    """无网络桩 provider（协议结构检查也用它）。"""

    name = "stub"

    def __init__(self, results):
        self._results = results

    def search(self, query, limit=MAX_RESULTS_PER_QUERY):
        return list(self._results)[:limit]


def _mk(url, n=0):
    return SearchResult(url=url, title="t%d" % n, snippet="s%d" % n)


def test_search_dedup_and_cap():
    raw = ([_mk("https://Example.com/a", 0), _mk("https://example.com/a/", 1),
            _mk("https://example.com/a#frag", 2)]
           + [_mk("https://example.com/p%d" % i, i + 3) for i in range(9)])
    resp = search("q", providers=[StubProvider(raw)])
    assert len(resp.results) <= MAX_RESULTS_PER_QUERY == 10
    assert resp.provider == "stub"
    assert resp.dropped_duplicates == 2             # 尾斜杠与 fragment 归一
    assert len({r.url for r in resp.results}) == len(resp.results)


def test_search_falls_back_to_bing_on_ddg_error():
    """mock httpx：DDG 500 → Bing 兜底出结果，fallback_from 记账。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if "duckduckgo" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, text=BING_HTML)
    resp = search("Anolis OS 23",
                  providers=[DdgHtmlSearch(make_client(handler)),
                             BingSearch(make_client(handler))])
    assert resp.provider == "bing"
    assert resp.fallback_from == ("duckduckgo",)
    assert resp.results and "openanolis.cn" in resp.results[0].url


def test_search_all_providers_fail_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")
    with pytest.raises(SearchQueryError):
        search("Anolis OS 23",
               providers=[DdgHtmlSearch(make_client(handler)),
                          BingSearch(make_client(handler))])


def test_search_rejects_non_provider():
    class Bad:
        name = "bad"                                # 无 search 方法
    with pytest.raises(SearchQueryError, match="not a SearchProvider"):
        search("q", providers=[Bad()])


def test_is_search_provider_structural():
    assert is_search_provider(StubProvider([]))
    assert is_search_provider(DdgHtmlSearch(ddg_client()))
    assert not is_search_provider(object())
    assert not is_search_provider(type("NoName", (), {"search": lambda s, q: []})())


def test_normalize_url_dedup_key():
    assert (normalize_url("https://Example.com/a/#x")
            == normalize_url("https://example.com/a"))
    assert normalize_url("https://e.com/p?b=2&a=1") == "https://e.com/p?b=2&a=1"
    assert normalize_url("https://e.com/") == "https://e.com"
    assert normalize_url("not a url") == "not a url"   # 非法输入原样返回
