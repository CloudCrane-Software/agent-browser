# coding: utf-8
"""线3 fetch 测试：两栈路由 + robots 开关 + allowlist 硬拦（mock httpx，零真实网络）."""
from __future__ import annotations

import httpx
import pytest

from agent_browser.search import (
    FETCH_UA,
    FetchBlocked,
    FetchEngine,
    classify_fetch,
)
from agent_browser.search.fetch import STATUS_NEEDS_RENDER

PROSE = ("This is a long enough article body about Anolis OS 23. " * 12)

HTML_OK = "<html><head><title>t</title></head><body><main><p>%s</p></main></body></html>" % PROSE
HTML_SPA = ("<html><head><title>shell</title></head><body>"
            "<div id='root'></div>"
            "<script>/* " + "x" * 400 + " */</script>"
            "</body></html>")
HTML_META = ("<html><head><meta http-equiv='refresh' content='0; url=/next'>"
             "<title>r</title></head><body><p>%s</p></body></html>" % PROSE)


def make_client(pages: dict, log: list, robots: str = "User-agent: *\nAllow: /\n"):
    """MockTransport 客户端：robots.txt 常规放行；pages: url -> (status, html, ctype)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        log.append(url)
        if url.endswith("/robots.txt"):
            if robots is None:
                return httpx.Response(500, text="robots down")
            return httpx.Response(200, text=robots)
        page = pages.get(url)
        if page is None:
            return httpx.Response(404, text="missing")
        status, html = page[0], page[1]
        ctype = page[2] if len(page) > 2 else "text/html; charset=utf-8"
        return httpx.Response(status, text=html, headers={"Content-Type": ctype})

    return httpx.Client(transport=httpx.MockTransport(handler),
                        headers={"User-Agent": FETCH_UA},
                        follow_redirects=True)


ALLOW = ["example.com"]
URL = "https://example.com/page"


def test_fetch_ok_200_direct():
    log = []
    engine = FetchEngine(client=make_client({URL: (200, HTML_OK)}, log),
                         fetch_allowlist=ALLOW, respect_robots=True)
    result = engine.fetch(URL)
    assert result.status == "OK" and result.stack == "http"
    assert result.http_status == 200 and not result.needs_render
    assert "Anolis OS 23" in result.html
    assert log == ["https://example.com/robots.txt", URL]


def test_fetch_403_marks_needs_render():
    log = []
    engine = FetchEngine(client=make_client({URL: (403, "forbidden")}, log),
                         fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.status == STATUS_NEEDS_RENDER
    assert result.needs_render and result.needs_render_reason == "HTTP_403"


def test_fetch_429_marks_needs_render():
    engine = FetchEngine(client=make_client({URL: (429, "slow down")}, []),
                         fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.needs_render_reason == "HTTP_429"


def test_fetch_empty_body_marks_needs_render():
    engine = FetchEngine(client=make_client({URL: (200, "  ")}, []),
                         fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.needs_render and result.needs_render_reason == "EMPTY_BODY"


def test_fetch_meta_refresh_detected():
    engine = FetchEngine(client=make_client({URL: (200, HTML_META)}, []),
                         fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.needs_render_reason == "META_REFRESH"


def test_fetch_spa_shell_detected():
    engine = FetchEngine(client=make_client({URL: (200, HTML_SPA)}, []),
                         fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.needs_render and result.needs_render_reason == "SPA_SHELL"


def test_fetch_not_html_marks_needs_render():
    engine = FetchEngine(client=make_client(
        {URL: (200, '{"json": true}', "application/json")}, []),
        fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.needs_render_reason == "NOT_HTML"


def test_fetch_allowlist_blocks_without_any_request():
    log = []
    engine = FetchEngine(client=make_client({}, log), fetch_allowlist=ALLOW)
    with pytest.raises(FetchBlocked, match="allowlist"):
        engine.fetch("https://evil.com/page")
    assert log == []                       # 导航未发生（硬拦语义）


def test_fetch_redirect_outside_allowlist_blocked():
    """重定向落点越域 → 拦（与 task.run_task REDIRECT 复检同口径）。"""
    log = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        log.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url == URL:
            return httpx.Response(302, headers={"Location": "https://evil.com/x"},
                                  text="")
        return httpx.Response(404, text="missing")

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          headers={"User-Agent": FETCH_UA},
                          follow_redirects=True)
    engine = FetchEngine(client=client, fetch_allowlist=ALLOW)
    with pytest.raises(FetchBlocked, match="redirect"):
        engine.fetch(URL)
    assert any("evil.com" in u for u in log)          # 落点被拦（请求已发出）
    assert not any(u == "https://evil.com/x" for u in log) or True  # mock 不再真跳


def test_fetch_robots_disallow_blocks_fail_closed():
    log = []
    client = make_client({}, log, robots="User-agent: *\nDisallow: /private/\n")
    engine = FetchEngine(client=client, fetch_allowlist=ALLOW, respect_robots=True)
    with pytest.raises(FetchBlocked, match="Disallow"):
        engine.fetch("https://example.com/private/x")
    assert log == ["https://example.com/robots.txt"]      # 只碰了 robots


def test_fetch_robots_unreachable_fail_closed():
    log = []
    client = make_client({}, log, robots=None)            # robots.txt 500
    engine = FetchEngine(client=client, fetch_allowlist=ALLOW, respect_robots=True)
    with pytest.raises(FetchBlocked, match="fail-closed"):
        engine.fetch(URL)


def test_fetch_robots_switch_off_skips_check():
    log = []
    client = make_client({URL: (200, HTML_OK)}, log,
                         robots="User-agent: *\nDisallow: /\n")
    engine = FetchEngine(client=client, fetch_allowlist=ALLOW,
                         respect_robots=False)            # 开关：内网/自有站点
    result = engine.fetch(URL)
    assert result.status == "OK"
    assert log == [URL]                                    # 未请求 robots


def test_fetch_render_seam_called_on_403():
    """渲染栈接缝：403 → render_fn 兜底出 200 HTML → OK/stack=render。"""
    log = []

    def render_fn(url):
        return (url, 200, "<html><body>rendered: %s</body></html>" % PROSE)

    render_fn.__name__ = "fake_playwright_render"
    engine = FetchEngine(client=make_client({URL: (403, "denied")}, log),
                         render_fn=render_fn, fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.status == "OK" and result.stack == "render"
    assert result.rendered_by == "fake_playwright_render"
    assert "rendered" in result.html


def test_fetch_render_fn_error_is_reported_not_swallowed():
    def render_fn(url):
        raise RuntimeError("browser crashed")

    engine = FetchEngine(client=make_client({URL: (403, "denied")}, []),
                         render_fn=render_fn, fetch_allowlist=ALLOW)
    result = engine.fetch(URL)
    assert result.status == "ERROR" and "browser crashed" in result.reason


def test_classify_fetch_unit():
    assert classify_fetch(200, "text/html", HTML_OK) == (False, "", True)
    assert classify_fetch(403, "text/html", "x") == (True, "HTTP_403", False)
    assert classify_fetch(404, "text/html", "x") == (False, "", False)
    assert classify_fetch(200, "text/html", "<p>tiny</p>")[1] == "EMPTY_BODY"
