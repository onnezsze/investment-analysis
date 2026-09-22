#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
review_discipline.py — 交易纪律复盘（反事实 / 影子账本）

问题：置信度门槛 0.65、RR≥2.5 等纪律是不是太严格？
方法：对每一条"被门控挡下"的决策，用当时的价格与状态（ATR）构造一个影子交易：
      入场 = 决策时价格，止损 = 2xATR（与工作流一致），止盈 = 2.5x 止损距离（与 RR 门槛一致），
      然后用决策之后的 15m K 线逐根推演：先触发止损还是先触发止盈。
      最后按置信度分桶统计 —— 若低置信度桶的期望为负、高置信度桶为正，则门槛合理。
局限：历史只有数小时，多数影子交易尚未走完 → 同时报告 MAE/MFE 与浮动 R。
"""
import json
import os
import sys
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0"}
LEDGER = os.path.expanduser("~/crypto_snapshots/trades.jsonl")
RR = 2.5                                   # 与 RR 门槛一致


def k15(symbol, start_ms, limit=1000):
    u = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
         f"&interval=15m&startTime={start_ms}&limit={limit}")
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25) as r:
            return json.load(r)
    except Exception:
        return []


def atr_of(state_file):
    try:
        s = json.load(open(state_file))
        return float(s["trend_relative_bps"]["atr_pct_4h"])
    except Exception:
        return None


def from_iso(ts):
    import datetime
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main():
    rows = [json.loads(l) for l in open(LEDGER) if l.strip()]
    dec = [r for r in rows if r.get("event") is None and r.get("action") in ("long", "short")
           and r.get("price_at_decision") and r.get("ts_utc")]
    print(f"可推演决策 {len(dec)} 条（含方向与价格）\n")
    buckets = defaultdict(list)
    detail = []
    for d in dec:
        atr = atr_of(d.get("state_file") or "")
        if not atr:
            continue
        entry, side = float(d["price_at_decision"]), d["action"]
        stop_pct = 2 * atr / 100
        stop = entry * (1 - stop_pct) if side == "long" else entry * (1 + stop_pct)
        tp = entry * (1 + RR * stop_pct) if side == "long" else entry * (1 - RR * stop_pct)
        t0 = int(from_iso(d["ts_utc"]).timestamp() * 1000)
        bars = k15(d["symbol"], t0)
        if not bars:
            continue
        outcome, R, mae, mfe = None, None, 0.0, 0.0
        for b in bars:
            hi, lo, cl = float(b[2]), float(b[3]), float(b[4])
            if side == "long":
                mae = min(mae, (lo - entry) / (entry * stop_pct))
                mfe = max(mfe, (hi - entry) / (entry * stop_pct))
                if lo <= stop:
                    outcome, R = "止损", -1.0
                    break
                if hi >= tp:
                    outcome, R = "止盈", RR
                    break
            else:
                mae = min(mae, (entry - hi) / (entry * stop_pct))
                mfe = max(mfe, (entry - lo) / (entry * stop_pct))
                if hi >= stop:
                    outcome, R = "止损", -1.0
                    break
                if lo <= tp:
                    outcome, R = "止盈", RR
                    break
        if outcome is None:
            cl = float(bars[-1][4])
            R = ((cl - entry) if side == "long" else (entry - cl)) / (entry * stop_pct)
            outcome = "未走完"
        conf = float(d.get("confidence") or 0)
        b = "<0.35" if conf < 0.35 else "0.35-0.50" if conf < 0.50 else "0.50-0.65" if conf < 0.65 else "≥0.65"
        buckets[b].append({"R": R, "outcome": outcome, "mae": mae, "mfe": mfe, "conf": conf})
        detail.append({"symbol": d["symbol"], "side": side, "conf": conf, "R": R, "outcome": outcome,
                       "class": d.get("asset_class")})
    order = ["<0.35", "0.35-0.50", "0.50-0.65", "≥0.65"]
    print(f"{'置信度桶':10}{'笔数':>5}{'止损/止盈/未走完':>18}{'平均R':>8}{'中位R':>8}{'中位MAE':>9}{'中位MFE':>9}")
    print("-" * 70)
    for k in order:
        g = buckets.get(k)
        if not g:
            continue
        import statistics as st
        n = len(g)
        sc = sum(1 for x in g if x["outcome"] == "止损")
        tc = sum(1 for x in g if x["outcome"] == "止盈")
        rs = [x["R"] for x in g]
        print(f"{k:10}{n:>5}{f'{sc}/{tc}/{n-sc-tc}':>18}{st.mean(rs):>8.2f}"
              f"{st.median(rs):>8.2f}{st.median([x['mae'] for x in g]):>9.2f}"
              f"{st.median([x['mfe'] for x in g]):>9.2f}")
    allr = [x["R"] for g in buckets.values() for x in g]
    if allr:
        import statistics as st
        print("-" * 70)
        print(f"{'合计':10}{len(allr):>5}{'':>18}{st.mean(allr):>8.2f}{st.median(allr):>8.2f}")
        halted = sum(1 for x in allr if x == -1.0)
        print(f"\n止损命中 {halted}/{len(allr)} = {halted / len(allr) * 100:.1f}%"
              f"｜影子交易整体期望 {st.mean(allr):+.3f} R（RR={RR} 的盈亏平衡需 ≥ {1 / (1 + RR):.1%} 胜率）")
    json.dump(detail, open(os.path.expanduser("~/crypto_snapshots/shadow_book.json"), "w"),
              ensure_ascii=False, indent=1)
    print("\n[OK] 明细 → ~/crypto_snapshots/shadow_book.json")


if __name__ == "__main__":
    main()
