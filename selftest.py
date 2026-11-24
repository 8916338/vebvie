#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则引擎自检：不联网，验证解析能力是否正常。"""

import sys

from lxml import html as lhtml

from bscheck.checker import _jsonpath_nodes
from bscheck.fetcher import fill_template, norm_domain, parse_search_url
from bscheck.rules import RuleResolver

HTML = """
<html><body>
<div class="item" id="first">
  <h3><a href="/book/1/">斗罗大陆</a></h3>
  <p class="author">唐家三少</p>
  <img src="/cover/1.jpg"/>
</div>
<div class="item">
  <h3><a href="/book/2/">三体</a></h3>
  <p class="author">刘慈欣</p>
</div>
<ul id="list">
  <li><a href="/c/1.html">第一章 开始</a></li>
  <li><a href="/c/2.html">第二章 继续</a></li>
</ul>
<div class="content">正文内容&nbsp;第一段<br/>第二段  结束</div>
</body></html>
"""

JSON_DATA = {"code": 0, "data": [
    {"novelName": "斗罗大陆", "novelId": 101, "author": "唐家三少"},
    {"novelName": "三体", "novelId": 102, "author": "刘慈欣"},
]}

fail = 0


def check(name, got, want):
    global fail
    ok = got == want
    if not ok:
        fail += 1
    print("  %s %-46s got=%r" % ("✓" if ok else "✗", name, got))
    if not ok:
        print("      期望: %r" % (want,))


def main():
    doc = lhtml.fromstring(HTML)
    r = RuleResolver()

    print("\n[DefaultRule 语法]")
    check("class.item → 数量", len(r._select(doc, "class.item")), 2)
    check("class.item@tag.h3@tag.a@text", r.eval_one(doc, "class.item@tag.h3@tag.a@text"), "斗罗大陆")
    check("class.item.1@tag.a@text (索引)", r.eval_one(doc, "class.item.1@tag.h3@tag.a@text"), "三体")
    check("class.author@text", r.eval_one(doc, "class.author@text"), "唐家三少")
    check("id.list@tag.li.0@tag.a@text", r.eval_one(doc, "id.list@tag.li.0@tag.a@text"), "第一章 开始")

    print("\n[取值 @href / @src / @html]")
    check("@href", r.eval_one(doc, "class.item@tag.h3@tag.a@href"), "/book/1/")
    check("@src", r.eval_one(doc, "class.item@tag.img@src"), "/cover/1.jpg")
    check("@html 非空", bool(r.eval_one(doc, "class.content@html")), True)

    print("\n[XPath / CSS 属性选择器]")
    check("xpath //div[@id='first']//a/text()",
          r.eval_one(doc, "//div[@id='first']//a/text()"), "斗罗大陆")
    check("xpath 取属性", r.eval_one(doc, "//div[@id='first']//a/@href"), "/book/1/")

    print("\n[正则清洗 ##]")
    check("删除匹配（两处都删）", r.eval_one(doc, "class.content@text##第.段"), "正文内容 结束")
    check("只删一处", r.eval_one(doc, "class.content@text##第一段"), "正文内容 第二段 结束")
    check("替换分组", r.eval_one(doc, "class.author@text##^(.*)$##$1老师"), "唐家三少老师")

    print("\n[组合 && ||]")
    check("|| 取第一个有结果的",
          r.eval_one(doc, "class.nonexist@text||class.author@text"), "唐家三少")
    check("&& 合并（2 author + 2 章节）", len(r.eval(doc, "class.author@text&&id.list@tag.li@tag.a@text")), 4)

    print("\n[JSONPath]")
    nodes = _jsonpath_nodes(JSON_DATA, "$.data[*]")
    check("$.data[*] 节点数", len(nodes), 2)
    check("$.data[0].novelName", r.eval_one(JSON_DATA, "$.data[0].novelName"), "斗罗大陆")
    check("首条取字段", r.eval_one(nodes[0], "$.novelName"), "斗罗大陆")

    print("\n[搜索地址解析]")
    u, o = parse_search_url('https://x.com/s,{"method":"POST","body":"k={{key}}"}')
    check("拆出地址", u, "https://x.com/s")
    check("拆出 method", o.get("method"), "POST")
    u2, o2 = parse_search_url("/search?key={{key}}")
    check("相对地址原样", u2, "/search?key={{key}}")
    check("模板替换", fill_template("/s?key={{key}}&p={{page}}", {"key": "abc", "page": 1}),
          "/s?key=abc&p=1")

    print("\n[域名归一]")
    check("去 www/协议", norm_domain("https://www.SuDuGu.org/a/b"), "sudugu.org")
    check("保留子域", norm_domain("http://m.xiaobanzhu.com"), "m.xiaobanzhu.com")

    print("\n[不执行 JS]")
    from bscheck.rules import JsRuleError
    try:
        r.eval_one(doc, "class.item@text<js>result</js>")
        print("  ✗ JS 规则应当抛异常")
        globals()["fail"] += 1
    except JsRuleError:
        print("  ✓ JS 规则正确抛 JsRuleError")

    print("\n" + ("全部通过 ✅" if fail == 0 else "有 %d 项未通过 ❌" % fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
