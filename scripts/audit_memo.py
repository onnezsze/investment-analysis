#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_memo.py — 备忘录数字对账器（质量门 Step 5 的自动化）

把备忘录里出现的每个数字，回头到 snapshot JSON 里找是否存在。
找不到的数字会被列出来，让 agent 逐条确认来源（新闻/官方财报/推导计算），
从而抓出「编造的数字」和「重跑取数后没同步的陈旧数字」。

用法:
  python3 audit_memo.py memo_NVDA_20260922.md snapshot_NVDA_20260922.json
  python3 audit_memo.py memo.md snapshot.json --extra 962.21,89.5   # 已知来自新闻/推算的数字
  python3 audit_memo.py memo.md snapshot.json --strict              # 有未确认数字则 exit 1

判读原则:
  · 输出分三组：① 命中快照 ② 命中推导值(同比/占比/风险回报比等) ③ 待确认
  · 第① ②组 = 可回溯；第③组必须人工给出出处，给不出就是编造，必须删掉或改。
"""
from __future__ import annotations

import argparse
import json
import re
import sys


UNIT_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
UNIT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*([KMBT])\b")


def leaf_numbers(obj, out=None):
    out = out if out is not None else set()
    if isinstance(obj, dict):
        for v in obj.values():
            leaf_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            leaf_numbers(v, out)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        for m in UNIT_RE.finditer(obj):          # "441.55B" / "5.49T" / "1.35M"
            out.add(float(m.group(1).replace(",", "")) * UNIT_MULT[m.group(2)])
    return out


def num_variants(x: float) -> set[str]:
    """一个数值可能的书写形式：含小数位与单位换算（亿/百万/十亿/万亿）"""
    v = set()
    for nd in (0, 1, 2, 3, 4):
        s = f"{x:.{nd}f}"
        v.add(s)
        try:
            v.add(f"{int(float(s)):,}" if nd == 0 else f"{float(s):,.{nd}f}")
        except (ValueError, OverflowError):
            pass
    for scale in (1e4, 1e6, 1e8, 1e9, 1e12):   # 中文「亿」=1e8，百万/十亿/万亿
        y = x / scale
        for nd in (0, 1, 2, 3, 4):
            v.add(f"{y:.{nd}f}")
            try:
                v.add(f"{y:,.{nd}f}")
            except (ValueError, OverflowError):
                pass
    return v


def derived_pool(nums: set[float]) -> set[str]:
    """从快照数值派生的常见计算：百分比变化、占比、比率、风险回报比"""
    pool = set()
    vals = [n for n in nums if n != 0][:600]
    for a in vals:
        for b in vals:
            if a is b:
                continue
            try:
                pool |= num_variants((a / b - 1) * 100)      # 同比/涨跌幅
                pool |= num_variants(a / b * 100)            # 占比
                pool |= num_variants(a / b)                  # 倍数
                pool |= num_variants(a - b)                  # 差值
                pool |= num_variants(a + b)                  # 差值
            except ZeroDivisionError:
                continue
    return pool


NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def extract_memo_numbers(text: str) -> list[str]:
    text = text.replace("−", "-").replace("–", "-")
    out = []
    for m in NUM_RE.finditer(text):
        tok = m.group(0)
        # 过滤年份、页码、纯序号等噪音
        plain = tok.replace(",", "")
        try:
            f = float(plain)
        except ValueError:
            continue
        if f == float(int(f)) and 1900 <= f <= 2100 and "," not in tok and "." not in tok:
            continue  # 年份
        if len(plain.replace(".", "")) < 2:
            continue
        if re.fullmatch(r"0\d", plain):
            continue  # 月/日/时的时间片段
        # 逗号必须是标准千分位（过滤 12,26,9 这类参数列表）
        if "," in tok and not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", tok):
            continue
        out.append(tok)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="备忘录数字对账器")
    ap.add_argument("memo")
    ap.add_argument("snapshot")
    ap.add_argument("--extra", default="", help="已知来自新闻/外部的数字，逗号分隔")
    ap.add_argument("--strict", action="store_true", help="存在待确认数字时 exit 1")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    raw = json.loads(open(a.snapshot, encoding="utf-8").read())
    memo = open(a.memo, encoding="utf-8").read()
    nums = leaf_numbers(raw)

    hit_exact, hit_derived, pool_exact, pool_derived = [], [], set(), set()
    for n in nums:
        pool_exact |= num_variants(n)
    pool_derived = derived_pool(nums)
    extra = {x.strip().replace(",", "") for x in a.extra.split(",") if x.strip()}

    seen = set()
    unknown = []
    for tok in extract_memo_numbers(memo):
        if tok in seen:
            continue
        seen.add(tok)
        if tok in pool_exact:
            hit_exact.append(tok)
        elif tok in pool_derived or tok.replace(",", "") in extra:
            hit_derived.append(tok)
        else:
            unknown.append(tok)

    covered = len(hit_exact) + len(hit_derived)
    pct = round(covered / len(seen) * 100, 1) if seen else 0.0
    if not a.quiet:
        print(f"备忘录数字总数(去重): {len(seen)}")
        print(f"  ① 直接命中快照: {len(hit_exact)}")
        print(f"  ② 命中推导值/白名单: {len(hit_derived)}")
        print(f"  ③ 待确认来源: {len(unknown)}  (可回溯率 {pct}%)")
        if unknown:
            print("\n待确认数字（逐条给出出处：官方财报 / 新闻链接 / 计算过程；给不出即删除）:")
            print("  " + ", ".join(unknown[:80]) + (" ..." if len(unknown) > 80 else ""))
    return 1 if (a.strict and unknown) else 0


if __name__ == "__main__":
    sys.exit(main())
