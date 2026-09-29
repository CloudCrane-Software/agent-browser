# coding: utf-8
"""线3 extract 测试：readability 简化抽取（剪枝/候选/密度/兜底）."""
from __future__ import annotations

from agent_browser.search import extract_main

PROSE = ("Anolis OS 23 ships the ANCK 6.6 kernel with a ten-year lifecycle "
         "and full compatibility with the OpenAnolis ecosystem. " * 6)

NAV = ("<nav><a href='/a'>A</a><a href='/b'>B</a><a href='/c'>C</a></nav>")
FOOTER = ("<footer><p>Copyright OpenAnolis 2026. All rights reserved "
          "and legal boilerplate text repeated. </p></footer>")


def page(body_inner: str) -> str:
    return "<html><head><title>Anolis OS 23 release</title></head>" \
           "<body>%s</body></html>" % body_inner


def test_extract_article_with_noise_pruned():
    html = page(NAV + "<article><h2>Release</h2><p>%s</p></article>" % PROSE + FOOTER)
    art = extract_main(html)
    assert art.node_path.startswith("article")
    assert "ANCK 6.6" in art.text
    assert "Copyright" not in art.text            # footer 剪枝
    assert not art.fell_back
    assert art.title == "Anolis OS 23 release"


def test_extract_main_role_candidate():
    html = page("<div role='main'><p>%s</p></div>" % PROSE)
    art = extract_main(html)
    assert not art.fell_back
    assert "ANCK" in art.text


def test_extract_class_hint_candidate():
    html = page("<div class='post-content entry'><p>%s</p></div>" % PROSE)
    art = extract_main(html)
    assert not art.fell_back
    assert "post-content" in art.node_path


def test_extract_falls_back_to_body():
    html = page("<p>too short</p>")
    art = extract_main(html)
    assert art.fell_back and art.node_path == "body"
    assert "too short" in art.text


def test_extract_link_density_penalty_prefers_prose():
    """链接密集的 content 候选（列表壳）应输给低密度 article 正文。"""
    link_spam = "".join("<a href='/x%d'>item %d text</a> " % (i, i)
                        for i in range(30))
    html = page("<div class='content'>%s</div>"
                "<article><p>%s</p></article>" % (link_spam, PROSE))
    art = extract_main(html)
    assert art.node_path.startswith("article")
    assert art.link_density < 0.5
    assert "item 0 text" not in art.text


def test_extract_empty_and_broken_html_never_raises():
    art = extract_main("")
    assert art.fell_back and art.text == ""
    art2 = extract_main("<html><body><p>未闭合的段落")
    assert art2.fell_back
