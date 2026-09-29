# coding: utf-8
"""线3 真实冒烟（GPU 机 anolis-gpu-01）：search("Anolis OS 23") + 服务端点 200.

零 mock、真实公网调用（预算 ≤5 页次）：DDG HTML 1 次 + Bing 兜底 1 次 +
端点 /search（provider=bing）1 次 + /fetch www.bing.com 根页 1 次（robots+页面）。
输出 JSON 到 stdout（由调用方落盘）。
"""
import json
import sys
import threading
import time

sys.path.insert(0, "/tmp/l3search/src")

import httpx  # noqa: E402

from agent_browser.search import (  # noqa: E402
    BingSearch, DdgHtmlSearch, FETCH_UA, FetchEngine, search)
from agent_browser.search.server import SearchFetchService, serve  # noqa: E402

out = {"host_check": {"python": sys.version.split()[0],
                      "httpx": httpx.__version__}}
allowlist = ["openanolis.cn", "bing.com", "duckduckgo.com"]

# ---- 1) 真实搜索（DDG 先行，失败/超时兜底 Bing）----------------------------
client = httpx.Client(headers={"User-Agent": FETCH_UA}, timeout=15.0,
                      follow_redirects=True)
t0 = time.monotonic()
try:
    resp = search("Anolis OS 23",
                  providers=[DdgHtmlSearch(client), BingSearch(client)])
    out["search"] = {
        "provider": resp.provider,
        "fallback_from": list(resp.fallback_from),
        "count": len(resp.results),
        "elapsed_s": round(time.monotonic() - t0, 2),
        "results": [{"position": i + 1, "url": r.url, "title": r.title,
                     "snippet": r.snippet[:140]}
                    for i, r in enumerate(resp.results)],
    }
    out["search"]["acceptance_ge3_with_openanolis"] = bool(
        len(resp.results) >= 3
        and sum(1 for r in resp.results if "openanolis" in r.url) >= 3)
except Exception as exc:  # noqa: BLE001
    out["search"] = {"error": repr(exc), "elapsed_s": round(time.monotonic() - t0, 2)}

# ---- 2) 服务端点 200 验收（127.0.0.1 回环，real provider 链）----------------
svc = SearchFetchService(fetch_allowlist=allowlist)
httpd, port = serve(port=0, service=svc)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port
local = httpx.Client(trust_env=False, timeout=60.0)

t0 = time.monotonic()
r_search = local.post(base + "/search",
                      json={"query": "Anolis OS 23", "provider": "bing"})
body = r_search.json()
out["endpoint_search"] = {
    "http_status": r_search.status_code,
    "provider": body.get("provider"),
    "count": body.get("count"),
    "first_results": [{"url": r["url"], "title": r["title"]}
                      for r in body.get("results", [])[:3]],
    "elapsed_s": round(time.monotonic() - t0, 2),
}

t0 = time.monotonic()
r_fetch = local.post(base + "/fetch", json={"url": "https://www.bing.com/"})
fbody = r_fetch.json()
out["endpoint_fetch"] = {
    "http_status": r_fetch.status_code,
    "status": fbody.get("status"), "stack": fbody.get("stack"),
    "final_url": fbody.get("final_url"), "http": fbody.get("http_status"),
    "text_head": (fbody.get("text") or "")[:120],
    "text_len": len(fbody.get("text") or ""),
    "elapsed_s": round(time.monotonic() - t0, 2),
}

# allowlist 对 fetch 生效（越域必须 403）
r_bad = local.post(base + "/fetch", json={"url": "https://example.com/"})
out["endpoint_fetch_blocked"] = {"http_status": r_bad.status_code}

httpd.shutdown()
httpd.server_close()

out["verdict"] = {
    "search_ok": out.get("search", {}).get("acceptance_ge3_with_openanolis", False),
    "endpoint_search_200": out["endpoint_search"]["http_status"] == 200,
    "endpoint_fetch_200": out["endpoint_fetch"]["http_status"] == 200,
    "allowlist_403": out["endpoint_fetch_blocked"]["http_status"] == 403,
}
print(json.dumps(out, ensure_ascii=False, indent=1))
