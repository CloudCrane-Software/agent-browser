# coding: utf-8
"""线3 server 测试：POST /search + POST /fetch + 白名单/fail-closed（全 mock）."""
from __future__ import annotations

import json
import pathlib
import threading

import httpx
import pytest

from agent_browser.search import FETCH_UA
from agent_browser.search.server import SearchFetchService, make_handler, serve

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
DDG_HTML = (FIXTURES / "ddg_anolis.html").read_text(encoding="utf-8")
PROSE = "Long enough server-side body text about Anolis OS 23. " * 12

URL_OK = "https://example.com/page"
HTML_OK = "<html><body><p>%s</p></body></html>" % PROSE


def mock_search_client(ddg_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if "duckduckgo" in str(request.url):
            return httpx.Response(ddg_status, text=DDG_HTML)
        return httpx.Response(200, text="<html><body>no b_algo</body></html>")
    return httpx.Client(transport=httpx.MockTransport(handler),
                        headers={"User-Agent": "pytest"})


def mock_fetch_client():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if url == URL_OK:
            return httpx.Response(200, text=HTML_OK,
                                  headers={"Content-Type": "text/html"})
        return httpx.Response(404, text="no")
    return httpx.Client(transport=httpx.MockTransport(handler),
                        headers={"User-Agent": FETCH_UA}, follow_redirects=True)


@pytest.fixture()
def service():
    return SearchFetchService(
        search_client=mock_search_client(),
        fetch_client=mock_fetch_client(),
        fetch_allowlist=["example.com"])


@pytest.fixture()
def http(service):
    httpd, port = serve(port=0, service=service)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    yield base, service
    httpd.shutdown()
    httpd.server_close()


# 复用连接 + 绕过任何 env 代理（Windows 回环偶发 ConnectTimeout 的防御）
CLIENT = httpx.Client(trust_env=False, timeout=30.0)


def post(base, path, payload):
    return CLIENT.post(base + path, json=payload)


def get(base, path):
    return CLIENT.get(base + path)


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

def test_search_endpoint_200(service, http):
    base, _ = http
    resp = post(base, "/search", {"query": "Anolis OS 23"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "duckduckgo"
    assert body["count"] >= 1 and body["count"] <= 10
    first = body["results"][0]
    assert first["url"] == "https://openanolis.cn/signature"
    assert first["title"] and first["snippet"] and first["position"] == 1
    assert any("openanolis.cn" in r["url"] for r in body["results"])


def test_search_endpoint_provider_whitelist(service, http):
    base, _ = http
    resp = post(base, "/search", {"query": "q", "provider": "searx"})
    assert resp.status_code == 403
    assert resp.json()["allowed"] == ["duckduckgo", "bing"]


def test_search_endpoint_requires_query(service, http):
    base, _ = http
    assert post(base, "/search", {}).status_code == 400
    assert post(base, "/search", {"query": "  "}).status_code == 400


def test_search_endpoint_all_fail_is_502(http):
    base, _ = http
    failing = SearchFetchService(search_client=mock_search_client(ddg_status=503),
                                 fetch_client=mock_fetch_client(),
                                 fetch_allowlist=["example.com"])
    # 用同一个服务实例换不了 client，起独立端点
    httpd, port = serve(port=0, service=failing)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        resp = post("http://127.0.0.1:%d" % port, "/search", {"query": "q"})
        assert resp.status_code == 502
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_search_limit_clamped_to_10(service, http):
    base, _ = http
    resp = post(base, "/search", {"query": "Anolis OS 23", "limit": 999})
    assert resp.status_code == 200
    assert resp.json()["count"] <= 10


# ---------------------------------------------------------------------------
# /fetch
# ---------------------------------------------------------------------------

def test_fetch_endpoint_200_and_extract(service, http):
    base, _ = http
    resp = post(base, "/fetch", {"url": URL_OK})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK" and body["stack"] == "http"
    assert body["http_status"] == 200
    assert "Anolis OS 23" in body["text"]
    assert body["html_len"] == len(body["html"]) or body["html"]


def test_fetch_endpoint_allowlist_403(service, http):
    base, service = http
    resp = post(base, "/fetch", {"url": "https://evil.com/page"})
    assert resp.status_code == 403
    assert "allowlist" in resp.json()["detail"]


def test_fetch_endpoint_fail_closed_without_allowlist(http):
    no_allow = SearchFetchService(search_client=mock_search_client(),
                                  fetch_client=mock_fetch_client(),
                                  fetch_allowlist=[])
    httpd, port = serve(port=0, service=no_allow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        resp = post("http://127.0.0.1:%d" % port, "/fetch", {"url": URL_OK})
        assert resp.status_code == 403
        assert "fail-closed" in resp.json()["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# HTTP 帧健壮性
# ---------------------------------------------------------------------------

def test_health_and_frame_errors(service, http):
    base, _ = http
    health = get(base, "/health")
    assert health.status_code == 200
    assert health.json()["search_providers"] == ["bing", "duckduckgo"]
    assert get(base, "/search").status_code == 404
    assert post(base, "/nope", {}).status_code == 404
    bad = CLIENT.post(base + "/search", content=b"not json",
                     headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_audit_trail_records_requests(service, http):
    base, svc = http
    post(base, "/search", {"query": "Anolis OS 23"})
    post(base, "/fetch", {"url": "https://evil.com/x"})
    searches = [e for e in svc.audit if e["endpoint"] == "search" and e.get("ok")]
    blocked = [e for e in svc.audit if e["endpoint"] == "fetch" and e.get("blocked")]
    assert searches and blocked          # 成功与拦截都如实记账


# ---------------------------------------------------------------------------
# 并入线2 server.py 的路由委托（agent_browser.server._make_handler）
# ---------------------------------------------------------------------------

try:                                    # 线2 server.py 与本分支并行施工：
    from agent_browser.server import _make_handler as _L2_HANDLER  # noqa: F401
    HAS_LINE2_SERVER = True
except ImportError:                     # 分支上未并入时跳过（并入 main 后自动启用）
    HAS_LINE2_SERVER = False


def _line2_http(service_obj):
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _L2_HANDLER(service_obj))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


@pytest.mark.skipif(not HAS_LINE2_SERVER,
                    reason="线2 server.py 未随线3分支提交（并行工单隔离）；并入后本测试生效")
def test_line2_handler_delegates_search_and_fetch():
    from types import SimpleNamespace
    from agent_browser.search.server import mount_search_fetch
    svc = SimpleNamespace()                          # 线2 BrowserService 替身
    mount_search_fetch(svc, search_client=mock_search_client(),
                       fetch_client=mock_fetch_client(),
                       fetch_allowlist=["example.com"])
    httpd, base = _line2_http(svc)
    try:
        resp = post(base, "/search", {"query": "Anolis OS 23"})
        assert resp.status_code == 200
        assert resp.json()["provider"] == "duckduckgo"
        ok = post(base, "/fetch", {"url": URL_OK})
        assert ok.status_code == 200 and ok.json()["status"] == "OK"
        blocked = post(base, "/fetch", {"url": "https://evil.com/x"})
        assert blocked.status_code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(not HAS_LINE2_SERVER,
                    reason="线2 server.py 未随线3分支提交（并行工单隔离）；并入后本测试生效")
def test_line2_handler_unmounted_search_is_503():
    from types import SimpleNamespace
    svc = SimpleNamespace()                          # 无 search_fetch 属性
    httpd, base = _line2_http(svc)
    try:
        resp = post(base, "/search", {"query": "q"})
        assert resp.status_code == 503
        assert "not mounted" in resp.json()["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
