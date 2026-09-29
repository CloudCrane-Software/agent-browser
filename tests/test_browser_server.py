# coding: utf-8
"""线2 浏览器动作 server 测试——真实 HTTP 栈（ThreadingHTTPServer on 127.0.0.1
随机口）+ fake 驱动池；覆盖 health/建会话/五动作/allowlist 硬拦/审计卫生/销毁.

注：线3 的 search/fetch server 测试在 tests/test_server.py（对方命名在先），
本文件只测 agent_browser/server.py 的浏览器动作面，故改名避撞。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_browser.server import BrowserService, create_server
from agent_browser.sessions import BrowserPool
from test_pool import FakePoolDriver


def make_service(max_size=2, **pool_kw):
    pool_kw.setdefault("driver_factory", FakePoolDriver)
    pool_kw.setdefault("max_size", max_size)
    pool_kw.setdefault("enable_watchdog", False)
    pool = BrowserPool(**pool_kw)
    return BrowserService(pool=pool), pool


def make_http(service):
    httpd, _ = create_server(bind_ip="127.0.0.1", port=0, service=service)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    return httpd, url


def request(url, path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url + path, data=data, method=method or
                                 ("POST" if data is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(body) if "json" in ctype else body), ctype
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body), ""
        except ValueError:
            return exc.code, body, ""


@pytest.fixture()
def server():
    service, pool = make_service()
    httpd, url = make_http(service)
    yield service, pool, url
    httpd.shutdown()
    httpd.server_close()
    service.shutdown()


def new_session(url, allowlist=("example.com",)):
    status, payload, _ = request(url, "/session", {"allowlist": list(allowlist)})
    assert status == 200, payload
    return payload["session_id"]


# ---------------------------------------------------------------- health

def test_health_ok_with_pool_stats(server):
    _service, _pool, url = server
    status, payload, ctype = request(url, "/health")
    assert status == 200 and ctype.startswith("application/json")
    assert payload["ok"] is True and payload["service"] == "agent-browser"
    assert payload["driver"] == "fake-pool"
    assert payload["pool"]["max_size"] == 2


# ---------------------------------------------------------------- 建会话

def test_create_session_requires_nonempty_allowlist_fail_closed(server):
    service, _pool, url = server
    for bad in ([], None, "example.com"):
        status, payload, _ = request(url, "/session", {"allowlist": bad})
        assert status == 400, (bad, payload)
    assert len(service.pool) == 0


def test_create_session_returns_id_and_optional_first_goto(server):
    _service, pool, url = server
    sid = new_session(url)
    assert sid and len(pool) == 1
    status, payload, _ = request(
        url, "/session",
        {"allowlist": ["example.com"], "url": "https://example.com/"})
    assert status == 200 and payload["session_id"]
    assert len(pool) == 2


def test_create_session_with_blocked_first_goto_destroys_session(server):
    service, pool, url = server
    status, payload, _ = request(
        url, "/session", {"allowlist": ["example.com"],
                          "url": "https://evil.io/"})
    assert status == 403 and "ALLOWLIST_VIOLATION" in payload["error"]
    assert len(pool) == 0                             # 首跳被拦不留半开会话
    blocks = [e for e in service.audit.events() if e["type"] == "block"]
    assert blocks[0]["reason"] == "ALLOWLIST_VIOLATION"


# ---------------------------------------------------------------- 五动作

def test_goto_click_type_extract_roundtrip(server):
    _service, _pool, url = server
    sid = new_session(url)
    status, payload, _ = request(url, "/session/%s/goto" % sid,
                                 {"url": "https://example.com/"})
    assert status == 200 and payload["url"] == "https://example.com/"
    status, payload, _ = request(url, "/session/%s/click" % sid,
                                 {"selector": "h1"})
    assert status == 200 and payload["clicked"] == "h1"
    status, payload, _ = request(url, "/session/%s/type" % sid,
                                 {"selector": "input#q", "text": "SECRET-TEXT"})
    assert status == 200
    assert "SECRET-TEXT" not in json.dumps(payload)   # 输入文本不回显
    assert payload["typed_len"] == len("SECRET-TEXT")
    status, payload, _ = request(url, "/session/%s/extract" % sid, {})
    assert status == 200 and payload["text"]


def test_screenshot_returns_png_bytes(server):
    _service, _pool, url = server
    sid = new_session(url)
    status, body, ctype = request(url, "/session/%s/screenshot" % sid, {})
    assert status == 200 and ctype == "image/png"
    assert isinstance(body, bytes) and body


def test_goto_disallowed_host_hard_blocked(server):
    service, _pool, url = server
    sid = new_session(url)
    for target in ("https://evil.io/", "ftp://example.com/x"):
        status, payload, _ = request(url, "/session/%s/goto" % sid,
                                     {"url": target})
        assert status == 403 and payload["error"] == "ALLOWLIST_VIOLATION"
    blocks = [e for e in service.audit.events() if e["type"] == "block"]
    assert blocks[-1]["target"] == "ftp://example.com/x"


def test_redirect_to_disallowed_host_blocked(server):
    service, _pool, url = server

    class RedirectingDriver(FakePoolDriver):
        def goto(self, target):
            return "https://evil.io/"                 # 模拟重定向逃逸
    service.pool._factory = RedirectingDriver
    sid = new_session(url)
    status, payload, _ = request(url, "/session/%s/goto" % sid,
                                 {"url": "https://example.com/"})
    assert status == 403
    assert payload["error"] == "ALLOWLIST_VIOLATION_REDIRECT"
    blocks = [e for e in service.audit.events() if e["type"] == "block"
              and e.get("reason") == "ALLOWLIST_VIOLATION_REDIRECT"]
    assert blocks and blocks[0]["target"] == "https://evil.io/"


# ---------------------------------------------------------------- 错误路径

def test_unknown_session_404_and_unknown_action_404(server):
    _service, _pool, url = server
    status, payload, _ = request(url, "/session/bsess-nope/goto",
                                 {"url": "https://example.com/"})
    assert status == 404
    sid = new_session(url)
    status, payload, _ = request(url, "/session/%s/fly" % sid, {})
    assert status == 404 and "unknown action" in payload["error"]


def test_bad_requests_400(server):
    _service, _pool, url = server
    sid = new_session(url)
    for path, payload in (
            ("/session/%s/goto" % sid, {}),
            ("/session/%s/click" % sid, {}),
            ("/session/%s/type" % sid, {"selector": "a"}),
            ("/session/%s/type" % sid, {"text": "b"}),
            ("/session/%s/extract" % sid, {"selector": 5})):
        status, _payload, _ = request(url, path, payload)
        assert status == 400, (path, payload)


def test_driver_error_maps_to_500_with_detail(server):
    service, _pool, url = server

    class BrokenDriver(FakePoolDriver):
        def goto(self, target):
            raise RuntimeError("browser crashed")
    service.pool._factory = BrokenDriver
    sid = new_session(url)
    status, payload, _ = request(url, "/session/%s/goto" % sid,
                                 {"url": "https://example.com/"})
    assert status == 500 and payload["error"] == "DRIVER_ERROR"
    errs = [e for e in service.audit.events() if e["type"] == "error"]
    assert errs and errs[0]["reason"] == "DRIVER_ERROR"


# ---------------------------------------------------------------- 销毁与池满

def test_close_endpoint_releases_slot(server):
    _service, pool, url = server
    sid = new_session(url)
    status, payload, _ = request(url, "/session/%s/close" % sid, {})
    assert status == 200 and payload["closed"] == sid
    assert len(pool) == 0
    status, _payload, _ = request(url, "/session/%s/goto" % sid,
                                  {"url": "https://example.com/"})
    assert status == 404


def test_delete_session_endpoint(server):
    _service, pool, url = server
    sid = new_session(url)
    status, _payload, _ = request(url, "/session/%s" % sid, method="DELETE")
    assert status == 200 and len(pool) == 0


def test_pool_exhaustion_maps_to_503(server):
    service, pool, url = server
    sids = [new_session(url) for _ in range(2)]       # max_size=2
    status, payload, _ = request(url, "/session", {"allowlist": ["example.com"]})
    assert status == 503                              # 池满：服务过载语义
    assert payload["error"] == "POOL_UNAVAILABLE"
    assert "exhausted" in payload["detail"].lower()
    for sid in sids:
        request(url, "/session/%s/close" % sid, {})
    assert len(pool) == 0


def test_type_value_never_in_audit(server):
    service, _pool, url = server
    sid = new_session(url)
    request(url, "/session/%s/type" % sid,
            {"selector": "input#q", "text": "AUDIT-SECRET-XYZ"})
    assert "AUDIT-SECRET-XYZ" not in repr(service.audit.events())
    type_events = [e for e in service.audit.events()
                   if e["type"] == "action" and e.get("kind") == "type"]
    assert type_events[0]["value_len"] == len("AUDIT-SECRET-XYZ")
