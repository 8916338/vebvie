#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书源加载 + 域名级合并去重。"""

import glob
import json
import os
import re

from .fetcher import norm_domain

RULE_GROUPS = ("ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent", "ruleExplore")


# --------------------------------------------------------------------- 加载
def _load_json_file(path):
    """读取一个书源文件，容忍 BOM / 全角逗号 / 尾逗号。返回 (list, err)"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return [], "read_fail:%s" % e

    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], "decode_fail"

    text = text.strip()
    if not text:
        return [], "empty"

    # 去掉 BOM / 零宽字符
    text = text.lstrip("\ufeff\u200b")

    try:
        data = json.loads(text)
    except Exception:
        # 修常见毛病：中文全角逗号（只在引号外）、对象/数组尾逗号
        fixed = _repair_json(text)
        try:
            data = json.loads(fixed, strict=False)
        except Exception as e:
            return [], "json_error:%s" % str(e)[:80]

    if isinstance(data, dict):
        # 有些导出是 {"sources": [...]}
        for k in ("sources", "data", "bookSources", "list", "items"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            data = [data]

    if not isinstance(data, list):
        return [], "not_list"

    items = [x for x in data if isinstance(x, dict) and (x.get("bookSourceUrl") or x.get("bookSourceName"))]
    return items, ""


def _repair_json(text):
    """把全角逗号/引号等中文标点在字符串外修正。"""
    out = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
        elif ch in "，、":
            out.append(",")
        elif ch in "“”":
            out.append('"')
        elif ch in "：":
            out.append(":")
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r",(\s*[}\]])", r"\1", s)  # 尾逗号
    return s


def iter_source_files(paths):
    """展开目录/文件/通配符，返回 json 文件列表。"""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for ext in ("json", "JSON", "txt", "TXT"):
                files.extend(glob.glob(os.path.join(p, "**", "*." + ext), recursive=True))
        elif any(c in p for c in "*?["):
            files.extend(glob.glob(p, recursive=True))
        elif os.path.isfile(p):
            files.append(p)
    seen, out = set(), []
    for f in sorted(files):
        ap = os.path.realpath(f)
        if ap in seen:
            continue
        seen.add(ap)
        out.append(f)
    return out


def load_sources(paths, log=print):
    """返回 (sources, stats)"""
    files = iter_source_files(paths)
    if not files:
        log("没有找到任何书源文件")
        return [], {"files": 0, "raw": 0, "bad": 0}

    all_items = []
    stats = {"files": len(files), "raw": 0, "bad": 0, "bad_files": []}
    for f in files:
        items, err = _load_json_file(f)
        if err:
            stats["bad"] += 1
            stats["bad_files"].append((os.path.basename(f), err))
            log("  跳过 %s (%s)" % (os.path.basename(f), err))
            continue
        stats["raw"] += len(items)
        all_items.extend(items)
        log("  %s → %d 条" % (os.path.basename(f), len(items)))
    return all_items, stats


# ----------------------------------------------------------------- 质量评分
def _rule_score(src):
    """规则完整度打分，用于同域名时择优保留。"""
    score = 0
    if src.get("enabled", True):
        score += 50
    for g in RULE_GROUPS:
        r = src.get(g)
        if isinstance(r, dict):
            score += sum(1 for v in r.values() if isinstance(v, str) and v.strip())
    if (src.get("searchUrl") or "").strip():
        score += 20
    if (src.get("ruleToc") or {}).get("chapterList"):
        score += 10
    if (src.get("ruleContent") or {}).get("content"):
        score += 10
    # 原有响应时间小的更优
    try:
        rt = float(src.get("respondTime") or 0)
        if rt > 0:
            score += max(0, min(20, 20000 / rt))
    except Exception:
        pass
    return score


def _url_key(url):
    """精确 URL key（协议+host+path 归一）。"""
    u = (url or "").split("#")[0].strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = u.rstrip("/")
    return u


def merge_sources(sources, ignore_port=True, ignore_protocol=True, log=print):
    """
    两级去重：
      1. 完全相同的 bookSourceUrl → 只留最完整的一条
      2. 同一域名 → 只留最完整的一条（多线路站点合并）
    返回 (kept, stats)
    """
    stats = {"input": len(sources), "exact_dup": 0, "domain_dup": 0}

    # 1) 精确 URL 去重
    exact = {}
    order = []
    for s in sources:
        k = _url_key(s.get("bookSourceUrl"))
        if not k:
            k = "noname:" + (s.get("bookSourceName") or "")
        if k in exact:
            stats["exact_dup"] += 1
            if _rule_score(s) > _rule_score(exact[k]):
                exact[k] = s
        else:
            exact[k] = s
            order.append(k)

    uniq = [exact[k] for k in order]

    # 2) 域名去重
    by_domain = {}
    dom_order = []
    for s in uniq:
        d = norm_domain(s.get("bookSourceUrl"))
        if not d:
            d = "nodomain:" + (s.get("bookSourceName") or "")
        s["_zaka_domain_key"] = d
        if d in by_domain:
            stats["domain_dup"] += 1
            kept = by_domain[d]
            kept.setdefault("_zaka_alias", [kept.get("bookSourceName") or ""])
            kept["_zaka_alias"].append(s.get("bookSourceName") or "")
            kept.setdefault("_zaka_alt_urls", [])
            alt = (s.get("bookSourceUrl") or "").strip()
            if alt and alt not in kept["_zaka_alt_urls"]:
                kept["_zaka_alt_urls"].append(alt)
            if _rule_score(s) > _rule_score(kept):
                s.setdefault("_zaka_alias", [])
                s["_zaka_alias"] = kept.get("_zaka_alias", []) + (s.get("_zaka_alias") or [])
                s.setdefault("_zaka_alt_urls", [])
                s["_zaka_alt_urls"] = kept.get("_zaka_alt_urls", []) + (s.get("_zaka_alt_urls") or [])
                by_domain[d] = s
        else:
            by_domain[d] = s
            dom_order.append(d)

    kept = [by_domain[d] for d in dom_order]

    # 合并信息写回字段（标准字段，阅读可忽略）
    for s in kept:
        alias = s.pop("_zaka_alias", None)
        alts = s.pop("_zaka_alt_urls", None)
        dom = s.pop("_zaka_domain_key", "")
        if alias:
            uniq_alias = []
            for a in alias:
                if a and a not in uniq_alias:
                    uniq_alias.append(a)
            if len(uniq_alias) > 1:
                s["zakaAlias"] = uniq_alias
        if alts:
            s["zakaAltUrls"] = [u for u in alts if u][:5]
        if dom:
            s["zakaDomain"] = dom

    stats["after"] = len(kept)
    return kept, stats
