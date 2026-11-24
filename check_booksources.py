#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
书源有效性检测工具

流程：读取书源文件 → 按域名合并去重 → 并发检测（搜索/目录/正文）→
      写入延迟耗时并按快慢排序 → 输出有效书源 + 报告 + 打包。

用法示例：
    python check_booksources.py --src booksources --out dist
    python check_booksources.py --src a.json b.json --workers 40 --limit 200
    python check_booksources.py --src booksources --resume      # 断点续跑
"""

import argparse
import csv
import json
import os
import shutil
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from bscheck.checker import (F_JS, F_NO_SEARCH, OK, STEP_ORDER, CheckResult,
                             SourceChecker, reason_step)
from bscheck.dedupe import load_sources, merge_sources
from bscheck.fetcher import Fetcher, norm_domain

VERSION = "1.1.0"
BJT = timezone(timedelta(hours=8))

DEFAULT_KEYWORDS = ["斗罗大陆", "诡秘之主", "三体", "剑来", "凡人修仙传"]

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def now_str():
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")


def fmt_ms(ms):
    if ms < 1000:
        return "%dms" % ms
    return "%.1fs" % (ms / 1000.0)


def safe_name(s, maxlen=40):
    s = (s or "source").strip()
    s = "".join(ch for ch in s if ch not in '\\/:*?"<>|\n\r\t')
    return s[:maxlen] or "source"


# --------------------------------------------------------------------- 参数
def build_parser():
    p = argparse.ArgumentParser(
        description="书源有效性检测：合并去重 → 搜索/目录/正文验证 → 按延迟排序输出")
    p.add_argument("--src", nargs="+", default=["booksources"],
                   help="书源文件或目录（可多个，目录会递归扫描 .json/.txt）")
    p.add_argument("--out", default="dist", help="输出目录，默认 dist")
    p.add_argument("--workers", type=int, default=32, help="并发线程数，默认 32")
    p.add_argument("--timeout", type=float, default=12, help="单请求超时秒数，默认 12")
    p.add_argument("--retries", type=int, default=1, help="单请求重试次数，默认 1")
    p.add_argument("--min-content", type=int, default=200, help="正文最少字数，默认 200")
    p.add_argument("--keywords", default="", help="搜索关键词，逗号分隔")
    p.add_argument("--limit", type=int, default=0, help="只检测前 N 条（调试用）")
    p.add_argument("--shards", type=int, default=1, help="分片总数（GitHub Actions 矩阵用）")
    p.add_argument("--shard", type=int, default=0, help="当前分片编号，从 0 开始")
    p.add_argument("--no-merge", action="store_true", help="不做域名合并，只做精确 URL 去重")
    p.add_argument("--resume", action="store_true", help="复用已有结果，跳过已检测的源")
    p.add_argument("--proxy", default="", help="HTTP 代理，如 http://127.0.0.1:7890")
    p.add_argument("--deadline-min", type=float, default=0, help="整体时间预算（分钟），0=不限")
    p.add_argument("--keep-all-fields", action="store_true",
                   help="（兼容旧版，现已默认保留完整字段）")
    p.add_argument("--fail-lean", action="store_true",
                   help="失败书源只输出名称/地址/分组/原因等精简字段（默认保留全部原始字段）")
    p.add_argument("--quiet", action="store_true")
    return p


# ------------------------------------------------------------------ 结果落盘
def result_path(out, shard, shards):
    if shards > 1:
        return os.path.join(out, "_partial", "result_%02d.jsonl" % shard)
    return os.path.join(out, "_partial", "result.jsonl")


def load_done(path):
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = rec.get("_key")
            if key:
                done[key] = rec
    return done


def source_key(src):
    from bscheck.fetcher import norm_domain
    d = norm_domain(src.get("bookSourceUrl")) or ""
    return (d + "|" + (src.get("bookSourceName") or "")).lower()


# ---------------------------------------------------------------------- 主流程
def main(argv=None):
    args = build_parser().parse_args(argv)
    t_all = time.time()

    os.makedirs(args.out, exist_ok=True)
    partial = os.path.join(args.out, "_partial")
    os.makedirs(partial, exist_ok=True)

    log("=" * 62)
    log("书源有效性检测 v%s   %s" % (VERSION, now_str()))
    log("=" * 62)

    # 1) 读取
    log("\n[1/5] 读取书源文件")
    sources, load_stats = load_sources(args.src, log=log if not args.quiet else (lambda *a: None))
    if not sources:
        log("没有可用书源，退出")
        return 2
    log("  合计读到 %d 条（文件 %d 个，读取失败 %d 个）"
        % (load_stats["raw"], load_stats["files"], load_stats["bad"]))

    # 2) 合并去重
    log("\n[2/5] 域名合并去重")
    if args.no_merge:
        seen, merged = set(), []
        for s in sources:
            k = (s.get("bookSourceUrl") or "").split("#")[0].strip().lower().rstrip("/")
            if k in seen:
                continue
            seen.add(k)
            merged.append(s)
        mstats = {"input": len(sources), "exact_dup": len(sources) - len(merged),
                  "domain_dup": 0, "after": len(merged)}
    else:
        merged, mstats = merge_sources(sources, log=log)
    log("  输入 %d → 精确重复去掉 %d → 同域名合并 %d → 剩 %d 条"
        % (mstats["input"], mstats["exact_dup"], mstats.get("domain_dup", 0), len(merged)))

    # 分片
    if args.shards > 1:
        merged = [s for i, s in enumerate(merged) if i % args.shards == args.shard]
        log("  分片 %d/%d：本片 %d 条" % (args.shard + 1, args.shards, len(merged)))

    if args.limit and len(merged) > args.limit:
        merged = merged[:args.limit]
        log("  限制只测前 %d 条" % args.limit)

    # 3) 检测
    kw = [k.strip() for k in args.keywords.split(",") if k.strip()] or DEFAULT_KEYWORDS
    fetcher = Fetcher(timeout=args.timeout, retries=args.retries, proxy=args.proxy or None)
    checker = SourceChecker(fetcher=fetcher, keywords=kw, min_content=args.min_content)

    rpath = result_path(args.out, args.shard, args.shards)
    done = load_done(rpath) if args.resume else {}
    if done:
        log("  断点续跑：已有 %d 条结果，跳过" % len(done))

    todo = [s for s in merged if source_key(s) not in done]
    log("\n[3/5] 开始检测：%d 条待测，并发 %d，单条超时 %ss"
        % (len(todo), args.workers, args.timeout))

    deadline = None
    if args.deadline_min > 0:
        deadline = time.time() + args.deadline_min * 60

    stats = {"ok": 0, "fail": 0, "skip": 0, "done": 0}
    reasons = {}
    results = []
    stop_flag = threading.Event()

    fout = open(rpath, "a", encoding="utf-8")

    def work(src):
        if stop_flag.is_set():
            return None
        try:
            r = checker.check(src)
        except Exception as e:
            r = CheckResult()
            r.reason = "异常: %s" % type(e).__name__
            r.detail = str(e)[:120]
        rec = {
            "_key": source_key(src),
            "name": src.get("bookSourceName") or "",
            "url": src.get("bookSourceUrl") or "",
            "groups": src.get("bookSourceGroup") or "",
            **r.to_dict(),
        }
        return rec

    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futs = {pool.submit(work, s): s for s in todo}
            for fut in as_completed(futs):
                if deadline and time.time() > deadline and not stop_flag.is_set():
                    stop_flag.set()
                    log("\n  已到时间预算，停止派发新任务，先落盘已完成的")
                rec = fut.result()
                if rec is None:
                    stats["skip"] += 1
                    continue
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fout.flush()
                results.append(rec)
                stats["done"] += 1
                if rec["ok"]:
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1
                    reasons[rec["reason"]] = reasons.get(rec["reason"], 0) + 1

                n = stats["done"]
                if n % 25 == 0 or n == len(todo):
                    el = time.time() - t0
                    speed = n / el if el > 0 else 0
                    eta = (len(todo) - n) / speed if speed > 0 else 0
                    log("  进度 %d/%d  有效 %d  无效 %d  速度 %.1f条/s  剩余约 %.0fs"
                        % (n, len(todo), stats["ok"], stats["fail"], speed, eta))
    finally:
        fout.close()

    # 4) 汇总（含断点续跑的历史结果）
    all_recs = list(load_done(rpath).values())
    # 只保留本轮合并后的源
    keys = {source_key(s) for s in merged}
    all_recs = [r for r in all_recs if r.get("_key") in keys]

    by_key = {r["_key"]: r for r in all_recs}
    ok_srcs, bad_srcs = [], []
    for s in merged:
        k = source_key(s)
        r = by_key.get(k)
        if not r:
            continue
        src = dict(s)
        src.pop("_zaka_domain_key", None)
        if r.get("book") and not src.get("zakaBook"):
            src["zakaBook"] = r["book"]
        if r.get("chapters"):
            src["zakaChapters"] = r["chapters"]
        if r.get("contentLen"):
            src["zakaContentLen"] = r["contentLen"]
        if r.get("ok"):
            src["zakaCheck"] = "ok"
            src["zakaDelay"] = int(r.get("delayMs") or 0)
            ok_srcs.append((src, r))
        else:
            reason = r.get("reason") or ""
            src["zakaCheck"] = "fail"
            src["zakaReason"] = reason
            src["zakaFailStep"] = reason_step(reason)
            src["zakaFailTime"] = int(r.get("delayMs") or 0)
            if not src.get("zakaDomain"):
                src["zakaDomain"] = norm_domain(src.get("bookSourceUrl")) or ""
            if r.get("detail"):
                src["zakaDetail"] = r["detail"]
            if r.get("book"):
                src["zakaBook"] = r["book"]
            if r.get("chapters"):
                src["zakaChapters"] = r["chapters"]
            if r.get("contentLen"):
                src["zakaContentLen"] = r["contentLen"]
            bad_srcs.append((src, r))

    # 5) 输出
    log("\n[4/5] 整理输出")
    ok_srcs.sort(key=lambda x: x[1].get("delayMs") or 10 ** 9)
    bad_srcs.sort(key=lambda x: (STEP_ORDER.get(x[0].get("zakaFailStep"), 9),
                                 x[1].get("reason") or "",
                                 x[1].get("delayMs") or 0))

    stamp = now_str().replace(":", "").replace(" ", "_").replace("-", "")
    shard_suffix = "" if args.shards == 1 else "_s%dof%d" % (args.shard, args.shards)

    ok_file = os.path.join(args.out, "有效书源_%d条%s.json" % (len(ok_srcs), shard_suffix))
    write_json(ok_file, [s for s, _ in ok_srcs])

    # ---- 失败书源：完整字段版（可直接导入阅读器/拿去修）+ 精简清单 + 明细表 ----
    bad_file = os.path.join(args.out, "失败书源_%d条%s.json" % (len(bad_srcs), shard_suffix))
    write_json(bad_file, [fail_lean(s) if args.fail_lean else s for s, _ in bad_srcs])

    bad_lean_file = os.path.join(args.out, "失败书源_精简_%d条%s.json"
                                 % (len(bad_srcs), shard_suffix))
    write_json(bad_lean_file, [fail_lean(s) for s, _ in bad_srcs])

    bad_csv = os.path.join(args.out, "失败书源明细%s.csv" % shard_suffix)
    with open(bad_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "失败步骤", "原因", "详情", "失败耗时ms", "书源名", "域名",
                    "分组", "书源地址", "搜到的书名", "章节数", "正文字数"])
        for i, (s, r) in enumerate(bad_srcs, 1):
            w.writerow([i, s.get("zakaFailStep", ""), s.get("zakaReason", ""),
                        r.get("detail", ""), r.get("delayMs", 0),
                        s.get("bookSourceName", ""), s.get("zakaDomain", ""),
                        s.get("bookSourceGroup", ""), s.get("bookSourceUrl", ""),
                        r.get("book", ""), r.get("chapters", 0), r.get("contentLen", 0)])

    csv_file = os.path.join(args.out, "检测报告%s.csv" % shard_suffix)
    with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["排名", "状态", "延迟ms", "搜索ms", "目录ms", "正文ms", "书名", "章节数",
                    "正文字数", "原因", "书源名", "域名", "分组"])
        rank = 0
        for s, r in ok_srcs:
            rank += 1
            w.writerow([rank, "有效", r.get("delayMs"), r.get("searchMs"), r.get("tocMs"),
                        r.get("contentMs"), r.get("book"), r.get("chapters"),
                        r.get("contentLen"), "", s.get("bookSourceName"),
                        s.get("zakaDomain", ""), s.get("bookSourceGroup", "")])
        for s, r in bad_srcs:
            w.writerow(["-", "无效", "", "", "", "", "", "", "", r.get("reason"),
                        s.get("bookSourceName"), s.get("zakaDomain", ""),
                        s.get("bookSourceGroup", "")])

    md_file = os.path.join(args.out, "检测汇总%s.md" % shard_suffix)
    write_summary(md_file, ok_srcs, bad_srcs, mstats, load_stats, reasons,
                  time.time() - t_all, args, stamp)

    # 打包
    zip_file = os.path.join(args.out, "有效书源_打包%s.zip" % shard_suffix)
    with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ok_file, os.path.basename(ok_file))
        z.write(md_file, os.path.basename(md_file))
        z.write(csv_file, os.path.basename(csv_file))
        for extra in (bad_file, bad_lean_file, bad_csv):
            if os.path.exists(extra):
                z.write(extra, os.path.basename(extra))

    log("  有效 %d 条 / 失败 %d 条 / 跳过 %d 条"
        % (len(ok_srcs), len(bad_srcs), stats["skip"]))
    if reasons:
        log("  失败原因分布：")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            log("    %-14s %4d   （卡在：%s）" % (k, v, reason_step(k)))

    if bad_srcs:
        log("\n  失败书源前 10 条：")
        for i, (s, r) in enumerate(bad_srcs[:10], 1):
            log("   %2d. [%s] %-14s %-18s %s" % (
                i, s.get("zakaFailStep", ""), safe_name(s.get("bookSourceName"), 14),
                s.get("zakaDomain", ""), (r.get("detail") or "")[:40]))

    if ok_srcs:
        log("\n  最快 10 个：")
        for i, (s, r) in enumerate(ok_srcs[:10], 1):
            log("   %2d. %-6s %-16s %s" % (i, fmt_ms(r["delayMs"]),
                                           safe_name(s.get("bookSourceName"), 16),
                                           s.get("zakaDomain", "")))

    log("\n[5/5] 完成，用时 %s" % fmt_ms(int((time.time() - t_all) * 1000)))
    log("  有效书源：%s" % ok_file)
    if bad_srcs:
        log("  失败书源：%s" % bad_file)
        log("  失败精简：%s" % bad_lean_file)
        log("  失败明细：%s" % bad_csv)
    log("  检测报告：%s" % csv_file)
    log("  打包文件：%s" % zip_file)

    # 给 GitHub Actions 用的输出
    write_github_output(ok_file, zip_file, len(ok_srcs), len(bad_srcs),
                        bad_file, bad_csv)
    return 0


def fail_lean(s):
    """失败源的精简写法：只留能一眼看出"哪个源、哪个站、卡在哪"。"""
    return {
        "bookSourceName": s.get("bookSourceName"),
        "bookSourceUrl": s.get("bookSourceUrl"),
        "bookSourceGroup": s.get("bookSourceGroup", ""),
        "zakaDomain": s.get("zakaDomain", ""),
        "zakaCheck": "fail",
        "zakaFailStep": s.get("zakaFailStep", ""),
        "zakaReason": s.get("zakaReason"),
        "zakaDetail": s.get("zakaDetail", ""),
        "zakaFailTime": s.get("zakaFailTime"),
    }


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_summary(path, ok_srcs, bad_srcs, mstats, load_stats, reasons, elapsed, args, stamp):
    total = len(ok_srcs) + len(bad_srcs)
    rate = (len(ok_srcs) / total * 100) if total else 0
    lines = []
    lines.append("# 书源检测汇总\n")
    lines.append("- 检测时间：%s（北京时间）" % now_str())
    lines.append("- 工具版本：v%s" % VERSION)
    lines.append("- 读取文件：%d 个，原始 %d 条" % (load_stats["files"], load_stats["raw"]))
    lines.append("- 精确重复剔除：%d 条" % mstats.get("exact_dup", 0))
    lines.append("- 同域名合并剔除：%d 条" % mstats.get("domain_dup", 0))
    lines.append("- 实检：%d 条" % total)
    lines.append("- **有效：%d 条（%.1f%%）**" % (len(ok_srcs), rate))
    lines.append("- 失败：%d 条" % len(bad_srcs))
    lines.append("- 总耗时：%s\n" % fmt_ms(int(elapsed * 1000)))

    if reasons:
        lines.append("## 失败原因分布\n")
        lines.append("| 卡在 | 原因 | 数量 |")
        lines.append("|---|---|---|")
        for k, v in sorted(reasons.items(),
                           key=lambda x: (STEP_ORDER.get(reason_step(x[0]), 9), -x[1])):
            lines.append("| %s | %s | %d |" % (reason_step(k), k, v))
        lines.append("")

    if bad_srcs:
        lines.append("## 失败书源明细（前 30 条）\n")
        lines.append("| # | 卡在 | 原因 | 书源 | 域名 | 详情 | 耗时 |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, (s, r) in enumerate(bad_srcs[:30], 1):
            lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
                i, esc(s.get("zakaFailStep")), esc(s.get("zakaReason")),
                esc(s.get("bookSourceName")), esc(s.get("zakaDomain")),
                esc((r.get("detail") or "")[:60]), fmt_ms(r.get("delayMs") or 0)))
        if len(bad_srcs) > 30:
            lines.append("| … | | | 共 %d 条，全量见 失败书源明细.csv | | | |" % len(bad_srcs))
        lines.append("")
        lines.append("失败源完整清单见 `失败书源_%d条.json`（保留全部原始规则字段，"
                     "可直接导入阅读器或拿去修），精简清单见 `失败书源_精简_%d条.json`。\n"
                     % (len(bad_srcs), len(bad_srcs)))

    if ok_srcs:
        lines.append("## 延迟最快 30 个\n")
        lines.append("| # | 延迟 | 书源 | 域名 | 搜索 | 目录 | 正文 | 章节 | 正文字数 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, (s, r) in enumerate(ok_srcs[:30], 1):
            lines.append("| %d | %s | %s | %s | %s | %s | %s | %d | %d |" % (
                i, fmt_ms(r.get("delayMs", 0)), esc(s.get("bookSourceName")),
                esc(s.get("zakaDomain")), fmt_ms(r.get("searchMs", 0)),
                fmt_ms(r.get("tocMs", 0)), fmt_ms(r.get("contentMs", 0)),
                r.get("chapters", 0), r.get("contentLen", 0)))
        lines.append("")

    lines.append("## 判定标准\n")
    lines.append("一本书要连续走通三步才算有效：")
    lines.append("1. **搜索**：能按书名搜出结果，并拿到书籍链接")
    lines.append("2. **目录**：打开书籍页/目录页，能解析出章节列表")
    lines.append("3. **正文**：打开章节页，能取到正文且字数 ≥ %d\n" % args.min_content)
    lines.append("`zakaDelay` 字段 = 从发起搜索到拿到正文的总耗时（毫秒），已按此从小到大排序。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def esc(s):
    return (s or "").replace("|", "/").replace("\n", " ")


def write_github_output(ok_file, zip_file, n_ok, n_bad, bad_file="", bad_csv=""):
    p = os.environ.get("GITHUB_OUTPUT")
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("ok_file=%s\n" % ok_file)
            f.write("zip_file=%s\n" % zip_file)
            f.write("ok_count=%d\n" % n_ok)
            f.write("bad_count=%d\n" % n_bad)
            if bad_file:
                f.write("bad_file=%s\n" % bad_file)
            if bad_csv:
                f.write("bad_csv=%s\n" % bad_csv)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
