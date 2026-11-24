#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并分片结果：把 dist/ 下各分片产出的有效/失败书源合成一份，有效按延迟重排。"""

import glob
import json
import os
import sys

LEAN_KEYS = ("bookSourceName", "bookSourceUrl", "bookSourceGroup", "zakaDomain",
             "zakaCheck", "zakaFailStep", "zakaReason", "zakaDetail", "zakaFailTime")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def key_of(s):
    return ((s.get("zakaDomain") or "") + "|" + (s.get("bookSourceName") or "")).lower()


def dedupe(items):
    seen, out = set(), []
    for s in items:
        k = key_of(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def dump(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main(out="dist"):
    ok_files = sorted(glob.glob(os.path.join(out, "有效书源_*条_s*of*.json")))
    bad_files = sorted(glob.glob(os.path.join(out, "失败书源_[0-9]*条_s*of*.json")))
    if not bad_files:  # 兼容旧版分片名
        bad_files = sorted(glob.glob(os.path.join(out, "无效书源_*条_s*of*.json")))
    if not ok_files and not bad_files:
        print("没有找到分片文件")
        return 1

    merged = dedupe([s for f in ok_files for s in load(f)])
    merged.sort(key=lambda s: s.get("zakaDelay") or 10 ** 9)

    ok_path = os.path.join(out, "有效书源_%d条.json" % len(merged))
    dump(ok_path, merged)

    bad = dedupe([s for f in bad_files for s in load(f)])
    bad.sort(key=lambda s: (s.get("zakaFailStep") or "", s.get("zakaReason") or ""))
    bad_path = os.path.join(out, "失败书源_%d条.json" % len(bad))
    dump(bad_path, bad)

    lean_path = os.path.join(out, "失败书源_精简_%d条.json" % len(bad))
    dump(lean_path, [{k: s.get(k) for k in LEAN_KEYS if k in s} for s in bad])

    print("合并完成：有效 %d 条 → %s" % (len(merged), ok_path))
    print("          失败 %d 条 → %s" % (len(bad), bad_path))
    print("          失败精简 → %s" % lean_path)

    if merged:
        print("\n最快 10 个：")
        for i, s in enumerate(merged[:10], 1):
            print("  %2d. %6.1fs  %s" % (i, (s.get("zakaDelay") or 0) / 1000.0,
                                         s.get("bookSourceName")))

    if bad:
        print("\n失败原因分布：")
        stat = {}
        for s in bad:
            k = "%s / %s" % (s.get("zakaFailStep") or "-", s.get("zakaReason") or "-")
            stat[k] = stat.get(k, 0) + 1
        for k, v in sorted(stat.items(), key=lambda x: -x[1]):
            print("  %-24s %d" % (k, v))

    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write("merged_file=%s\n" % ok_path)
            f.write("merged_count=%d\n" % len(merged))
            f.write("fail_file=%s\n" % bad_path)
            f.write("fail_count=%d\n" % len(bad))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist"))
