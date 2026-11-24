#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
失败书源重写工具

读一份「失败书源」清单，逐条重新检测；能靠重写规则救回来的就改规则救回来。

  python fix_failed.py --src 失败书源_1434条.json --out dist --workers 10

输出：
  有效书源_重写_N条.json   重写后跑通（含重写前就跑通的），按延迟从快到慢排序
  仍失败_M条.json          重写也救不回来，字段更新为本次检测结果
  修复报告.md              分原因统计、改了哪些规则
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from bscheck.checker import OK, SourceChecker, reason_step
from bscheck.fetcher import Fetcher
from bscheck.fixer import Fixer

VERSION = "1.2.0"
BJT = timezone(timedelta(hours=8))
_lock = threading.Lock()


def log(*a):
    with _lock:
        print(*a, flush=True)


def now_ms():
    return int(time.time() * 1000)


def stamp():
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M")


def key_of(src):
    return (src.get("bookSourceUrl") or "") + "|" + (src.get("bookSourceName") or "")


def write_json(path, data, indent=None):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def mark_ok(src, res):
    out = dict(src)
    out["zakaCheck"] = "ok"
    out["zakaDelay"] = res.delay_ms
    out["zakaChapters"] = res.chapters
    out["zakaContentLen"] = res.content_len
    out["zakaBook"] = res.book
    out["zakaSearchMs"] = res.t_search
    out["zakaTocMs"] = res.t_toc
    out["zakaContentMs"] = res.t_content
    out["zakaCheckedAt"] = now_ms()
    for k in ("zakaReason", "zakaDetail", "zakaFailStep", "zakaFailTime"):
        out.pop(k, None)
    return out


def mark_fail(src, res):
    out = dict(src)
    out["zakaCheck"] = "fail"
    out["zakaReason"] = res.reason or ""
    out["zakaDetail"] = res.detail or ""
    out["zakaFailStep"] = reason_step(res.reason)
    out["zakaFailTime"] = res.delay_ms
    out["zakaCheckedAt"] = now_ms()
    out.pop("zakaDelay", None)
    return out


def work(src, args):
    ff = Fetcher(timeout=args.timeout, retries=args.retries)
    ck = SourceChecker(fetcher=ff, per_source_timeout=args.per_source_timeout)
    fx = Fixer(fetcher=ff, timeout=args.timeout, min_content=args.min_content,
               budget=args.budget)

    t0 = time.time()
    res0 = ck.check(src)
    if res0.ok:
        return {"key": key_of(src), "status": "ok", "note": "原样可用",
                "src": mark_ok(src, res0), "secs": time.time() - t0}

    # 本机 DNS 解析不了的，重写没有意义（站可能活着）
    if res0.reason == "域名解析失败":
        return {"key": key_of(src), "status": "dns", "note": "本地 DNS 无法解析",
                "src": mark_fail(src, res0), "secs": time.time() - t0}

    fixed, note, res = fx.fix(src, ck, res0=res0)
    if fixed:
        return {"key": key_of(src), "status": "fixed", "note": note,
                "src": mark_ok(fixed, res), "secs": time.time() - t0}
    return {"key": key_of(src), "status": "fail", "note": note,
            "src": mark_fail(src, res or res0), "secs": time.time() - t0}


def main(argv=None):
    p = argparse.ArgumentParser(description="失败书源自动重写")
    p.add_argument("--src", nargs="+", required=True, help="失败书源文件（json）")
    p.add_argument("--out", default="dist_fix", help="输出目录")
    p.add_argument("--workers", type=int, default=10)
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--min-content", type=int, default=200)
    p.add_argument("--per-source-timeout", type=float, default=45)
    p.add_argument("--budget", type=float, default=30, help="单条重写时间预算（秒）")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试）")
    p.add_argument("--resume", action="store_true", help="复用已有中间结果")
    args = p.parse_args(argv)

    srcs = []
    for f in args.src:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("items") or data.get("sources") or []
        srcs.extend(x for x in data if isinstance(x, dict))
    if args.limit:
        srcs = srcs[:args.limit]
    log("读到失败书源 %d 条" % len(srcs))

    os.makedirs(args.out, exist_ok=True)
    todo = srcs
    if args.resume:
        done = {}
        mid = os.path.join(args.out, "中间结果.jsonl")
        if os.path.exists(mid):
            with open(mid, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    done[rec["key"]] = rec
            todo = [s for s in srcs if key_of(s) not in done]
            log("断点续跑：已完成 %d 条，剩 %d 条" % (len(done), len(todo)))
        else:
            done = {}
    else:
        done = {}

    mid = os.path.join(args.out, "中间结果.jsonl")
    t0 = time.time()
    stats = {"ok": 0, "fixed": 0, "fail": 0, "dns": 0}
    with open(mid, "a", encoding="utf-8") as fout, \
            ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(work, s, args): s for s in todo}
        for fut in as_completed(futs):
            try:
                rec = fut.result()
            except Exception as e:
                s = futs[fut]
                rec = {"key": key_of(s), "status": "fail",
                       "note": "异常 %s" % type(e).__name__, "src": s, "secs": 0}
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fout.flush()
            done[rec["key"]] = rec
            stats[rec["status"]] = stats.get(rec["status"], 0) + 1
            n = sum(stats.values())
            el = time.time() - t0
            speed = n / el if el else 0
            eta = (len(todo) - n) / speed if speed else 0
            if n % 20 == 0 or n == len(todo):
                log("  进度 %d/%d  原样可用 %d  重写救回 %d  仍失败 %d  本地DNS %d  剩余约 %.0fs"
                    % (n, len(todo), stats["ok"], stats["fixed"], stats["fail"],
                       stats["dns"], eta))

    ok_srcs, bad_srcs = [], []
    notes = {}
    for rec in done.values():
        if rec["status"] in ("ok", "fixed"):
            ok_srcs.append(rec["src"])
            if rec["status"] == "fixed":
                notes[rec["note"]] = notes.get(rec["note"], 0) + 1
        else:
            bad_srcs.append(rec["src"])

    ok_srcs.sort(key=lambda x: x.get("zakaDelay") or 10 ** 9)
    bad_srcs.sort(key=lambda x: (x.get("zakaFailStep") or "", x.get("zakaReason") or ""))

    ok_file = os.path.join(args.out, "有效书源_重写_%d条.json" % len(ok_srcs))
    bad_file = os.path.join(args.out, "仍失败_%d条.json" % len(bad_srcs))
    write_json(ok_file, ok_srcs, indent=2)
    write_json(bad_file, bad_srcs, indent=2)

    # 报告
    from collections import Counter
    cnt = Counter((s.get("zakaFailStep"), s.get("zakaReason")) for s in bad_srcs)
    lines = ["# 失败书源重写报告", "",
             "- 生成时间：%s" % stamp(),
             "- 输入：%d 条" % len(srcs),
             "- 重写后可用：**%d 条**（其中原样可用 %d、重写救回 %d）"
             % (len(ok_srcs), sum(1 for r in done.values() if r["status"] == "ok"),
                sum(1 for r in done.values() if r["status"] == "fixed")),
             "- 仍失败：%d 条" % len(bad_srcs), ""]
    if notes:
        lines += ["## 重写方式统计", ""]
        for k, v in sorted(notes.items(), key=lambda kv: -kv[1]):
            lines.append("- %s：%d 条" % (k, v))
        lines.append("")
    lines += ["## 仍失败原因分布", "", "| 卡在哪步 | 原因 | 条数 |", "|---|---|---|"]
    for (step, reason), v in cnt.most_common():
        lines.append("| %s | %s | %d |" % (step, reason, v))
    lines += ["", "## 说明", "",
              "- 重写方式：按卡点重建搜索/目录/正文规则，重建后必须真跑通「搜索→目录→正文」三步才算有效。",
              "- 标了「本地 DNS」的是本机解析不了域名，不代表源坏了，换网络或用 GitHub Actions 跑通常就正常。",
              "- 重写后的书源保留了原本的分组、名称和其它字段，只改了失效的地址与规则。"]
    md = os.path.join(args.out, "修复报告.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log("\n完成：可用 %d 条 / 仍失败 %d 条  (耗时 %.0fs)" % (len(ok_srcs), len(bad_srcs),
                                                        time.time() - t0))
    log("  %s" % ok_file)
    log("  %s" % bad_file)
    log("  %s" % md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
