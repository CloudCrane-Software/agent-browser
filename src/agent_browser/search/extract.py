# coding: utf-8
"""正文抽取：readability 思路简化版（main/article 启发式 + 文本密度阈值）.

自研判断依据：readability-lxml/readability-python 依赖 lxml+cssselect 或带
权重词典的重算法；本仓只需要“给 fetch 到的 HTML 出干净正文”，用 bs4 启发式
即可覆盖绝大多数文档型页面，**不引重库**：

1. 剪枝：script/style/nav/header/footer/aside/form/noscript/template、
   role=navigation 等噪音节点直接摘除；
2. 候选：``<article>`` > ``<main>`` > ``[role=main]`` > id/class 含
   article|content|main|post|entry|body 的 div/section；
3. 评分（密度阈值）：每个候选取 ``可见文本长 / 子节点数``（链接密度做惩罚，
   readability 同思路——导航/列表链接密度高正文密度低），取最高分；
4. 兜底：无候选/全低分 → ``<body>`` 全文本（诚实降级，不猜）。

输出 :class:`ExtractedArticle`：title/text/len/link_density/node_path——
结构化、可审计（node_path 记录正文来自哪个选择器，不编造来源）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup, Tag

__all__ = ["ExtractedArticle", "extract_main", "MIN_TEXT_LEN", "DENSITY_FLOOR"]

# 密度阈值：正文候选的纯文本至少这个长度才可信（更短视为抽取失败 → 兜底 body）
MIN_TEXT_LEN = 120
# 链接密度惩罚线：>0.5 的候选（列表页/导航壳）降权
LINK_DENSITY_PENALTY = 0.5
DENSITY_FLOOR = 0.0

_NOISE_TAGS = ("script", "style", "noscript", "template", "nav", "header",
               "footer", "aside", "form", "iframe", "svg", "button")
_CANDIDATE_HINT = re.compile(
    r"article|content|main|post|entry|body|story|text", re.I)


@dataclass
class ExtractedArticle:
    """抽取结果（结构化+来源可审计）。"""

    title: str
    text: str
    node_path: str          # 正文来自的 CSS 选择器描述（如 "article" / "body"）
    link_density: float = 0.0
    fell_back: bool = False  # True=候选全败，降级取 body


def _clean_text(node: Tag) -> str:
    return " ".join(node.get_text(" ").split())


def _link_density(node: Tag) -> float:
    """<a> 内文本占比（readability 同名指标）。"""
    total = len(_clean_text(node))
    if total == 0:
        return 1.0
    linked = 0
    for a in node.find_all("a"):
        linked += len(_clean_text(a))
    return linked / total


def _prune(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(attrs={"role": ("navigation", "banner", "complementary")}):
        tag.decompose()
    for tag in soup.find_all(class_=re.compile(r"\b(sidebar|breadcrumb|comment|"
                                               r"advertisement|promo|share|"
                                               r"related|menu|footer|header)\b", re.I)):
        tag.decompose()


def _candidates(soup: BeautifulSoup):
    """候选节点按优先级：article > main > [role=main] > 启发式 id/class。"""
    yield from soup.find_all("article")
    yield from soup.find_all("main")
    yield from soup.find_all(attrs={"role": "main"})
    for tag in soup.find_all(("div", "section")):
        blob = "%s %s" % (" ".join(tag.get("id", "").split()),
                          " ".join(tag.get("class", [])))
        if _CANDIDATE_HINT.search(blob):
            yield tag


def extract_main(html: str) -> ExtractedArticle:
    """从 HTML 抽正文。永不抛解析错（解析失败 → 空结果诚实返回）。"""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001 — bs4 极少抛，保险
        return ExtractedArticle(title="", text="", node_path="none",
                                fell_back=True)
    title = _clean_text(soup.title) if soup.title else ""
    _prune(soup)

    best, best_score, best_path = None, DENSITY_FLOOR - 1.0, "body"
    for cand in _candidates(soup):
        text = _clean_text(cand)
        if len(text) < MIN_TEXT_LEN:
            continue
        density = _link_density(cand)
        # 评分 = 文本长度 ×（链接密度惩罚）；同分取先出现（文档序靠前语义更近主文）
        score = len(text) * (LINK_DENSITY_PENALTY if density > LINK_DENSITY_PENALTY else 1.0)
        if score > best_score:
            best, best_score = cand, score
            best_path = _describe(cand)
    if best is None:
        body = soup.body or soup
        return ExtractedArticle(title=title, text=_clean_text(body),
                                node_path="body", fell_back=True,
                                link_density=round(_link_density(body), 3))
    return ExtractedArticle(title=title, text=_clean_text(best),
                            node_path=best_path,
                            link_density=round(_link_density(best), 3))


def _describe(node: Tag) -> str:
    """给节点一个可审计的定位描述（tag#id.class1.class2）。"""
    parts = [node.name or "?"]
    if node.get("id"):
        parts.append("#%s" % node["id"])
    cls = node.get("class") or []
    if cls:
        parts.append(".%s" % ".".join(cls[:3]))
    return "".join(parts)
