#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
失败书源自动重写器

思路：站点还活着、只是规则过时/写错的书源，靠启发式重新生成规则救回来。

  · 地址修复      无效地址从 searchUrl / exploreUrl / bookUrlPattern 里提取真实域名
  · 搜索重建      抓首页找搜索表单，或试常见搜索路径，确认能出结果再落地 searchUrl + ruleSearch
  · 目录重建      在书籍页找章节链接，定位共同容器，生成 chapterList / chapterName / chapterUrl
  · 正文重建      在章节页找文本量最大的正文容器，生成 ruleContent
  · 同域扩散      同域名只要有 1 条修通，其余条目套用它的规则再各自验证

每条修完都交给检测流水线（搜索→目录→正文）真跑一遍，通了才算修好。
"""

import copy
import json
import re
import time
from urllib.parse import quote, urljoin, urlparse

from lxml import html as lhtml

from .checker import _clean_content, _parse_html
from .fetcher import (Fetcher, fill_template, is_valid_host, join_url, make_headers,
                      parse_search_url)

# 章节标题特征：第12章 / 12. / 正文 / 最新章节
CHAPTER_RE = re.compile(
    r"(第\s*[0-9零一二三四五六七八九十百千万两〇]+\s*[章节回卷话集篇]"
    r"|^\s*\d{1,5}\s*[\.、:：\s]"
    r"|章节|正文|最新章节)")

# 明显不是搜索结果/章节的链接路径
JUNK_PATH_RE = re.compile(
    r"(^/?(search|so|login|register|reg|signup|user|member|ucenter|about|help|contact"
    r"|privacy|terms|feedback|bookcase|shelf|rank|top|sort|category|download|app|setting)"
    r"|\.(css|js|jpg|jpeg|png|gif|webp|ico|svg|woff2?|ttf)$)", re.I)

SEARCH_FIELD_HINTS = ("keyword", "searchkey", "searchword", "searchtext", "kw",
                      "wd", "query", "word", "key", "q", "s", "name")

# 常见搜索路径模板（中文站基本跑不出这几类）
SEARCH_TEMPLATES = [
    "/search.php?keyword={{key}}",
    "/search.php?searchkey={{key}}",
    "/modules/article/search.php?searchkey={{key}}",
    "/search?q={{key}}",
    "/search?keyword={{key}}",
    "/s?q={{key}}",
    "/index.php?keyword={{key}}",
    "/search/{{key}}",
    "/?s={{key}}",
    "/so.php?q={{key}}",
]

UA_KEYWORD = "斗罗大陆"


def log_none(*a):
    pass


# ---------------------------------------------------------------- URL 小工具
def abs_url(href, base):
    """补全链接；javascript/空链接返回空串。"""
    if not href:
        return ""
    h = str(href).strip()
    if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    if h.startswith("//"):
        return "https:" + h
    if h.lower().startswith(("http://", "https://")):
        return h
    try:
        return urljoin(base, h)
    except Exception:
        return ""


def clean_url(u):
    """地址清洗：切掉域名后面粘的垃圾字符、去片段、补协议、中文域名转 punycode。"""
    u = (u or "").strip().strip("\"'")
    u = u.split("#")[0].strip()
    u = re.sub(r"\s+", "", u)
    if not u:
        return ""
    if u.startswith("//"):
        u = "http:" + u
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u.lstrip("/")

    # 域名后面常粘着「乃星」「-3」「##1」这类垃圾，按合法域名截断
    m = re.match(r"^(https?)://([A-Za-z0-9.\-]+\.[A-Za-z]{2,10})(?![A-Za-z0-9])", u, re.I)
    if not m:
        return ""
    scheme, host = m.group(1).lower(), m.group(2).lower()
    if "." not in host:
        return ""
    tail = u[m.end():]
    port = ""
    pm = re.match(r":(\d{2,5})", tail)
    if pm:
        port = ":" + pm.group(1)
        tail = tail[pm.end():]
    path = tail.split("#")[0] if tail.startswith("/") else ""
    path = path.split("?")[0] if False else path
    try:
        if any(ord(ch) > 127 for ch in host):
            host = host.encode("idna").decode("ascii")
    except Exception:
        return ""
    if not re.match(r"^[a-z0-9.\-]+$", host):
        return ""
    return "%s://%s%s%s" % (scheme, host, port, path or "/")


def url_from_other_fields(src):
    """书源地址坏掉时，从其它字段里挖出真实域名。"""
    out = []
    fields = ["searchUrl", "exploreUrl", "bookUrlPattern", "loginUrl",
              "bookSourceComment", "bookUrlPattern"]
    blob = []
    for f in fields:
        v = src.get(f)
        if isinstance(v, str):
            blob.append(v)
    for rule_group in ("ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent",
                       "ruleExplore"):
        g = src.get(rule_group)
        if isinstance(g, dict):
            blob.extend(str(v) for v in g.values() if isinstance(v, str))
    for text in blob:
        for m in re.finditer(r"https?://([A-Za-z0-9\.\-]+\.[A-Za-z]{2,})", text):
            host = m.group(1).lower()
            if host in ("www.w3.org", "example.com", "t.me", "github.com",
                        "raw.githubusercontent.com", "google.com"):
                continue
            scheme = "https" if "https://" in text[max(0, m.start() - 1):m.start() + 9] else "http"
            out.append("%s://%s/" % (scheme, host))
    # 去重保序
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


def bad_url_reason(u):
    """地址是不是压根不像网址。"""
    if not u:
        return True
    host = urlparse(u).hostname or ""
    return "." not in host


# ---------------------------------------------------------------- 结构定位
def path_of(el):
    try:
        return el.getroottree().getpath(el)
    except Exception:
        return ""


def unique_rule(doc, el):
    """给某个元素生成「只命中它自己」的规则前缀。"""
    if el is None or not isinstance(el.tag, str):
        return ""
    eid = (el.get("id") or "").strip()
    if re.match(r"^[A-Za-z_][\w\-]*$", eid):
        try:
            if len(doc.xpath("//*[@id=$i]", i=eid)) == 1:
                return "id." + eid
        except Exception:
            pass
    for c in (el.get("class") or "").split():
        if not re.match(r"^[A-Za-z_][\w\-]*$", c) or len(c) < 3:
            continue
        try:
            if len(doc.xpath(
                    '//*[contains(concat(" ",normalize-space(@class)," "),$c)]',
                    c=" %s " % c)) == 1:
                return "class." + c
        except Exception:
            pass
    p = path_of(el)
    return ("xpath:" + p) if p else ""


def lca_of(nodes):
    """最近共同祖先。"""
    nodes = [n for n in nodes if n is not None]
    if not nodes:
        return None
    common = None
    for n in nodes:
        ids = set(id(x) for x in n.iterancestors())
        ids.add(id(n))
        common = ids if common is None else (common & ids)
        if not common:
            return None
    for anc in nodes[0].iterancestors():
        if id(anc) in common:
            return anc
    return None


def _text(a):
    try:
        return re.sub(r"\s+", " ", a.text_content()).strip()
    except Exception:
        return ""


def _host_of(u):
    try:
        h = (urlparse(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _link_ok(a, base, want_chapter=False):
    href = abs_url(a.get("href"), base)
    if not href or not href.startswith("http"):
        return ""
    if _host_of(href) != _host_of(base):
        return ""
    path = urlparse(href).path or "/"
    if path in ("", "/"):
        return ""
    if JUNK_PATH_RE.search(path):
        return ""
    txt = _text(a)
    if want_chapter:
        if not txt:
            return ""
        if len(txt) > 60:
            return ""
    else:
        if len(txt) < 2 or len(txt) > 40:
            return ""
    return href


def _group_by_parent(links):
    """把链接按父节点分组，返回数量最多且 >=min_n 的那组 (parent, [(a,href)])。"""
    groups = {}
    for a, href in links:
        p = a.getparent()
        if p is None or not isinstance(p.tag, str):
            continue
        groups.setdefault(id(p), (p, []))[1].append((a, href))
    if not groups:
        return None
    best = max(groups.values(), key=lambda g: len(g[1]))
    return best



def _url_pattern(u):
    """把链接压成模式：/zhan/8028/637112.html -> /zhan/N/N.html"""
    try:
        p = urlparse(u).path or "/"
    except Exception:
        return ""
    return re.sub(r"\d+", "N", p)


def _best_pattern_group(links, min_n=5):
    """按 URL 模式分组，取最大的一组——章节/搜索结果通常共享同一 URL 模式。"""
    groups = {}
    for a, h in links:
        groups.setdefault(_url_pattern(h), []).append((a, h))
    if not groups:
        return None
    pattern, items = max(groups.items(), key=lambda kv: len(kv[1]))
    if len(items) < min_n:
        return None
    return pattern, items


def rule_for_list(doc, items):
    """给一组同构条目生成 (列表规则, 名称规则, 链接规则)。

    优先级：唯一 id / 唯一 class 的容器 → 全局标签（页面里数量对得上就用）→ 绝对路径。
    绝对路径最容易因页面抖动失效，所以尽量不用。
    """
    anchors = [a for a, _ in items]
    if not anchors:
        return None
    anc = lca_of(anchors)
    if anc is None or not isinstance(anc.tag, str) or anc.tag in ("html", "body"):
        return None

    # 条目的父节点标签（dd / li / div / a …）
    from collections import Counter
    pts = Counter(a.getparent().tag for a in anchors
                  if a.getparent() is not None and isinstance(a.getparent().tag, str))
    ptag = pts.most_common(1)[0][0] if pts else "a"
    direct_a = all(a.getparent() is anc for a in anchors)

    cont = unique_rule(doc, anc)
    if cont and not cont.startswith("xpath:/"):
        if direct_a:
            return cont + "@tag.a", "@text", "@href"
        if ptag == "a":
            return cont + "@tag.a", "@text", "@href"
        return cont + "@tag." + ptag, "tag.a@text", "tag.a@href"

    # 容器没有唯一 id/class：看整页里这个标签是不是基本就是这批条目
    if not direct_a and ptag != "a":
        try:
            n_all = len(doc.xpath("//*[local-name()=$t]", t=ptag.lower()))
        except Exception:
            n_all = 0
        if n_all and n_all <= max(len(anchors) * 2, 12):
            return "xpath://%s" % ptag.lower(), "tag.a@text", "tag.a@href"

    # 最后才用绝对路径
    if cont:
        if direct_a or ptag == "a":
            return cont + "@tag.a", "@text", "@href"
        return cont + "@tag." + ptag, "tag.a@text", "tag.a@href"
    return None



def build_search_request(src, keyword, headers_base=None):
    """按书源的 searchUrl 构造一次真实搜索请求参数。"""
    su = (src.get("searchUrl") or "").strip()
    if not su or "<js>" in su or su.lower().startswith("@js:") or su.startswith("data:"):
        return None
    tpl, opts = parse_search_url(su)
    base = clean_url(src.get("bookSourceUrl")) or ""
    if not base:
        return None
    cs = (opts.get("charset") or "").lower()
    key_q = quote(keyword, encoding="gb18030") if cs in ("gbk", "gb2312", "gb18030") else quote(keyword)
    mapping = {"key": key_q, "keyword": key_q, "page": 1, "baseUrl": base,
               "baseurl": base, "host": (urlparse(base).hostname or "")}
    url = join_url(base, fill_template(tpl, mapping))
    if not url:
        return None
    method = (opts.get("method") or "GET").upper()
    body = fill_template(opts.get("body") or "", mapping) if method == "POST" else None
    return {"url": url, "method": method, "body": body, "base": base}


def find_result_rule(doc, anchors, n_expected=0):
    """列表页通用定位：找这批链接共同涉及的、带 class 的容器。

    站点改版常换样式名，但「一堆同类条目挂在同一个 class 容器里」这个结构很稳。
    """
    from collections import Counter
    n = n_expected or len(anchors)
    votes, holder = Counter(), {}
    for a in anchors:
        cur = a
        for _depth in range(1, 7):
            cur = cur.getparent()
            if cur is None or not isinstance(cur.tag, str):
                break
            cls = (cur.get("class") or "").strip()
            eid = (cur.get("id") or "").strip()
            key = (cur.tag, cls, eid)
            votes[key] += 1
            holder.setdefault(key, cur)

    min_hits = max(3, int(n * 0.6))
    best = None
    for key, c in votes.items():
        tag, cls, eid = key
        el = holder[key]
        if c < min_hits:
            continue
        # id 唯一：直接可用
        if eid and re.match(r"^[A-Za-z_][\w\-]*$", eid):
            try:
                if len(doc.xpath("//*[@id=$i]", i=eid)) == 1:
                    score = c + 20
                    if best is None or score > best[0]:
                        best = (score, "id." + eid + "@tag.a")
            except Exception:
                pass
            continue
        first = next((x for x in cls.split()
                      if re.match(r"^[A-Za-z_][\w\-]*$", x) and len(x) >= 3), "")
        if not first:
            continue
        try:
            total = len(doc.xpath(
                '//*[contains(concat(" ",normalize-space(@class)," "),$c)]',
                c=" %s " % first))
        except Exception:
            continue
        if total < 1 or total > max(n * 3, 25):
            continue
        score = c + (10 if total <= n * 1.6 else 0) + (6 if el.tag in ("ul", "ol", "div", "section", "table") else 0)
        if best is None or score > best[0]:
            best = (score, "class." + first + "@tag.a")

    if best:
        return best[1], "@text", "@href"
    return None


# ---------------------------------------------------------------- 重建规则
class Fixer(object):
    def __init__(self, fetcher=None, timeout=12, min_content=200,
                 budget=75, keywords=None, log=None):
        self.fetcher = fetcher or Fetcher(timeout=timeout)
        self.timeout = timeout
        self.min_content = min_content
        self.budget = budget
        self.keywords = keywords or [UA_KEYWORD, "诡秘之主"]
        self.log = log or log_none

    # ------------------------------------------------------------ 搜索探测
    def _fetch_doc(self, url, headers, referer=""):
        r = self.fetcher.request(url, headers=headers, referer=referer)
        if not r.ok or r.status >= 400:
            return None, r
        return _parse_html(r.text), r

    def _search_candidates(self, doc, base):
        """从首页表单 + 常见模板里生成候选搜索地址模板。"""
        cands = []
        if doc is not None:
            for form in doc.xpath("//form"):
                method = (form.get("method") or "get").strip().upper()
                if method != "GET":
                    continue
                action = abs_url(form.get("action") or "", base) or (base + "/")
                field = None
                for inp in form.xpath(".//input"):
                    nm = (inp.get("name") or "").strip()
                    if not nm:
                        continue
                    low = nm.lower()
                    if any(h == low or h in low for h in SEARCH_FIELD_HINTS):
                        field = nm
                        break
                if not field:
                    for inp in form.xpath(".//input"):
                        typ = (inp.get("type") or "text").lower()
                        nm = (inp.get("name") or "").strip()
                        if typ in ("text", "search") and nm:
                            field = nm
                            break
                if not field:
                    continue
                hidden = []
                for inp in form.xpath(".//input"):
                    typ = (inp.get("type") or "").lower()
                    nm = (inp.get("name") or "").strip()
                    if typ == "hidden" and nm and nm != field:
                        hidden.append("%s=%s" % (quote(nm, safe=""),
                                                 quote(inp.get("value") or "", safe="")))
                q = "&".join(hidden + ["%s={{key}}" % quote(field, safe="")])
                sep = "&" if "?" in action else "?"
                cands.append(action + sep + q)

        root = base.rstrip("/")
        for tpl in SEARCH_TEMPLATES:
            cands.append(root + tpl)

        # 去重
        seen, out = set(), []
        for c in cands:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out[:5]

    def _score_search_page(self, doc, base, keyword, want_url):
        """判断这页是不是真的搜索结果页，返回 (score, items)。"""
        if doc is None:
            return 0, None
        links = []
        for a in doc.xpath("//a[@href]"):
            href = _link_ok(a, base)
            if href:
                links.append((a, href))
        if len(links) < 3:
            return 0, None

        # 优先：书名里就带关键词的那些链接
        hits = [(a, h) for a, h in links if keyword and keyword in _text(a)]
        pool = hits if len(hits) >= 2 else links
        grp = _best_pattern_group(pool, 2)
        if not grp:
            return 0, None
        pattern, items = grp
        names = [_text(a) for a, _ in items]
        good = [n for n in names if 2 <= len(n) <= 40]
        if len(items) < 2 or not good:
            return 0, None
        score = len(items) + len(good)
        body = doc.text_content() or ""
        if keyword and keyword in body:
            score += 12
        if hits:
            score += 8
        if want_url and want_url.rstrip("/") != (base or "").rstrip("/"):
            score += 2
        if re.search(r"(搜索|search|结果|找到)", body[:4000], re.I):
            score += 3
        return score, items

    def probe_search(self, base, headers, keyword):
        """找到能出结果的搜索地址，并生成 ruleSearch。"""
        doc, _ = self._fetch_doc(base.rstrip("/") + "/", headers)
        best = None
        for tpl in self._search_candidates(doc, base):
            url = join_url(base, fill_template(tpl, {"key": quote(keyword), "page": 1}))
            if not url:
                continue
            pdoc, r = self._fetch_doc(url, headers, referer=base.rstrip("/") + "/")
            if pdoc is None:
                continue
            score, grp = self._score_search_page(pdoc, r.url or url, keyword, base)
            if score and (best is None or score > best[0]):
                best = (score, tpl, pdoc, r.url or url, grp)
            if best and best[0] >= 25:
                break
        if not best:
            return None
        score, tpl, pdoc, s_base, items = best
        anchors = [a for a, _ in items]
        built = find_result_rule(pdoc, anchors, len(anchors)) or rule_for_list(pdoc, items)
        if not built:
            return None
        book_list, name_rule, url_rule = built

        return {
            "searchUrl": tpl,
            "bookList": book_list,
            "name": name_rule,
            "bookUrl": url_rule,
            "first_book": items[0][1],
            "first_name": _text(items[0][0]) or keyword,
            "base": s_base,
            "items": len(items),
            "score": score,
        }

    # ------------------------------------------------------------ 目录探测
    def probe_toc(self, book_url, headers, referer=""):
        """在书籍页找章节列表，必要时跟进一次「开始阅读 / 目录」链接。"""
        doc, r = self._fetch_doc(book_url, headers, referer)
        if doc is None:
            return None
        page_url = r.url or book_url
        got = self._toc_from_doc(doc, page_url)
        if got:
            got["toc_page"] = ""
            return got

        # 跟进「开始阅读 / 目录」入口
        for a in doc.xpath("//a[@href]"):
            t = _text(a)
            if not t or len(t) > 12:
                continue
            if not re.search(r"(开始阅读|立即阅读|目录|章节列表|全部章节|查看目录|点击阅读)", t):
                continue
            href = abs_url(a.get("href"), page_url)
            if not href or _host_of(href) != _host_of(page_url):
                continue
            d2, r2 = self._fetch_doc(href, headers, referer=page_url)
            if d2 is None:
                continue
            got = self._toc_from_doc(d2, r2.url or href)
            if got:
                # toggleUrl 用 xpath 指到那个入口
                got["toc_page"] = "xpath:%s/@href" % path_of(a)
                return got
        return None

    def _toc_from_doc(self, doc, base):
        links = []
        for a in doc.xpath("//a[@href]"):
            href = _link_ok(a, base, want_chapter=True)
            if href:
                links.append((a, href))
        if len(links) < 5:
            return None

        # 先挑带「第 N 章 / 正文」这类标题的链接，再按 URL 模式锁定同一批章节
        chapterish = [(a, h) for a, h in links if CHAPTER_RE.search(_text(a))]
        grp = _best_pattern_group(chapterish, 5) if len(chapterish) >= 5 else None
        if not grp:
            grp = _best_pattern_group(links, 5)
        if not grp:
            return None
        pattern, items = grp
        built = rule_for_list(doc, items)
        if not built:
            return None
        rule, cn, cu = built
        return {
            "chapterList": rule,
            "chapterName": cn,
            "chapterUrl": cu,
            "chapters": len(items),
            "first_chapter": items[0][1],
            "first_chapter_name": _text(items[0][0]),
        }

    # ------------------------------------------------------------ 正文探测
    def probe_content(self, chap_url, headers, referer=""):
        doc, r = self._fetch_doc(chap_url, headers, referer)
        if doc is None:
            return None
        best = None
        for el in doc.iter():
            if not isinstance(el.tag, str):
                continue
            if el.tag in ("script", "style", "nav", "header", "footer", "a", "title"):
                continue
            try:
                txt = "".join(el.itertext())
            except Exception:
                continue
            n = len(_clean_content(txt))
            if n < self.min_content:
                continue
            na = len(el.findall(".//a"))
            np_ = len(el.findall(".//p"))
            # 正文：字多、链接少、段落多
            score = n - na * 120 + np_ * 8
            if best is None or score > best[0]:
                best = (score, el, n)
        if not best:
            return None
        _, el, n = best
        rule = unique_rule(doc, el)
        if not rule:
            return None
        return {"content": rule + "@text", "chars": n, "url": r.url or chap_url}

    # ------------------------------------------------------------ 整体重建
    def rebuild(self, src):
        """给一条失败源重建整条链路，返回 (新源, 说明) 或 (None, 原因)。"""
        t0 = time.time()
        fixed = copy.deepcopy(src)
        headers = make_headers(fixed.get("header"))

        # 1) 地址
        base = clean_url(fixed.get("bookSourceUrl"))
        if bad_url_reason(base):
            got = url_from_other_fields(fixed)
            base = got[0] if got else ""
        if not base:
            return None, "地址无法修复"
        fixed["bookSourceUrl"] = base.rstrip("/")

        # 2) 搜索
        kw = UA_KEYWORD
        rs0 = fixed.get("ruleSearch")
        if isinstance(rs0, dict) and (rs0.get("checkKeyWord") or "").strip():
            kw = (rs0.get("checkKeyWord") or "").split(",")[0].strip() or kw
        s = self.probe_search(base, headers, kw)
        if not s:
            return None, "搜索入口探测不到"
        if time.time() - t0 > self.budget:
            return None, "超出时间预算"

        rs = fixed.get("ruleSearch")
        rs = dict(rs) if isinstance(rs, dict) else {}
        rs.update({
            "bookList": s["bookList"],
            "name": s["name"],
            "bookUrl": s["bookUrl"],
            "checkKeyWord": rs.get("checkKeyWord") or kw,
        })
        for k in ("author", "kind", "lastChapter", "intro", "coverUrl", "wordCount"):
            v = rs.get(k)
            if isinstance(v, str) and ("<js>" in v or v.lower().startswith("@js:")):
                rs.pop(k, None)
        fixed["ruleSearch"] = rs
        fixed["searchUrl"] = s["searchUrl"]

        notes = ["搜索规则重建"]

        # 3) 目录
        toc = self.probe_toc(s["first_book"], headers, s["base"])
        if not toc:
            return None, "目录探测不到"
        if time.time() - t0 > self.budget:
            return None, "超出时间预算"

        rt = fixed.get("ruleToc")
        rt = dict(rt) if isinstance(rt, dict) else {}
        rt.update({
            "chapterList": toc["chapterList"],
            "chapterName": toc["chapterName"],
            "chapterUrl": toc["chapterUrl"],
        })
        rt.pop("isVip", None)
        rt.pop("updateTime", None)
        fixed["ruleToc"] = rt

        bi = fixed.get("ruleBookInfo")
        bi = dict(bi) if isinstance(bi, dict) else {}
        if toc.get("toc_page"):
            bi["tocUrl"] = toc["toc_page"]
        else:
            bi["tocUrl"] = ""
        fixed["ruleBookInfo"] = bi
        notes.append("目录规则重建")

        # 4) 正文
        content = self.probe_content(toc["first_chapter"], headers, s["base"])
        if not content:
            return None, "正文探测不到"
        rc = fixed.get("ruleContent")
        rc = dict(rc) if isinstance(rc, dict) else {}
        rc["content"] = content["content"]
        fixed["ruleContent"] = rc
        notes.append("正文规则重建")

        fixed["zakaFixed"] = "、".join(notes)
        fixed["zakaFixedAt"] = int(time.time() * 1000)
        return fixed, "、".join(notes)


    def find_working_keyword(self, src, checker, headers):
        """用原搜索地址试几个热门书名，找出这个站真正能搜到书的词。"""
        rs = src.get("ruleSearch")
        rule = (rs.get("bookList") or "").strip() if isinstance(rs, dict) else ""
        tried = 0
        for kw in ["斗罗大陆", "诡秘之主", "三体", "剑来", "凡人修仙传",
                   "完美世界", "遮天", "武动乾坤"]:
            req = build_search_request(src, kw)
            if not req:
                return ""
            tried += 1
            if tried > 5:
                break
            r = self.fetcher.request(req["url"], method=req["method"], body=req["body"],
                                     headers=headers, referer=req["base"] + "/")
            if not r.ok or r.status >= 400:
                continue
            doc = _parse_html(r.text)
            if doc is None:
                continue
            nodes = []
            try:
                if rule:
                    nodes = checker._select(doc, rule, r.url or req["url"])
                else:
                    score, items = self._score_search_page(doc, r.url or req["url"], kw,
                                                           req["base"])
                    nodes = [a for a, _ in (items or [])]
            except Exception:
                nodes = []
            if nodes:
                return kw
        return ""

    # ------------------------------------------------------------ 增量修补
    @staticmethod
    def _js_fields(src):
        """找出规则里带 JS 的字段。"""
        out = []
        for grp in ("ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent",
                    "ruleExplore"):
            g = src.get(grp)
            if not isinstance(g, dict):
                continue
            for k, v in g.items():
                if isinstance(v, str) and ("<js>" in v or v.lower().startswith("@js:")):
                    out.append((grp, k))
        return out

    def _apply_toc(self, fixed, toc):
        rt = fixed.get("ruleToc")
        rt = dict(rt) if isinstance(rt, dict) else {}
        rt.update({
            "chapterList": toc["chapterList"],
            "chapterName": toc["chapterName"],
            "chapterUrl": toc["chapterUrl"],
        })
        for k in ("isVip", "updateTime", "nextTocUrl"):
            rt.pop(k, None)
        fixed["ruleToc"] = rt
        bi = fixed.get("ruleBookInfo")
        bi = dict(bi) if isinstance(bi, dict) else {}
        bi["tocUrl"] = toc.get("toc_page") or ""
        fixed["ruleBookInfo"] = bi

    def _apply_search(self, fixed, s):
        rs = fixed.get("ruleSearch")
        rs = dict(rs) if isinstance(rs, dict) else {}
        rs.update({"bookList": s["bookList"], "name": s["name"],
                   "bookUrl": s["bookUrl"]})
        for k in ("author", "kind", "lastChapter", "intro", "coverUrl",
                  "wordCount", "checkKeyWord"):
            pass
        fixed["ruleSearch"] = rs
        if s.get("searchUrl"):
            fixed["searchUrl"] = s["searchUrl"]

    def fix(self, src, checker, res0=None):
        """按检测卡点精准修补，每步改完都真检一遍。

        返回 (新源 or None, 说明, 最后一次 CheckResult)
        """
        t0 = time.time()
        fixed = copy.deepcopy(src)
        notes = []
        budget = self.budget

        res = res0 if res0 is not None else checker.check(fixed)
        if res.ok:
            return fixed, "原样可用", res

        from .checker import (F_BAD_URL, F_CHAP_NET, F_CONTENT_EMPTY,
                              F_CONTENT_SHORT, F_DNS, F_JS, F_NO_SEARCH,
                              F_SEARCH_EMPTY, F_SEARCH_NET, F_SEARCH_PARSE,
                              F_TOC_EMPTY, F_TOC_NET)

        # ---- 1. 地址本身是坏的
        if res.reason == F_BAD_URL:
            base = clean_url(fixed.get("bookSourceUrl"))
            if bad_url_reason(base):
                got = url_from_other_fields(fixed)
                base = got[0] if got else ""
            if base:
                fixed["bookSourceUrl"] = base.rstrip("/")
                notes.append("地址修复")
                res = checker.check(fixed)
                if res.ok:
                    return self._done(fixed, notes, res)

        # ---- 1.2 换 UA：不少源自带的移动端 UA 被站点拉黑（403）
        if (not res.ok and res.reason in (F_SEARCH_NET, F_TOC_NET, F_CHAP_NET)
                and "403" in (res.detail or "")):
            hdr = fixed.get("header")
            hdr = hdr if isinstance(hdr, str) else (json.dumps(hdr, ensure_ascii=False)
                                                    if isinstance(hdr, dict) else "")
            if hdr:
                try:
                    data = json.loads(hdr) if hdr.strip().startswith("{") else {}
                except Exception:
                    data = {}
                if isinstance(data, dict):
                    data["User-Agent"] = (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
                    fixed["header"] = json.dumps(data, ensure_ascii=False)
                    r_ua = checker.check(fixed)
                    if r_ua.ok:
                        notes.append("更换请求 UA")
                        return self._done(fixed, notes, r_ua)
                    fixed["header"] = hdr
                    res = r_ua if r_ua.reason != res.reason else res

        # ---- 1.5 协议互换：http 站挂了常常是只认 https（或反过来）
        _d = (res.detail or "").lower()
        if (not res.ok and (res.reason == F_DNS or
                            (res.reason in (F_SEARCH_NET, F_TOC_NET, F_CHAP_NET)
                             and any(k in _d for k in ("timeout", "conn", "ssl", "reset"))))):
            cur = fixed.get("bookSourceUrl") or ""
            alt = ""
            if cur.lower().startswith("http://"):
                alt = "https://" + cur[7:]
            elif cur.lower().startswith("https://"):
                alt = "http://" + cur[8:]
            if alt:
                fixed["bookSourceUrl"] = alt.rstrip("/")
                r_alt = checker.check(fixed)
                if r_alt.ok:
                    notes.append("协议互换")
                    return self._done(fixed, notes, r_alt)
                fixed["bookSourceUrl"] = cur

        # ---- 2. 有书籍页链接，说明搜索是通的：先修目录
        if res.book_url and res.reason in (F_TOC_EMPTY, F_TOC_NET, F_CHAP_NET,
                                           F_TOC_NET, F_JS):
            headers = make_headers(fixed.get("header"))
            toc = self.probe_toc(res.book_url, headers, res.search_url)
            if toc:
                self._apply_toc(fixed, toc)
                notes.append("目录规则重建")
                res2 = checker.check(fixed)
                if res2.ok:
                    return self._done(fixed, notes, res2)
                res = res2
            if time.time() - t0 > budget:
                return None, "超出时间预算", res

        # ---- 3. 有章节页链接：修正文
        if res.first_chapter and res.reason in (F_CONTENT_EMPTY, F_CONTENT_SHORT,
                                                F_JS):
            headers = make_headers(fixed.get("header"))
            c = self.probe_content(res.first_chapter, headers, res.search_url)
            if c:
                rc = fixed.get("ruleContent")
                rc = dict(rc) if isinstance(rc, dict) else {}
                rc["content"] = c["content"]
                fixed["ruleContent"] = rc
                notes.append("正文规则重建")
                res2 = checker.check(fixed)
                if res2.ok:
                    return self._done(fixed, notes, res2)
                res = res2
            if time.time() - t0 > budget:
                return None, "超出时间预算", res

        # ---- 3.5 搜索通了但没结果：多半是这个站没这本书，换个热门书名验
        if res.reason == F_SEARCH_EMPTY:
            headers = make_headers(fixed.get("header"))
            kw = self.find_working_keyword(fixed, checker, headers)
            if kw:
                rs = fixed.get("ruleSearch")
                rs = dict(rs) if isinstance(rs, dict) else {}
                rs["checkKeyWord"] = kw
                fixed["ruleSearch"] = rs
                notes.append("改用能搜到的校验词")
                res3 = checker.check(fixed)
                if res3.ok:
                    return self._done(fixed, notes, res3)
                res = res3

        # ---- 4. 搜索这一段的问题
        if res.reason in (F_SEARCH_PARSE, F_SEARCH_EMPTY):
            headers = make_headers(fixed.get("header"))
            s = self.probe_search_keep_url(fixed, headers)
            if s:
                self._apply_search(fixed, s)
                notes.append("搜索解析规则重建")
                res2 = checker.check(fixed)
                if res2.ok:
                    return self._done(fixed, notes, res2)
                res = res2

        if res.reason in (F_SEARCH_NET, F_NO_SEARCH, F_SEARCH_PARSE,
                          F_SEARCH_EMPTY, F_JS) and not res.ok:
            base = clean_url(fixed.get("bookSourceUrl")) or ""
            if base:
                headers = make_headers(fixed.get("header"))
                s = self.probe_search(base, headers, UA_KEYWORD)
                if s:
                    self._apply_search(fixed, s)
                    notes.append("搜索地址重建")
                    res2 = checker.check(fixed)
                    if res2.ok:
                        return self._done(fixed, notes, res2)
                    res = res2

        # ---- 5. 兜底：整条链路重建
        if not res.ok and time.time() - t0 < budget:
            try:
                rebuilt, note = self.rebuild(fixed)
            except Exception:
                rebuilt, note = None, "重建异常"
            if rebuilt:
                res3 = checker.check(rebuilt)
                if res3.ok:
                    notes.append("整链重建")
                    return self._done(rebuilt, notes, res3)
                res = res3

        return None, "重写后仍失败（%s）" % (res.reason or "未知"), res

    def _done(self, fixed, notes, res):
        fixed["zakaFixed"] = "、".join(dict.fromkeys(notes))
        fixed["zakaFixedAt"] = int(time.time() * 1000)
        return fixed, fixed["zakaFixed"], res

    # ------------------------------------------------- 保留原搜索地址，只修解析
    def probe_search_keep_url(self, src, headers):
        """搜索请求能发出去了，只是解析不出条目：用原 searchUrl 抓一页重写解析规则。"""
        from .checker import SourceChecker
        su = (src.get("searchUrl") or "").strip()
        if not su:
            return None
        tpl, opts = parse_search_url(su)
        base = clean_url(src.get("bookSourceUrl")) or ""
        kw = UA_KEYWORD
        rs = src.get("ruleSearch")
        if isinstance(rs, dict):
            ck = (rs.get("checkKeyWord") or "").strip()
            if ck:
                kw = ck.split(",")[0].strip()
        cs = (opts.get("charset") or "").lower()
        key_q = quote(kw, encoding="gb18030") if cs in ("gbk", "gb2312", "gb18030") else quote(kw)
        mapping = {"key": key_q, "keyword": key_q, "page": 1, "baseUrl": base,
                   "baseurl": base, "host": (urlparse(base).hostname or "")}
        url = join_url(base, fill_template(tpl, mapping))
        if not url:
            return None
        method = (opts.get("method") or "GET").upper()
        body = fill_template(opts.get("body") or "", mapping) if method == "POST" else None
        try:
            self.fetcher.request(base + "/", headers=headers)
        except Exception:
            pass
        r = self.fetcher.request(url, method=method, body=body, headers=headers,
                                 referer=base + "/")
        if not r.ok or r.status >= 400:
            return None
        doc = _parse_html(r.text)
        score, items = self._score_search_page(doc, r.url or url, kw, base)
        if not items:
            return None
        anchors = [a for a, _ in items]
        built = find_result_rule(doc, anchors, len(anchors)) or rule_for_list(doc, items)
        if not built:
            return None
        bl, nm, bu = built
        return {"searchUrl": su, "bookList": bl, "name": nm, "bookUrl": bu,
                "first_book": items[0][1], "first_name": _text(items[0][0]),
                "base": r.url or url, "items": len(items), "score": score}
