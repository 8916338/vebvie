#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单条书源的检测流水线：搜索 → 目录 → 正文，全程计时。"""

import json
import re
import time
from urllib.parse import quote, urlparse

from lxml import html as lhtml

from .fetcher import (Fetcher, fill_template, is_valid_host, join_url, make_headers,
                      norm_domain, parse_search_url)
from .rules import JsRuleError, RuleResolver

# 结果分类
OK = "ok"
F_NO_SEARCH = "无搜索地址"
F_BAD_URL = "地址无效"
F_JS = "规则依赖JS"
F_SEARCH_NET = "搜索请求失败"
F_DNS = "域名解析失败"
F_SEARCH_EMPTY = "搜索无结果"
F_SEARCH_PARSE = "搜索规则解析失败"
F_TOC_NET = "目录页打不开"
F_TOC_EMPTY = "目录为空"
F_CHAP_NET = "章节页打不开"
F_CONTENT_EMPTY = "正文为空"
F_CONTENT_SHORT = "正文过短"
F_TIMEOUT = "超时"
F_ERROR = "异常"

# 失败卡在哪一步（给失败书源报告用）
STEP_PRE = "前置"
STEP_SEARCH = "搜索"
STEP_TOC = "目录"
STEP_CONTENT = "正文"
STEP_RULE = "规则"
STEP_OTHER = "其它"

STEP_ORDER = {STEP_PRE: 0, STEP_SEARCH: 1, STEP_TOC: 2, STEP_CONTENT: 3,
              STEP_RULE: 4, STEP_OTHER: 5}

_REASON_STEP = {
    F_NO_SEARCH: STEP_PRE,
    F_BAD_URL: STEP_PRE,
    F_JS: STEP_RULE,
    F_SEARCH_NET: STEP_SEARCH,
    F_DNS: STEP_SEARCH,
    F_SEARCH_EMPTY: STEP_SEARCH,
    F_SEARCH_PARSE: STEP_SEARCH,
    F_TOC_NET: STEP_TOC,
    F_TOC_EMPTY: STEP_TOC,
    F_CHAP_NET: STEP_CONTENT,
    F_CONTENT_EMPTY: STEP_CONTENT,
    F_CONTENT_SHORT: STEP_CONTENT,
    F_TIMEOUT: STEP_OTHER,
    F_ERROR: STEP_OTHER,
}


def reason_step(reason):
    """把失败原因归到 前置 / 搜索 / 目录 / 正文 / 规则 / 其它 六类。"""
    return _REASON_STEP.get((reason or "").strip(), STEP_OTHER)


class CheckResult:
    __slots__ = ("ok", "reason", "delay_ms", "t_search", "t_toc", "t_content",
                 "book", "chapters", "content_len", "detail",
                 "book_url", "first_chapter", "search_url")

    def __init__(self):
        self.ok = False
        self.reason = ""
        self.delay_ms = 0
        self.t_search = 0
        self.t_toc = 0
        self.t_content = 0
        self.book = ""
        self.chapters = 0
        self.content_len = 0
        self.detail = ""
        # 中间结果：修书源时要用（不写进报告）
        self.book_url = ""
        self.first_chapter = ""
        self.search_url = ""

    def to_dict(self):
        return {
            "ok": self.ok,
            "reason": self.reason,
            "delayMs": self.delay_ms,
            "searchMs": self.t_search,
            "tocMs": self.t_toc,
            "contentMs": self.t_content,
            "book": self.book,
            "chapters": self.chapters,
            "contentLen": self.content_len,
            "detail": self.detail,
        }


def _parse_html(text):
    if not text:
        return None
    try:
        return lhtml.fromstring(text)
    except Exception:
        try:
            return lhtml.document_fromstring(text)
        except Exception:
            return None


def _looks_json(text):
    t = (text or "").lstrip()[:1]
    return t in ("{", "[")


class SourceChecker:
    def __init__(self, fetcher=None, keywords=None, min_content=200,
                 per_source_timeout=45, log=print):
        self.fetcher = fetcher or Fetcher()
        self.keywords = keywords or ["斗罗大陆", "诡秘之主", "三体", "剑来"]
        self.min_content = min_content
        self.budget = per_source_timeout
        self.log = log

    # ------------------------------------------------------------------ 主流程
    def check(self, src):
        res = CheckResult()
        t_start = time.time()

        try:
            self._run(src, res, t_start)
        except JsRuleError:
            res.ok = False
            res.reason = F_JS
        except Exception as e:
            res.ok = False
            res.reason = F_ERROR
            res.detail = "%s: %s" % (type(e).__name__, str(e)[:120])

        res.delay_ms = int((time.time() - t_start) * 1000)
        if res.delay_ms > self.budget * 1000 and res.ok:
            res.reason = F_TIMEOUT
        return res

    def _run(self, src, res, t_start):
        base = (src.get("bookSourceUrl") or "").strip()
        headers = make_headers(src.get("header"))
        base = base.split("#")[0].strip()
        base = base if re.match(r"^https?://", base, re.I) else ("http://" + base if base else "")
        if not base or not is_valid_host(base):
            res.reason = F_BAD_URL
            res.detail = "书源地址不是有效网址"
            return

        search_url = (src.get("searchUrl") or "").strip()
        if not search_url:
            res.reason = F_NO_SEARCH
            return

        resolver = RuleResolver()
        rs = src.get("ruleSearch") or {}
        if not isinstance(rs, dict):
            rs = {}

        # 关键词：书源自带的 checkKeyWord 优先
        kw = (rs.get("checkKeyWord") or "").strip()
        keyword = kw.split(",")[0].strip() if kw else self.keywords[0]

        # 预热：先访问一次首页拿 cookie，模拟阅读器行为（很多站不带 cookie 不返回搜索结果）
        try:
            self.fetcher.request(base + "/", headers=headers)
        except Exception:
            pass

        # 书源写了 {{cookie.removeCookie(source.key)}} 的，要在搜索前清 cookie：
        # 这类站带上网页 cookie 去搜索会直接返回空页/热门页（阅读器本身就会清）。
        if "removeCookie" in search_url:
            self.fetcher.clear_cookies()

        # ---------------- 1. 搜索 ----------------
        t0 = time.time()
        tpl, opts = parse_search_url(search_url)
        method = (opts.get("method") or "GET").upper()
        body_tpl = opts.get("body") or ""

        # 关键词编码：GBK 系站点必须用 gb18030 编码，否则搜不到
        cs = (opts.get("charset") or "").lower()
        if cs in ("gbk", "gb2312", "gb18030"):
            key_q = quote(keyword, encoding="gb18030")
        else:
            key_q = quote(keyword)
        mapping = {"key": key_q, "keyword": key_q, "page": 1,
                   "baseUrl": base, "baseurl": base, "host": urlparse(base).netloc}
        url = join_url(base, fill_template(tpl, mapping))
        if not url:
            res.reason = F_NO_SEARCH
            return

        if opts.get("charset"):
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        body = fill_template(body_tpl, mapping) if method == "POST" else None
        if method == "POST" and body and opts.get("charset"):
            # body 里可能带 {} 占位，直接按原样发
            pass

        r = self.fetcher.request(url, method=method, body=body, headers=headers,
                                 referer=base + "/")
        res.t_search = int((time.time() - t0) * 1000)
        if not r.ok:
            if r.error == "dns":
                res.reason = F_DNS
                res.detail = "本机 DNS 解析不了该域名（换网络/用 Actions 跑通常就好了）"
            else:
                res.reason = F_SEARCH_NET
                res.detail = r.error
            return
        if r.status >= 400:
            res.reason = F_SEARCH_NET
            res.detail = "HTTP %d" % r.status
            return

        page = self._doc_of(r.text, url)
        if page is None:
            res.reason = F_SEARCH_PARSE
            res.detail = "响应非 HTML/JSON"
            return

        s_base = r.url or url
        book_list_rule = (rs.get("bookList") or "").strip()
        try:
            if book_list_rule:
                nodes = self._select(page, book_list_rule, s_base)
            else:
                nodes = [page]
        except JsRuleError:
            res.reason = F_JS
            return
        if not nodes and len(self.keywords) > 1:
            # 换个关键词再试一次（有些站对特定词无结果，不代表源失效）
            for alt in self.keywords[1:2]:
                if alt == keyword:
                    continue
                akq = quote(alt, encoding="gb18030") if cs in ("gbk", "gb2312", "gb18030") else quote(alt)
                amap = {"key": akq, "keyword": akq, "page": 1, "baseUrl": base,
                        "baseurl": base, "host": urlparse(base).netloc}
                aurl = join_url(base, fill_template(tpl, amap))
                abody = fill_template(body_tpl, amap) if method == "POST" else None
                ar = self.fetcher.request(aurl, method=method, body=abody, headers=headers,
                                          referer=base + "/")
                if not ar.ok or ar.status >= 400:
                    continue
                apage = self._doc_of(ar.text, aurl)
                if apage is None:
                    continue
                anode = self._select(apage, book_list_rule, ar.url or aurl) if book_list_rule else [apage]
                if anode:
                    nodes = anode
                    keyword = alt
                    s_base = ar.url or aurl
                    page = apage
                    break

        if not nodes:
            # 有些站对短时间内的重复搜索直接返回空页/热门页（限流）。
            # 隔一下再打一次，避免把活着但被限流的源误判成失效。
            time.sleep(1.0)
            self.fetcher.clear_cookies()
            r2 = self.fetcher.request(url, method=method, body=body, headers=headers,
                                      referer=base + "/")
            if r2.ok and r2.status < 400:
                p2 = self._doc_of(r2.text, url)
                if p2 is not None:
                    try:
                        n2 = self._select(p2, book_list_rule, r2.url or url) \
                            if book_list_rule else [p2]
                    except JsRuleError:
                        n2 = []
                    if n2:
                        nodes = n2
                        page = p2
                        s_base = r2.url or url

        if not nodes:
            res.reason = F_SEARCH_EMPTY
            return

        name_rule = (rs.get("name") or "").strip()
        url_rule = (rs.get("bookUrl") or "").strip() or "@href"

        picked = None
        for node in nodes[:10]:
            try:
                bname = self._val(node, name_rule, s_base) if name_rule else ""
                burl = self._val(node, url_rule, s_base) if url_rule else ""
            except JsRuleError:
                res.reason = F_JS
                return
            burl = (burl or "").strip()
            if not burl:
                continue
            if not burl.lower().startswith(("http://", "https://", "//", "/")):
                continue
            bname = bname or keyword
            picked = (bname, join_url(s_base, burl))
            break

        if not picked:
            res.reason = F_SEARCH_PARSE
            res.detail = "搜到 %d 个条目但取不到书名/链接" % len(nodes)
            return
        res.book = picked[0][:40]
        res.book_url = picked[1]
        res.search_url = s_base

        # ---------------- 2. 详情 → 目录 ----------------
        t1 = time.time()
        book_url = picked[1]
        bi = src.get("ruleBookInfo") or {}
        toc_rule = (bi.get("tocUrl") or "").strip() if isinstance(bi, dict) else ""

        detail_res = self.fetcher.request(book_url, headers=headers, referer=s_base)
        if not detail_res.ok or detail_res.status >= 400:
            res.reason = F_TOC_NET
            res.detail = detail_res.error or ("HTTP %d" % detail_res.status)
            res.t_toc = int((time.time() - t1) * 1000)
            return

        detail_doc = self._doc_of(detail_res.text, book_url)
        if detail_doc is None:
            res.reason = F_TOC_EMPTY
            res.t_toc = int((time.time() - t1) * 1000)
            return

        d_base = detail_res.url or book_url
        toc_url = ""
        if toc_rule:
            try:
                toc_url = self._val(detail_doc, toc_rule, d_base)
            except JsRuleError:
                toc_url = ""
            toc_url = join_url(d_base, toc_url) if toc_url else ""

        toc_doc = detail_doc
        toc_base = d_base
        if toc_url and _strip(toc_url) != _strip(d_base):
            tr = self.fetcher.request(toc_url, headers=headers, referer=d_base)
            if not tr.ok or tr.status >= 400:
                res.reason = F_TOC_NET
                res.detail = "目录页 " + (tr.error or ("HTTP %d" % tr.status))
                res.t_toc = int((time.time() - t1) * 1000)
                return
            d2 = self._doc_of(tr.text, toc_url)
            if d2 is not None:
                toc_doc = d2
                toc_base = tr.url or toc_url

        rt = src.get("ruleToc") or {}
        cl_rule = (rt.get("chapterList") or "").strip() if isinstance(rt, dict) else ""
        if not cl_rule:
            res.reason = F_TOC_EMPTY
            res.detail = "没有目录规则"
            res.t_toc = int((time.time() - t1) * 1000)
            return

        try:
            ch_nodes = self._select(toc_doc, cl_rule, toc_base)
        except JsRuleError:
            res.reason = F_JS
            res.t_toc = int((time.time() - t1) * 1000)
            return
        if not ch_nodes:
            res.reason = F_TOC_EMPTY
            res.detail = "目录规则没匹配到章节"
            res.t_toc = int((time.time() - t1) * 1000)
            return
        res.chapters = len(ch_nodes)

        ch_name_rule = (rt.get("chapterName") or "").strip() or "text"
        ch_url_rule = (rt.get("chapterUrl") or "").strip() or "@href"

        chapters = []
        for node in ch_nodes[:400]:
            try:
                cu = self._val(node, ch_url_rule, toc_base)
                cn = self._val(node, ch_name_rule, toc_base)
            except JsRuleError:
                res.reason = F_JS
                res.t_toc = int((time.time() - t1) * 1000)
                return
            if cu:
                chapters.append((cn, join_url(toc_base, cu)))
        res.t_toc = int((time.time() - t1) * 1000)

        if not chapters:
            res.reason = F_TOC_EMPTY
            res.detail = "章节链接取不到"
            return
        res.first_chapter = chapters[min(2, len(chapters) - 1)][1]

        # ---------------- 3. 正文 ----------------
        t2 = time.time()
        rc = src.get("ruleContent") or {}
        content_rule = (rc.get("content") or "").strip() if isinstance(rc, dict) else ""
        if not content_rule:
            res.reason = F_CONTENT_EMPTY
            res.detail = "没有正文规则"
            res.t_content = int((time.time() - t2) * 1000)
            return

        # 取靠前但不一定是第一章的章节（第一章常是公告/求票）
        idxs = [2, 0, 1, 3, 5]
        best = 0
        tried = 0
        for i in idxs:
            if i >= len(chapters):
                continue
            tried += 1
            if tried > 3:
                break
            cn, curl = chapters[i]
            cr = self.fetcher.request(curl, headers=headers, referer=toc_base)
            if not cr.ok or cr.status >= 400:
                continue
            cdoc = self._doc_of(cr.text, curl)
            if cdoc is None:
                continue
            try:
                text = self._val(cdoc, content_rule, cr.url or curl)
            except JsRuleError:
                res.reason = F_JS
                res.t_content = int((time.time() - t2) * 1000)
                return
            text = _clean_content(text)
            if len(text) > best:
                best = len(text)
            if best >= self.min_content:
                break

        res.t_content = int((time.time() - t2) * 1000)
        res.content_len = best
        if best == 0:
            res.reason = F_CONTENT_EMPTY
            return
        if best < self.min_content:
            res.reason = F_CONTENT_SHORT
            res.detail = "%d 字 < %d" % (best, self.min_content)
            return

        res.ok = True
        res.reason = OK

    # ---------------------------------------------------------------- 工具方法
    @staticmethod
    def _doc_of(text, url):
        """HTML 或 JSON 都包一层统一入口。"""
        if text is None:
            return None
        t = text.lstrip()
        if not t:
            return None
        if t[0] in "{[":
            try:
                return json.loads(t)
            except Exception:
                pass
        return _parse_html(text)

    def _select(self, root, rule, base=""):
        if isinstance(root, (dict, list)):
            # JSON 源：bookList 用 jsonpath，保留原始对象（后面还要取字段）
            return self._json_select(root, rule)
        return self._select_nodes(root, rule, base)

    def _json_select(self, root, rule):
        """JSON 源里按 $.a.b[*] 取出对象列表。"""
        rule = (rule or "").strip()
        if not rule:
            return []
        for branch in RuleResolver._split_top(rule, "||"):
            b = branch.strip()
            if not b:
                continue
            if b.startswith("@json:"):
                b = b[6:].strip()
            if b.startswith("$"):
                got = _jsonpath_nodes(root, b)
                if got:
                    return got
            else:
                # 规则不是 jsonpath：当作 key
                got = _jsonpath_nodes(root, "$." + b)
                if got:
                    return got
        return []

    def _select_nodes(self, root, rule, base=""):
        """HTML 上返回节点列表（用于 bookList / chapterList），支持 || 分支。"""
        rule = (rule or "").strip()
        if not rule:
            return []
        if "<js>" in rule or rule.lower().startswith("@js:"):
            raise JsRuleError("js")
        if "||" in rule:
            for branch in RuleResolver._split_top(rule, "||"):
                got = self._select_nodes_branch(root, branch, base)
                if got:
                    return got
            return []
        return self._select_nodes_branch(root, rule, base)

    def _select_nodes_branch(self, root, rule, base=""):
        r = RuleResolver()
        rule = (rule or "").strip()
        if not rule:
            return []
        if "<js>" in rule or rule.lower().startswith("@js:"):
            raise JsRuleError("js")
        core, regex, repl = r._split_regex(rule)
        if regex:
            core = core  # 清洗对节点列表无意义，跳过
        cur = [root]
        segs = r._split_segments(core)
        for seg in segs:
            nxt = []
            for v in cur:
                if isinstance(v, str):
                    continue
                if not hasattr(v, "xpath"):
                    continue
                # 取值段说明到达了终点
                if seg.startswith("@"):
                    w = seg[1:].strip()
                    lw = w.lower()
                    if lw.startswith(("class.", "tag.", "id.", "text.", "children", "//", ".//", "[")):
                        seg = w  # @ 是分隔符，继续当选择器用
                    else:
                        if lw in ("text", "html", "owntext"):
                            nxt.append(v)
                        continue
                if "!" in seg and re.search(r"!\d+$", seg):
                    base_seg, ex = seg.rsplit("!", 1)
                    got = r._apply_segment([v], base_seg, base)
                    try:
                        ex_i = int(ex)
                        if -len(got) <= ex_i < len(got):
                            got.pop(ex_i)
                    except Exception:
                        pass
                    nxt.extend(got)
                else:
                    nxt.extend(r._apply_segment([v], seg, base))
            cur = nxt
        return [x for x in cur if not isinstance(x, str)]

    def _val(self, node, rule, base=""):
        if isinstance(node, dict) and rule and "{{" in rule:
            def _repl(m):
                p = m.group(1).strip()
                got = _jsonpath_nodes(node, p if p.startswith("$") else "$." + p)
                return str(got[0]) if got else ""
            rule = re.sub(r"\{\{([^}]+)\}\}", _repl, rule)
            # 替换完是纯地址（{{$._id}} 这种拼接写法），直接当结果
            if re.match(r"^https?://", rule.strip()):
                return rule.strip()
        r = RuleResolver()
        return r.eval_one(node, rule, base)


def _clean_content(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text) if "<" in text and ">" in text else text
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip(u):
    return (u or "").strip().rstrip("/").lower()


def _jsonpath_nodes(root, expr):
    """JSONPath 简版，返回原始对象列表（bookList 用）。"""
    from .rules import jsonpath_objects
    return jsonpath_objects(root, expr)
