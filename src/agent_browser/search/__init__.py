# coding: utf-8
"""搜索/抓取能力通用化（线3）：两栈策略——轻量 HTTP 栈 + JS 渲染栈接缝.

- :mod:`.search`   —— ``SearchProvider`` 协议 + DDG HTML 版（无需 key）+ Bing 兜底；
- :mod:`.fetch`    —— 两栈路由：httpx 直抓先行，403/429/空 body/meta refresh/SPA 特征
  判定转渲染栈（渲染栈=线1/线2 的 playwright，经 ``render_fn`` 接缝注入，本包不依赖）；
- :mod:`.extract`  —— readability 思路简化正文抽取（main/article 启发式+密度阈值，纯 bs4）。

路由策略（自研判断，写明依据）：先轻后重——HTTP 200+内容充分→直出；
403/JS challenge/SPA→转渲染栈。搜索源白名单内置（duckduckgo/bing）。
"""
from __future__ import annotations

from .search import (
    MAX_RESULTS_PER_QUERY,
    SEARCH_PROVIDER_ALLOWLIST,
    BingSearch,
    DdgHtmlSearch,
    SearchProvider,
    SearchQueryError,
    SearchResult,
    SearchResponse,
    is_search_provider,
    normalize_url,
    search,
)
from .fetch import (
    FETCH_UA,
    FetchEngine,
    FetchResult,
    FetchBlocked,
    NEEDS_RENDER_REASONS,
    classify_fetch,
)
from .extract import (
    ExtractedArticle,
    extract_main,
)

__all__ = [
    "MAX_RESULTS_PER_QUERY",
    "SEARCH_PROVIDER_ALLOWLIST",
    "BingSearch",
    "DdgHtmlSearch",
    "SearchProvider",
    "SearchQueryError",
    "SearchResult",
    "SearchResponse",
    "is_search_provider",
    "normalize_url",
    "search",
    "FETCH_UA",
    "FetchEngine",
    "FetchResult",
    "FetchBlocked",
    "NEEDS_RENDER_REASONS",
    "classify_fetch",
    "ExtractedArticle",
    "extract_main",
]
