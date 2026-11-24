#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Legado(阅读 3.0) 书源规则解析引擎 —— 精简实现

支持范围（覆盖市面上绝大多数书源）：
  DefaultRule 语法      class.item@tag.a.0@text
  CSS 属性选择器        [property="og:title"]
  XPath                 //div[@id="list"]/a
  JSONPath              $.data.list[*].name
  正则清洗              ##\\s+##        或   ##(本章完)##
  组合                  A&&B   A||B
  取值                  @text @ownText @html @all @href @src @任意属性

不执行 JS 规则（@js: / <js>），遇到时抛 JsRuleError，由调用方跳过该源。
不执行 @css: / @xpath: / @json: 前缀 —— 这些也一并支持，见 _STRIP_PREFIX。
"""

import json
import re
from urllib.parse import urljoin

from lxml import etree, html as lhtml

try:  # cssselect 让 [property="og:title"] 这类选择器可用
    import cssselect  # noqa: F401
except Exception:  # pragma: no cover
    cssselect = None


class JsRuleError(Exception):
    """规则里含有 JS，本引擎不执行。"""


# 结尾取值关键字（小写）
_VALUE_WORDS = {
    "text", "owntext", "html", "all", "textnodes", "content", "outerhtml", "innerhtml",
}

_NUM_TAIL = re.compile(r"^(.*?)\.(-?\d+)$")
_JS_PAT = re.compile(r"(^@js:|<js>|</js>|^@js\b)", re.I)


def _norm_ws(s):
    """压缩空白但保留换行结构。"""
    if s is None:
        return ""
    s = s.replace("\u00a0", " ").replace("\u3000", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _node_text(el):
    """取节点全部文本（含子节点），保留段落换行。"""
    try:
        parts = [t for t in el.itertext()]
    except Exception:
        return ""
    txt = "".join(parts)
    return _norm_ws(txt)


def _own_text(el):
    parts = []
    if el.text:
        parts.append(el.text)
    for ch in el:
        if ch.tail:
            parts.append(ch.tail)
    return _norm_ws("".join(parts))


def _inner_html(el):
    try:
        s = el.text or ""
        for ch in el:
            s += etree.tostring(ch, encoding="unicode", method="html")
        return s.strip()
    except Exception:
        return ""


def _to_str(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return _node_text(v)


class RuleResolver:
    """把一条 Legado 规则套到 HTML/JSON 上，返回字符串列表。"""

    def __init__(self):
        self.saw_js = False

    # ------------------------------------------------------------------ 入口
    def eval(self, root, rule, base_url=""):
        """
        root: lxml 节点 / dict / list
        rule: 规则字符串
        返回 list[str]（已清洗、已去空）
        """
        if not rule or not isinstance(rule, str):
            return []
        rule = rule.strip()
        if not rule:
            return []

        # 1) ## 正则清洗（最后两段）
        core, regex, repl = self._split_regex(rule)
        if not core:
            # 纯正则：直接对根节点文本做替换
            vals = [_to_str(root)]
            return self._clean(vals, regex, repl)

        # 2) || 取第一个非空
        branches = self._split_top(core, "||")
        for br in branches:
            vals = self._eval_and_group(root, br, base_url)
            vals = self._clean(vals, regex, repl)
            if any(v.strip() for v in vals):
                return [v for v in vals if v.strip()]
        return []

    def eval_one(self, root, rule, base_url=""):
        vals = self.eval(root, rule, base_url)
        for v in vals:
            if v and v.strip():
                return v.strip()
        return ""

    # ------------------------------------------------------------- 表达式拆分
    @staticmethod
    def _split_regex(rule):
        """规则##正则##替换  →  (规则, 正则, 替换)"""
        if "##" not in rule:
            return rule, None, ""
        parts = rule.split("##")
        core = parts[0]
        regex = parts[1] if len(parts) > 1 else None
        repl = parts[2] if len(parts) > 2 else ""
        return core, regex, repl

    @staticmethod
    def _split_top(expr, sep):
        """按分隔符切分，跳过 [] () 内部，避免切坏选择器。"""
        out, buf, depth = [], [], 0
        i = 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch in "[(":
                depth += 1
            elif ch in "])":
                depth = max(0, depth - 1)
            if depth == 0 and expr.startswith(sep, i):
                out.append("".join(buf))
                buf = []
                i += len(sep)
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
        return [x.strip() for x in out if x.strip()]

    def _eval_and_group(self, root, expr, base_url):
        """处理 A&&B：各自求值后合并。"""
        parts = self._split_top(expr, "&&")
        if len(parts) == 1:
            return self._eval_pipeline(root, parts[0], base_url)
        acc = []
        for p in parts:
            acc.extend(self._eval_pipeline(root, p, base_url))
        return acc

    # ------------------------------------------------------------- 单条流水线
    def _eval_pipeline(self, root, expr, base_url):
        expr = expr.strip()
        if not expr:
            return []

        low = expr.lower()

        # @js: / <js>
        if _JS_PAT.search(expr):
            self.saw_js = True
            raise JsRuleError("JS 规则")

        # @css: / @xpath: / @json: 前缀 → 去掉后按类型处理
        forced = None
        for p in ("@css:", "@xpath:", "@json:", "@jsonpath:"):
            if low.startswith(p):
                forced = p.strip("@: ")
                expr = expr[len(p):].strip()
                break

        # 纯 XPath
        if expr.startswith("//") or expr.startswith(".//") or expr.startswith("/("):
            vals = self._xpath(root, expr, base_url)
            return vals

        # 纯 JSONPath
        if expr.startswith("$") and (expr.startswith("$.") or expr.startswith("$[") or expr == "$"):
            vals = self._jsonpath(root, expr)
            return vals

        # 前缀式 JSONPath：json.xxx / data.xxx? 交给 @ 分段处理

        segs = self._split_segments(expr)
        cur = [root]
        for idx, seg in enumerate(segs):
            last = idx == len(segs) - 1
            cur = self._apply_segment(cur, seg, base_url, is_last=last)

        out = []
        for v in cur:
            if isinstance(v, str):
                out.append(v)
            elif v is None:
                continue
            else:
                out.append(_node_text(v))
        return [_norm_ws(x) for x in out if _norm_ws(x)]

    @staticmethod
    def _split_segments(expr):
        """按 @ 分段，但别拆到 XPath/属性选择器里的 @。"""
        segs, buf, depth, i = [], [], 0, 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch in "[(":
                depth += 1
            elif ch in "])":
                depth = max(0, depth - 1)
            if ch == "@" and depth == 0 and i > 0:
                # @ 前面是 :// 说明是 URL 协议，不拆
                prev = expr[max(0, i - 3):i]
                if prev.endswith("://"):
                    buf.append(ch)
                    i += 1
                    continue
                segs.append("".join(buf))
                buf = ["@"]
                i += 1
                continue
            buf.append(ch)
            i += 1
        segs.append("".join(buf))
        return [s.strip() for s in segs if s.strip()]

    # --------------------------------------------------------------- 单段执行
    def _apply_segment(self, cur, seg, base_url, is_last=False):
        seg = seg.strip()
        if not seg:
            return cur

        # 取值：@text / @html / @href ...
        # 但 @tag.h3 / @class.x 这类是"@ 当分隔符"的中间段，仍按选择器处理
        if seg.startswith("@"):
            word = seg[1:].strip()
            lw = word.lower()
            is_selector = (
                lw.startswith(("class.", "tag.", "id.", "text.", "children", "//", ".//", "[", "."))
                or lw in ("parent",)
            )
            if not is_selector:
                return [self._value_of(v, word, base_url) for v in cur]
            seg = word

        # 结尾索引 tag.a.0
        idx = None
        m = _NUM_TAIL.match(seg)
        if m and not re.match(r"^\d+$", seg):
            seg, idx = m.group(1), int(m.group(2))

        out = []
        for v in cur:
            if isinstance(v, (dict, list)):
                # JSON 数据上用点路径
                out.extend(self._json_key(v, seg))
                continue
            if not hasattr(v, "xpath"):
                # 字符串上再选择没意义
                continue
            out.extend(self._select(v, seg))

        if idx is not None:
            if -len(out) <= idx < len(out):
                out = [out[idx]]
            else:
                out = []
        return out

    def _select(self, node, seg):
        s = seg.strip()
        low = s.lower()

        try:
            if low.startswith("//") or low.startswith(".//"):
                return list(node.xpath(s if s.startswith("//") else s))
            if low.startswith("xpath:"):
                return list(node.xpath(s.split(":", 1)[1]))

            if low == "children":
                return [c for c in node if isinstance(c.tag, str)]
            if low in ("parent",):
                p = node.getparent()
                return [p] if p is not None else []

            if low == "text":
                t = _node_text(node)
                return [t] if t else []

            if low.startswith("class."):
                cls = s[6:].strip()
                if not cls:
                    return []
                xp = './/*[contains(concat(" ", normalize-space(@class), " "), $c)]'
                return list(node.xpath(xp, c=" %s " % cls.strip()))
            if low.startswith("id."):
                i = s[3:].strip()
                if not i:
                    return []
                r = node.xpath('.//*[@id=$i]', i=i)
                return list(r)
            if low.startswith("tag."):
                tag = s[4:].strip()
                if not tag:
                    return []
                # tag.div.0 里带了 class 的情况少见，按标签名找
                try:
                    return list(node.xpath(".//*[local-name()=$t]", t=tag.lower()))
                except Exception:
                    return []
            if low.startswith("text."):
                txt = s[5:].strip()
                if not txt:
                    return []
                r = node.xpath('.//*[contains(text(), $t)]', t=txt)
                return list(r)

            if s.startswith("[") or s.startswith("."):
                if cssselect is None:
                    return []
                sel = s if s.startswith(".") else "*" + s
                return list(node.cssselect(sel))

            # 兜底：当作 CSS 选择器
            if cssselect is not None:
                try:
                    return list(node.cssselect(s))
                except Exception:
                    return []
        except Exception:
            return []
        return []

    def _value_of(self, node, word, base_url):
        w = word.strip()
        low = w.lower()
        if isinstance(node, (dict, list, int, float, bool)):
            if low in ("text", "all", "content"):
                return _to_str(node)
            return ""
        if not hasattr(node, "xpath"):
            return _to_str(node)

        try:
            if low in ("text",):
                return _node_text(node)
            if low in ("owntext",):
                return _own_text(node)
            if low in ("html", "innerhtml"):
                return _inner_html(node)
            if low in ("outerhtml",):
                return etree.tostring(node, encoding="unicode", method="html").strip()
            if low in ("all", "textnodes"):
                return _node_text(node)
            if low == "content":
                # meta 标签常见
                c = node.get("content")
                return (c or "").strip()
            # 其余当作属性名
            v = node.get(w)
            if v is None:
                v = node.get(w.lower())
            v = (v or "").strip()
            if v and base_url and w.lower() in ("href", "src", "data-src"):
                v = urljoin(base_url, v)
            return v
        except Exception:
            return ""

    # -------------------------------------------------------------- XPath 整条
    def _xpath(self, root, expr, base_url):
        if not hasattr(root, "xpath"):
            return []
        try:
            res = root.xpath(expr)
        except Exception:
            return []
        out = []
        for r in res:
            if isinstance(r, str):
                out.append(_norm_ws(r))
            elif isinstance(r, (int, float)):
                out.append(str(r))
            else:
                out.append(_node_text(r))
        return [x for x in out if x]

    # ----------------------------------------------------------- JSONPath 简版
    _JSON_TOKEN = re.compile(r"""\.?([A-Za-z0-9_\u4e00-\u9fa5\-]+)|\[(\d+|\*)\]|\['([^']+)'\]|\["([^"]+)"\]""")

    def _jsonpath(self, root, expr):
        if isinstance(root, str):
            try:
                root = json.loads(root)
            except Exception:
                return []
        if not isinstance(root, (dict, list)):
            return []
        if expr in ("$", "$."):
            return [_to_str(root)]

        path = expr[1:]
        if path.startswith("."):
            path = path[1:]

        tokens = []
        pos = 0
        for m in self._JSON_TOKEN.finditer(path):
            if m.start() != pos:
                break
            pos = m.end()
            if m.group(1):
                tokens.append(("key", m.group(1)))
            elif m.group(2):
                tokens.append(("idx", m.group(2)))
            elif m.group(3) is not None:
                tokens.append(("key", m.group(3)))
            elif m.group(4) is not None:
                tokens.append(("key", m.group(4)))

        cur = [root]
        for kind, val in tokens:
            nxt = []
            for item in cur:
                if kind == "key":
                    if isinstance(item, dict) and val in item:
                        nxt.append(item[val])
                    elif isinstance(item, list):
                        for it in item:
                            if isinstance(it, dict) and val in it:
                                nxt.append(it[val])
                else:
                    if isinstance(item, list):
                        if val == "*":
                            nxt.extend(item)
                        else:
                            i = int(val)
                            if -len(item) <= i < len(item):
                                nxt.append(item[i])
            cur = nxt

        out = []
        for v in cur:
            out.append(_to_str(v))
        return [_norm_ws(x) for x in out if _norm_ws(x)]

    def _json_key(self, obj, key):
        """在 dict/list 上取 key（支持 a.b 形式）。"""
        parts = [p for p in key.split(".") if p]
        cur = [obj]
        for p in parts:
            nxt = []
            for item in cur:
                if isinstance(item, dict):
                    if p in item:
                        nxt.append(item[p])
                elif isinstance(item, list):
                    if p.isdigit():
                        i = int(p)
                        if -len(item) <= i < len(item):
                            nxt.append(item[i])
                    else:
                        for it in item:
                            if isinstance(it, dict) and p in it:
                                nxt.append(it[p])
            cur = nxt
        out = []
        for v in cur:
            out.append(_to_str(v))
        return [_norm_ws(x) for x in out if _norm_ws(x)]

    # ------------------------------------------------------------------ 清洗
    @staticmethod
    def _clean(vals, regex, repl):
        if not regex:
            return vals
        try:
            pat = re.compile(regex)
        except re.error:
            return vals
        out = []
        for v in vals:
            try:
                if repl:
                    # Legado 用 $1 引用分组，python 用 \1
                    r = re.sub(r"\$(\d)", r"\\\1", repl)
                    out.append(pat.sub(r, v))
                else:
                    out.append(pat.sub("", v))
            except Exception:
                out.append(v)
        return [_norm_ws(x) for x in out]
