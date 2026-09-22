#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
closed_trades.py — 了结交易胜率评估（达到样本门槛才输出，否则静默）

口径（全部用交易所真实数据，不靠估算）：
  · 了结事件 = Binance `/fapi/v1/income` 里 incomeType=REALIZED_PNL 且 income≠0 的记录，
    按标的 + 30 分钟时间间隔归并为「一笔了结」（避免一次平仓拆成多条 fill 记录）
  · 每笔了结的盈亏 = 该窗口内 REALIZED_PNL + COMMISSION + FUNDING_FEE（三者的净额）
  · R 倍数 = 净盈亏 / 开仓时记录的风险额（risk_usd，来自 autotrade 账本）
  · 盈亏平衡胜率：RR=2.5 时为 1/(1+2.5) = 28.6% —— 低于此值系统无正边际
  · 附带分档：按资产类别（crypto/tradfi）、按开仓置信度桶统计

静默规则：了结笔数 < --min（默认 20）→ stdout 为空（cron 不打扰）；
          达标后输出完整评估；并以 ~/crypto_snapshots/.winrate_last 记录上次汇报笔数，
          仅在笔数再增加 --step（默认 5）笔时再次汇报，避免每天重复同一条。
用法: python3 closed_trades.py [--min 20] [--step 5] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binance_exec as bx  # noqa: E402

LEDGER = os.path.expanduser("~/crypto_snapshots/trades.jsonl")
MARK = os.path.expanduser("~/crypto_snapshots/.winrate_last")
GAP_MS = 30 * 60 * 1000


def income_history(start_ms: int) -> list[dict]:
    out = []
    for t in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
        r = bx._req("GET", "/fapi/v1/income", {"incomeType": t, "startTime": start_ms, "limit": 1000})
        if isinstance(r, list):
            out += r
    return sorted(out, key=lambda x: int(x.get("time") or 0))


def load_entries() -> list[dict]:
    if not os.path.exists(LEDGER):
        return []
    rows = [json.loads(l) for l in open(LEDGER, encoding="utf-8") if l.strip()]
    return [r for r in rows if r.get("executed") and r.get("symbol")]


def episodes(inc: list[dict]) -> list[dict]:
    """把成交明细归并成「一笔了结」"""
    per: dict[str, list] = defaultdict(list)
    for x in inc:
        per[x.get("symbol")].append(x)
    out = []
    for sym, lst in per.items():
        cur = None
        for x in lst:
            t = int(x.get("time") or 0)
            amt = float(x.get("income") or 0)
            if cur and t - cur["end_ms"] <= GAP_MS:
                cur["end_ms"] = t
                cur["pnl"] += amt
                cur["n"] += 1
                if x["incomeType"] == "REALIZED_PNL":
                    cur["realized"] += amt
            else:
                if cur and cur["realized"] != 0:
                    out.append(cur)
                cur = {"symbol": sym, "start_ms": t, "end_ms": t, "pnl": amt, "realized": 0.0, "n": 1,
                       "first": datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")}
                if x["incomeType"] == "REALIZED_PNL":
                    cur["realized"] = amt
        if cur and cur["realized"] != 0:
            out.append(cur)
    return sorted(out, key=lambda x: x["start_ms"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=20, help="达到多少笔了结才评估")
    ap.add_argument("--step", type=int, default=5, help="之后每增加多少笔再次评估")
    ap.add_argument("--force", action="store_true", help="无视样本门槛强制输出（调试用）")
    a = ap.parse_args()

    entries = load_entries()
    start_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 30 * 24 * 3600 * 1000
    if entries:
        try:
            start_ms = min(int(datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00")).timestamp() * 1000)
                           for e in entries) - 3600_000
        except Exception:                                       # noqa: BLE001
            pass
    inc = income_history(start_ms)
    eps = episodes(inc)
    n = len(eps)

    if n < a.min and not a.force:
        return 0                                                # 静默：样本未达标

    last = 0
    if os.path.exists(MARK):
        try:
            last = int(open(MARK).read().strip() or 0)
        except Exception:                                       # noqa: BLE001
            last = 0
    if not a.force and last and n < last + a.step:
        return 0                                                # 静默：距上次汇报增量不足
    open(MARK, "w").write(str(n))

    # 匹配开仓记录（用于 R 倍数、置信度分档、资产类别）
    def find_entry(sym: str, t_ms: int) -> dict | None:
        cands = [e for e in entries if e.get("symbol") == sym]
        best = None
        for e in cands:
            et = datetime.fromisoformat(e["ts_utc"].replace("Z", "+00:00")).timestamp() * 1000
            if et <= t_ms + GAP_MS and (best is None or et > best[0]):
                best = (et, e)
        return best[1] if best else None

    rows = []
    for ep in eps:
        e = find_entry(ep["symbol"], ep["start_ms"])
        risk = (e or {}).get("risk_usd")
        if not risk and e:
            s = e.get("sizing") or {}
            if s.get("notional_usd") and s.get("stop_distance_pct"):
                risk = s["notional_usd"] * s["stop_distance_pct"] / 100
        rows.append({"symbol": ep["symbol"], "time": ep["first"], "pnl": round(ep["pnl"], 4),
                     "risk": round(risk, 4) if risk else None,
                     "R": round(ep["pnl"] / risk, 2) if risk else None,
                     "win": ep["pnl"] > 0,
                     "class": (e or {}).get("asset_class"),
                     "conf": (e or {}).get("confidence")})

    wins = [r for r in rows if r["win"]]
    wr = len(wins) / len(rows) * 100
    tot = sum(r["pnl"] for r in rows)
    rs = [r["R"] for r in rows if r["R"] is not None]
    exp_r = sum(rs) / len(rs) if rs else None

    print(f"# 了结交易胜率评估（样本 {n} 笔）")
    print(f"> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}｜数据来源：Binance "
          f"`/fapi/v1/income`（REALIZED_PNL + COMMISSION + FUNDING_FEE 净额）")
    print()
    print(f"- **胜率 {wr:.1f}%**（{len(wins)}/{len(rows)}）｜ 合计净盈亏 **{tot:+.4f} USDT**"
          + (f"｜ 期望 **{exp_r:+.2f} R**/笔" if exp_r is not None else ""))
    print(f"- 盈亏平衡胜率（RR=2.5）约 **28.6%** → " +
          ("**高于平衡线，具备正边际的初步证据**" if wr > 28.6 else "**低于平衡线，当前无正边际证据**"))
    sides = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        k = r["class"] or "unknown"
        sides[k][0] += 1
        sides[k][1] += 1 if r["win"] else 0
        sides[k][2] += r["pnl"]
    print("- 分资产类别：" + "；".join(
        f"{k} {v[0]} 笔 胜率 {v[1]/v[0]*100:.0f}% 净额 {v[2]:+.4f}" for k, v in sides.items()))
    cb = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        c = r["conf"]
        k = "未知" if c is None else ("<0.65" if c < 0.65 else "0.65-0.85" if c < 0.85 else ">=0.85")
        cb[k][0] += 1
        cb[k][1] += 1 if r["win"] else 0
        cb[k][2] += r["pnl"]
    print("- 分置信度桶（验证门槛是否有效）：" + "；".join(
        f"{k} {v[0]} 笔 胜率 {v[1]/v[0]*100:.0f}% 净额 {v[2]:+.4f}" for k, v in sorted(cb.items())))
    print()
    print("| 标的 | 了结时间 | 净盈亏 USDT | 风险额 | R 倍数 |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['symbol']} | {r['time']} | {r['pnl']:+.4f} | {r['risk'] or '—'} | {r['R'] or '—'} |")
    print()
    print(f"判据：RR≥2.5 下胜率需 ≥28.6% 才有继续价值；样本 <50 笔时结论仅供参考。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
