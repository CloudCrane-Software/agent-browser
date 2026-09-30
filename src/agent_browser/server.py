# coding: utf-8
"""无头服务入口：stdlib http.server 暴露池化浏览器动作（tailnet-only）.

接口（对齐工单口径）：

- ``GET  /health``                       → 200 {"ok": true, ...}；
- ``POST /session`` ``{"allowlist": [...], "url"?: "..."}``
                                          → {"session_id": ...}（allowlist
  非空硬校验，fail-closed，与 task 层同款红线）；
- ``POST /session/{id}/goto``    ``{"url"}``        → {"url": 最终URL}；
- ``POST /session/{id}/click``   ``{"selector"}``   → {"clicked": ...}；
- ``POST /session/{id}/type``    ``{"selector","text"}`` → 只回 ``typed_len``
  （输入文本不回显不进审计，与仓审计红线一致）；
- ``POST /session/{id}/extract`` ``{"selector"?}``  → {"text": markdown/YAML}；
- ``POST /session/{id}/screenshot`` ``{}``           → image/png 字节；
- ``POST /session/{id}/close``（或 ``DELETE /session/{id}``）→ 归还销毁。

绑定纪律：默认绑 ``100.64.0.7:8125``（GPU 机 tailnet 地址）——**只听 tailnet**，
不经 docker0/公网口暴露（对齐 fs_server 先例与 BP §2.2 "9222 暴露=RCE 面" 的
教训；本服务无 TLS/认证，靠 tailnet 边界，故绝不改绑 0.0.0.0）。

线程模型：ThreadingHTTPServer + 每会话单线程执行器——playwright sync API 要求
同一驱动全部调用同线程（见 playwright_driver 模块注释），故每会话动作、优雅
关闭都路由进该会话的执行器；进程树兜底杀（池看门狗）则线程安全、不经过它。
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .sessions import BrowserPool
from .task import AuditTrail, host_allowed

__all__ = ["BrowserService", "create_server", "main"]

MAX_BODY_BYTES = 1 << 20            # 1MB 请求体上限
ALLOWED_ACTIONS = ("goto", "click", "type", "extract", "screenshot", "close")


@dataclass
class _ServiceSession:
    session: object                  # BrowserSession（池借出物）
    allowlist: list
    executor: ThreadPoolExecutor
    created_at: str = ""
    meta: dict = field(default_factory=dict)


class BrowserService:
    """HTTP 之下的纯逻辑层（可独立单测，不启 socket）."""

    def __init__(self, pool=None, action_timeout_s=45.0, tenant_id="agent-browser",
                 acquire_timeout_s=5.0):
        # 注意不可写 `pool or BrowserPool()`：空池 __len__==0 为 falsy 会偷换默认池
        self.pool = pool if pool is not None else BrowserPool()
        self.action_timeout_s = float(action_timeout_s)
        # 池满快速失败：HTTP 请求不宜挂 30s 等 acquire（默认 5s 内让位）
        self.acquire_timeout_s = float(acquire_timeout_s)
        self.tenant_id = tenant_id
        self.audit = AuditTrail()
        self._sessions = {}
        self._lock = threading.Lock()
        self._driver_executors = {}   # id(driver) → executor（优雅关闭的路由缝）
        self.pool.close_hook = self._graceful_close

    # ------------------------------------------------------------ 生命周期

    def create_session(self, allowlist, url=None):
        """创建会话；allowlist 必须非空列表（fail-closed）；url 可选即刻首跳."""
        if not isinstance(allowlist, list) or not allowlist or \
                not all(isinstance(a, str) and a.strip() for a in allowlist):
            return 400, {"error": "allowlist must be a non-empty list of domains "
                                  "(fail-closed)"}
        try:
            session = self.pool.acquire(tenant_id=self.tenant_id,
                                        timeout_s=self.acquire_timeout_s)
        except Exception as exc:                       # noqa: BLE001 — 池满/池关
            return 503, {"error": "POOL_UNAVAILABLE",
                         "detail": str(exc)}
        executor = ThreadPoolExecutor(max_workers=1,
                                      thread_name_prefix="absess-" + session.session_id)
        with self._lock:
            self._driver_executors[id(session.driver)] = executor
            record = _ServiceSession(session=session, allowlist=list(allowlist),
                                     executor=executor)
            self._sessions[session.session_id] = record
        self.audit.append("session.create", session_id=session.session_id,
                          tenant_id=session.tenant_id, ok=True)
        if url:
            status, payload = self.action(session.session_id, "goto", {"url": url})
            if status != 200:
                self.destroy_session(session.session_id)   # 首跳被拦即销毁，不留半开
                return status, payload
        return 200, {"session_id": session.session_id}

    def destroy_session(self, session_id):
        with self._lock:
            record = self._sessions.pop(session_id, None)
        if record is None:
            return 404, {"error": "unknown session"}
        try:
            record.executor.submit(lambda: None).result(timeout=self.action_timeout_s)
        except Exception:                          # noqa: BLE001 — 排空卡死也要归还
            pass
        # 先归还（池的优雅关闭会路由进还活着的执行器，保证线程亲和），
        # 再关执行器——顺序反了 close 就只能走进程树兜底
        self.pool.release(session_id)
        record.executor.shutdown(wait=False, cancel_futures=True)
        self.audit.append("session.close", session_id=session_id, ok=True)
        return 200, {"closed": session_id}

    def _graceful_close(self, driver):
        """池归还时的优雅关闭路径：路由回会话执行器（线程亲和）."""
        executor = self._driver_executors.get(id(driver))
        if executor is None:
            driver.close()
            return
        try:
            executor.submit(self._safe_close, driver).result(timeout=20)
        except Exception:                          # noqa: BLE001 — 超时交给进程树兜底
            pass
        finally:
            self._driver_executors.pop(id(driver), None)

    @staticmethod
    def _safe_close(driver):
        try:
            driver.close()
        except Exception:                          # noqa: BLE001 — 尽力优雅
            pass

    def shutdown(self):
        with self._lock:
            sids = list(self._sessions)
            records = [self._sessions.pop(s) for s in sids]
        for record in records:
            try:
                record.executor.submit(lambda: None).result(timeout=5)
            except Exception:                      # noqa: BLE001
                pass
        # 先池归还（优雅关闭路由进还活着的执行器），再关执行器
        self.pool.shutdown()
        for record in records:
            record.executor.shutdown(wait=False)

    # ------------------------------------------------------------ 动作

    def health(self):
        return 200, {
            "ok": True,
            "service": "agent-browser",
            "version": __version__,
            "driver": self.pool.driver_name,
            "pool": self.pool.stats(),
        }

    def action(self, session_id, kind, payload):
        """执行单动作，返回 (http_status, json dict 或 ("png", bytes))."""
        with self._lock:
            record = self._sessions.get(session_id)
        if record is None:
            return 404, {"error": "unknown session"}
        if kind not in ALLOWED_ACTIONS:
            return 404, {"error": "unknown action: %s" % kind}
        if kind == "close":
            return self.destroy_session(session_id)

        payload = payload or {}
        try:
            if kind == "goto":
                url = payload.get("url")
                if not isinstance(url, str) or not url.strip():
                    return 400, {"error": "url required"}
                if not host_allowed(url, record.allowlist):
                    self.audit.append("block", session_id=session_id, kind="goto",
                                      target=url, ok=False,
                                      reason="ALLOWLIST_VIOLATION")
                    return 403, {"error": "ALLOWLIST_VIOLATION", "url": url}
                final_url, err = self._run(record, lambda d: d.goto(url))
                if err is not None:
                    return err
                if final_url != url and not host_allowed(final_url, record.allowlist):
                    # 重定向落点也要在 allowlist 内（与 task 层同口径）；被拦即
                    # 销毁会话——否则页面停在被禁域，后续 extract/screenshot
                    # 仍可读取（bfix-R1：此前仅返回 403 不回收会话）
                    self.audit.append("block", session_id=session_id, kind="goto",
                                      target=final_url, ok=False,
                                      reason="ALLOWLIST_VIOLATION_REDIRECT")
                    self.destroy_session(session_id)
                    return 403, {"error": "ALLOWLIST_VIOLATION_REDIRECT",
                                 "url": final_url}
                self.pool.touch(session_id)
                self.audit.append("action", session_id=session_id, kind="goto",
                                  target=url, ok=True)
                return 200, {"url": final_url}

            if kind == "click":
                selector = payload.get("selector")
                if not isinstance(selector, str) or not selector.strip():
                    return 400, {"error": "selector required"}
                outcome, err = self._run(record, lambda d: d.click(selector))
                if err is not None:
                    return err
                # 点击可触发页面内导航（playwright click 会等待已发起的导航
                # commit）——落点与 goto 同款复检 allowlist；被拦即销毁会话，
                # 不留可读的越域页面（bfix-R1：此前 click 零 allowlist 校验，
                # 一步即逃逸白名单）。
                # 兼容缝（如实登记）：协议六方法不含 current_url，无读数的
                # 旧驱动在此退化为不设防——本仓两驱动（playwright/Fake）均有
                # 读数，生产面不受影响。
                final_url, err = self._run(
                    record, lambda d: getattr(d, "current_url", "") or "")
                if err is not None:
                    return err
                if final_url and not host_allowed(final_url, record.allowlist):
                    self.audit.append("block", session_id=session_id, kind="click",
                                      target=final_url, ok=False,
                                      reason="ALLOWLIST_VIOLATION_CLICK")
                    self.destroy_session(session_id)
                    return 403, {"error": "ALLOWLIST_VIOLATION_CLICK",
                                 "url": final_url}
                self.pool.touch(session_id)
                self.audit.append("action", session_id=session_id, kind="click",
                                  target=selector, ok=True)
                return 200, outcome

            if kind == "type":
                selector = payload.get("selector")
                text = payload.get("text")
                if not isinstance(selector, str) or not selector.strip() or \
                        not isinstance(text, str):
                    return 400, {"error": "selector and text required"}
                outcome, err = self._run(record, lambda d: d.type(selector, text))
                if err is not None:
                    return err
                self.pool.touch(session_id)
                # 输入文本不回显不进审计，只报长度（与仓审计红线一致）
                self.audit.append("action", session_id=session_id, kind="type",
                                  target=selector, ok=True,
                                  value_len=len(text))
                return 200, {"typed_len": outcome["typed_len"],
                             "selector": outcome["selector"]}

            if kind == "extract":
                selector = payload.get("selector", "")
                if selector is None:
                    selector = ""
                if not isinstance(selector, str):
                    return 400, {"error": "selector must be a string"}
                outcome, err = self._run(record, lambda d: d.extract(selector))
                if err is not None:
                    return err
                self.pool.touch(session_id)
                self.audit.append("action", session_id=session_id, kind="extract",
                                  target=selector, ok=True)
                return 200, {"text": outcome}

            if kind == "screenshot":
                outcome, err = self._run(record, lambda d: d.screenshot())
                if err is not None:
                    return err
                self.pool.touch(session_id)
                self.audit.append("action", session_id=session_id, kind="screenshot",
                                  target="", ok=True)
                return 200, ("png", outcome)       # (kind-tag, bytes)

            return 404, {"error": "unknown action: %s" % kind}
        except Exception as exc:                       # noqa: BLE001 — 最后防线
            self.audit.append("error", session_id=session_id, kind=kind, ok=False,
                              reason="INTERNAL_ERROR", detail=str(exc))
            return 500, {"error": "INTERNAL_ERROR", "detail": str(exc)}

    def _run(self, record, fn):
        """把驱动调用提交到会话执行器（线程亲和），统一超时与错误归一.

        ``fn(driver)`` 在此绑定驱动参数——执行器只跑无参闭包（线程亲和由
        执行器单线程保证，驱动对象不跨线程暴露给 HTTP 线程）。
        """
        future = record.executor.submit(lambda: fn(record.session.driver))
        try:
            outcome = future.result(timeout=self.action_timeout_s)
        except Exception as exc:                       # noqa: BLE001
            reason = "TIMEOUT" if "TimeoutError" in type(exc).__name__ \
                else "DRIVER_ERROR"
            self.audit.append("error", session_id=record.session.session_id,
                              kind="action", ok=False, reason=reason,
                              detail=str(exc))
            code = 504 if reason == "TIMEOUT" else 500
            return None, (code, {"error": reason, "detail": str(exc)})
        return outcome, None

    def session_count(self):
        with self._lock:
            return len(self._sessions)


# ---------------------------------------------------------------------------
# HTTP 薄壳
# ---------------------------------------------------------------------------

def _make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agent-browser/" + __version__

        def log_message(self, fmt, *args):         # noqa: A003 — 一行式，无 body
            sys.stderr.write("[agent-browser] %s - %s\n"
                             % (self.address_string(), fmt % args))

        # -------------------------------------------------- helpers

        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_png(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return None, (413, {"error": "body too large"})
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}, None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None, (400, {"error": "invalid JSON body"})
            if not isinstance(data, dict):
                return None, (400, {"error": "JSON object expected"})
            return data, None

        # -------------------------------------------------- routes

        def do_GET(self):                          # noqa: N802 — http.server 约定
            if self.path.split("?")[0] == "/health":
                status, payload = service.health()
                self._send_json(status, payload)
            else:
                self._send_json(404, {"error": "not found"})

        def do_DELETE(self):                       # noqa: N802
            parts = self.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "session":
                status, payload = service.destroy_session(parts[1])
                self._send_json(status, payload)
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):                         # noqa: N802
            parts = self.path.strip("/").split("/")
            body, err = self._read_json()
            if err is not None:
                self._send_json(*err)
                return
            if parts == ["session"]:
                status, payload = service.create_session(
                    body.get("allowlist"), url=body.get("url"))
                self._send_json(status, payload)
                return
            if len(parts) == 3 and parts[0] == "session":
                status, payload = service.action(parts[1], parts[2], body)
                if isinstance(payload, tuple) and len(payload) == 2 \
                        and payload[0] == "png":
                    self._send_png(payload[1])
                else:
                    self._send_json(status, payload)
                return
            self._send_json(404, {"error": "not found"})

    return Handler


def create_server(bind_ip=None, port=None, service=None, pool_kwargs=None):
    """建 ThreadingHTTPServer（不阻塞）；service/pool 可注入供测试."""
    bind_ip = bind_ip or os.environ.get("AB_BIND_IP", "100.64.0.7")
    port = int(port or os.environ.get("AB_PORT", "8125"))
    if service is None:
        pool_kwargs = dict(pool_kwargs or {})
        pool_kwargs.setdefault("max_size",
                               int(os.environ.get("AB_MAX_SESSIONS", "8")))
        pool_kwargs.setdefault("idle_ttl_s", _env_float("AB_IDLE_TTL_S", 900.0))
        env_root = os.environ.get("AB_CONTEXT_ROOT")
        if env_root:                       # systemd unit 设定每会话 user-data-dir 根
            pool_kwargs.setdefault("context_root", env_root)
        pool_kwargs.setdefault("driver_name", "playwright")
        service = BrowserService(pool=BrowserPool(**pool_kwargs))
    httpd = ThreadingHTTPServer((bind_ip, port), _make_handler(service))
    httpd.daemon_threads = True
    return httpd, service


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def main(argv=None):
    httpd, service = create_server()
    host, port = httpd.server_address[:2]

    def _terminate(_sig, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    sys.stderr.write("[agent-browser] serving on http://%s:%s (tailnet only)\n"
                     % (host, port))
    sys.stderr.flush()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        service.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
