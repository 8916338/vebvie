#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 抓取层：会话复用、编码纠正、超时与重试。"""

import json
import re
import threading
import time
from urllib.parse import quote, urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

_charset_re = re.compile(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)
_META_CHARSET = re.compile(rb'<meta[^>]+charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)


class FetchResult:
    __slots__ = ("ok", "url", "status", "text", "error", "elapsed")

    def __init__(self, ok, url="", status=0, text="", error="", elapsed=0.0):
        self.ok = ok
        self.url = url
        self.status = status
        self.text = text
        self.error = error
        self.elapsed = elapsed

    def __repr__(self):
        return f"<Fetch {self.status} {self.error or 'ok'} {self.elapsed:.2f}s {len(self.text)}b>"


_URL_SAFE = ":/?#[]@!$&'()*+,;=%~"


def _encode_url(url):
    """把 URL 里的中文/空格等编成 %XX，已编码部分不动。"""
    try:
        return quote(url, safe=_URL_SAFE)
    except Exception:
        return url


def _safe_header(v):
    """表头值必须是 latin-1 可编码的；中文/emoji 转成 %XX。"""
    v = str(v)
    try:
        v.encode("latin-1")
        return v
    except UnicodeEncodeError:
        return quote(v, safe="")


def _guess_encoding(resp, raw):
    """按响应头 → meta → apparent_encoding → utf-8 顺序猜。"""
    enc = (resp.encoding or "").lower()
    declared = None
    m = _charset_re.search(resp.headers.get("Content-Type", "").encode("latin-1", "ignore"))
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()

    if not declared:
        m = _META_CHARSET.search(raw[:4096])
        if m:
            declared = m.group(1).decode("ascii", "ignore").lower()

    if declared:
        declared = {"gb2312": "gb18030", "gbk": "gb18030", "gb-2312": "gb18030"}.get(declared, declared)

    # requests 自己从 head 里猜的（requests 不认 <meta>，但认 header）
    cand = declared or enc
    if cand and cand not in ("iso-8859-1", "latin-1", "ascii"):
        return cand

    apparent = resp.apparent_encoding
    if apparent:
        a = apparent.lower()
        return {"gb2312": "gb18030", "gbk": "gb18030"}.get(a, a)
    return "utf-8"


class Fetcher:
    """线程内复用 session。"""

    def __init__(self, timeout=12, retries=1, proxy=None, verify_ssl=True, trust_env=True):
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy
        self.verify_ssl = verify_ssl
        self.trust_env = trust_env
        self._local = threading.local()

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update(DEFAULT_HEADERS)
            s.trust_env = self.trust_env
            if self.proxy:
                s.proxies = {"http": self.proxy, "https": self.proxy}
            self._local.s = s
        return s

    def clear_cookies(self):
        """清掉当前线程 session 的 cookie（书源里的 removeCookie 语义）。"""
        try:
            self._session().cookies.clear()
        except Exception:
            pass

    def request(self, url, method="GET", body=None, headers=None, referer=None,
                encoding=None, allow_redirect=True):
        url = url.strip()
        if url.startswith("//"):
            url = "http:" + url
        if not url.lower().startswith(("http://", "https://")):
            url = "http://" + url
        url = _encode_url(url)

        hdrs = {}
        for k, v in (headers or {}).items():
            try:
                hdrs[str(k)] = _safe_header(v)
            except Exception:
                continue
        if referer:
            hdrs.setdefault("Referer", _encode_url((referer or "").split("#")[0]))

        last_err = ""
        total = 0.0
        for attempt in range(self.retries + 1):
            t0 = time.time()
            try:
                s = self._session()
                kwargs = dict(
                    headers=hdrs,
                    timeout=(min(8, self.timeout), self.timeout),
                    allow_redirects=allow_redirect,
                    verify=self.verify_ssl,
                )
                if method.upper() == "POST":
                    m = method.upper()
                    if isinstance(body, dict):
                        kwargs["data"] = body
                    elif isinstance(body, str):
                        kwargs["data"] = body.encode("utf-8")
                    resp = s.request(m, url, **kwargs)
                else:
                    resp = s.request(method.upper(), url, **kwargs)

                raw = resp.content
                enc = encoding or _guess_encoding(resp, raw)
                try:
                    text = raw.decode(enc, errors="replace")
                except (LookupError, TypeError):
                    text = raw.decode("utf-8", errors="replace")

                el = time.time() - t0
                total += el

                # 429/503 是限流，不是源坏了：退避后重试
                if resp.status_code in (429, 503) and attempt < self.retries:
                    time.sleep(1.2 * (attempt + 1))
                    continue

                return FetchResult(True, url=resp.url, status=resp.status_code,
                                   text=text, elapsed=total or el)
            except requests.exceptions.SSLError as e:
                total += time.time() - t0
                last_err = "SSL"
                if self.verify_ssl:
                    try:
                        r = self._session().get(url, timeout=(8, self.timeout), verify=False,
                                               allow_redirects=True, headers=hdrs)
                        raw = r.content
                        enc = encoding or _guess_encoding(r, raw)
                        return FetchResult(True, url=r.url, status=r.status_code,
                                           text=raw.decode(enc, errors="replace"),
                                           elapsed=time.time() - t0)
                    except Exception:
                        pass
                continue
            except requests.exceptions.Timeout:
                total += time.time() - t0
                last_err = "timeout"
                continue
            except requests.exceptions.TooManyRedirects:
                total += time.time() - t0
                last_err = "redirect_loop"
                break
            except requests.exceptions.ConnectionError as e:
                total += time.time() - t0
                msg = str(e)
                last_err = "dns" if ("Name or service not known" in msg or "getaddrinfo" in msg
                                     or "Failed to resolve" in msg) else "conn"
                continue
            except Exception as e:
                total += time.time() - t0
                last_err = "error:%s" % type(e).__name__
                continue
        return FetchResult(False, url=url, error=last_err, elapsed=total)


# ------------------------------------------------------------------ URL 处理
def is_abs(u):
    return bool(re.match(r"^https?://", u or "", re.I))


def join_url(base, path):
    if not path:
        return base or ""
    path = path.strip()
    if is_abs(path):
        return path
    if path.startswith("//"):
        sch = urlparse(base).scheme or "http"
        return f"{sch}:{path}"
    return urljoin(base, path)


_HOST_RE = re.compile(r"^([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$|^\d{1,3}(\.\d{1,3}){3}$")


def is_valid_host(url):
    """判断 bookSourceUrl 是否是正常网址（有些源里填的是诗句/备注）。"""
    h = norm_domain(url)
    if not h:
        return False
    return bool(_HOST_RE.match(h))


def norm_domain(url):
    """归一化域名：去协议、去 www、去端口、去路径、转小写。"""
    u = (url or "").strip()
    if not u:
        return ""
    if not is_abs(u):
        u = "http://" + u
    try:
        p = urlparse(u)
    except Exception:
        return ""
    host = (p.hostname or "").lower().strip(".")
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    # 常见多地 IP 站点共用同一域名，端口保留会误拆线路；这里按需保留
    return host


def parse_search_url(search_url):
    """
    searchUrl 支持两种写法：
      /search?key={{key}}
      https://x.com/s,{"method":"POST","body":"keyword={{key}}","charset":"gbk"}
    返回 (url_template, options_dict)
    """
    s = (search_url or "").strip()
    if not s:
        return "", {}
    opts = {}
    # 逗号后跟 JSON 花括号（用括号配平找结尾，避开 URL 里的逗号）
    m = re.search(r",\s*(\{.*\})\s*$", s, re.S)
    if m:
        try:
            opts = json.loads(m.group(1))
        except Exception:
            opts = {}
        if opts:
            s = s[:m.start()].strip()
    return s, opts if isinstance(opts, dict) else {}


def fill_template(tpl, mapping):
    if not tpl:
        return ""
    out = tpl
    for k, v in mapping.items():
        out = out.replace("{{%s}}" % k, str(v))

    def _js(m):  # {{java.xxx}} 之类不支持，清空
        return ""
    out = re.sub(r"\{\{[^}]*\}\}", _js, out)
    return out


def make_headers(header_field):
    """书源里的 header 字段是 JSON 字符串。"""
    h = {}
    if not header_field:
        return h
    try:
        if isinstance(header_field, dict):
            data = header_field
        else:
            text = header_field.strip()
            # 有些源写了注释/尾逗号
            text = re.sub(r"//[^\n]*", "", text)
            text = re.sub(r",(\s*[}\]])", r"\1", text)
            data = json.loads(text)
        if isinstance(data, dict):
            for k, v in data.items():
                if not isinstance(k, str):
                    continue
                # 非法的 header 名会直接让请求抛 InvalidHeader，丢掉它
                if not re.match(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$", k):
                    continue
                if isinstance(v, (str, int, float)):
                    h[k] = str(v)
    except Exception:
        # 退化处理：逐行 k: v
        for line in str(header_field).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().strip('"')
                v = v.strip().strip('",')
                if k and v:
                    h[k] = v
    h.pop("Content-Length", None)
    h.pop("content-length", None)
    return h
