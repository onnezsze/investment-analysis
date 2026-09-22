#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ledger_report.py — 账本复盘：决策/执行/持仓管理 全景摘要（不依赖模型，纯事实）

统计口径：
  · 决策条数 = trades.jsonl 中的巡检决策记录（含被门控否决的）
  · 已执行   = executed=true 的建仓
  · 管理动作 = event=position_management 的记录，按 action 分类
  · 权益与持仓 = 实时从 Binance 读（钱包/可用/保证金/未实现盈亏/保护单）
用法: python3 ledger_report.py [--days 7]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.expanduser("~/crypto_snapshots/trades.jsonl")
DEC = os.path.expanduser("~/crypto_snapshots/decisions.jsonl")


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)

    rows = load(LEDGER)
    decs = load(DEC)
    recent = [r for r in rows if (r.get("ts_utc") or "") >= cutoff.isoformat()[:16]]

    decisions = [r for r in recent if r.get("event") is None and r.get("action")]
    mgmt = [r for r in recent if r.get("event") == "position_management"]
    prov = [r for r in recent if r.get("event") in ("provenance", "protective_order_fix")]
    executed = [r for r in recent if r.get("executed")]

    print(f"# 账本复盘（近 {a.days} 天，UTC）")
    print(f"> 生成时间 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"> 账本文件：{LEDGER}（{len(rows)} 条）｜ {DEC}（{len(decs)} 条）")
    print()
    print("## 一、决策层")
    print(f"- 巡检决策 **{len(decisions)}** 条 ｜ 实际建仓 **{len(executed)}** 笔 ｜ 被门控否决 "
          f"**{len(decisions) - len(executed)}** 条")
    if decisions:
        reasons = Counter()
        for r in decisions:
            v = r.get("verdict") or ""
            key = ("置信度不足" if "置信度" in v else "RR 不足" if "风险回报比" in v else
                   "模型给 no_trade" if "no_trade" in v else "其他")
            reasons[key] += 1
        print("- 否决原因分布：" + "；".join(f"{k} {v} 条" for k, v in reasons.most_common()))
        confs = [r.get("confidence") for r in decisions if isinstance(r.get("confidence"), (int, float))]
        if confs:
            print(f"- 置信度分布：最小 {min(confs)} ｜ 中位 {sorted(confs)[len(confs)//2]} ｜ 最大 {max(confs)}"
                  f"（门槛 0.65）")
        models = Counter(r.get("model") for r in decisions)
        print("- 使用模型：" + "；".join(f"{k} {v} 条" for k, v in models.items()))
        syms = Counter(r.get("symbol") for r in decisions)
        print("- 巡检覆盖标的：" + "；".join(f"{k} {v} 次" for k, v in syms.most_common()))

    print()
    print("## 二、执行层（真实成交）")
    if not executed:
        print("- 无")
    for r in executed:
        print(f"- {r['ts_utc'][:16]} **{r['symbol']}** {r.get('action')}｜置信度 {r.get('confidence')}"
              f"｜名义 ${r.get('planned_notional') or (r.get('sizing') or {}).get('notional_usd')}"
              f"｜止损 {((r.get('plan') or {}).get('stop'))}｜止盈 {((r.get('plan') or {}).get('tp1'))}"
              f"｜订单 {((r.get('order_result') or {}).get('orderId'))}")

    print()
    print("## 三、持仓管理（论点复核）")
    if not mgmt:
        print("- 无")
    else:
        c = Counter(r.get("action") for r in mgmt)
        print("- 动作分布：" + "；".join(f"{k} {v} 次" for k, v in c.most_common()))
        acts = [r for r in mgmt if r.get("action") in ("CLOSE", "TIGHTEN")]
        for r in acts:
            print(f"- {r['ts_utc'][:16]} **{r['symbol']}** {r.get('action')}｜{r.get('reason')}")
        last = {}
        for r in mgmt:
            last[r.get("symbol")] = r
        print("- 各标的最新读数：")
        for s, r in last.items():
            print(f"    {s}: 持仓方向概率 {r.get('p_pos')}｜置信度 {r.get('confidence')}｜动作 {r.get('action')}")

    print()
    print("## 四、账户现状（实时）")
    try:
        import binance_exec as bx
        acct = bx.account_state()
        print(f"- 钱包 **{float(acct['totalWalletBalance']):.4f}** USDT ｜ 可用 "
              f"{float(acct['availableBalance']):.4f} ｜ 未实现盈亏 {float(acct['totalUnrealizedProfit']):+.4f}")
        pos = [p for p in acct.get("positions", []) if float(p.get("positionAmt", 0)) != 0]
        for p in pos:
            print(f"- 持仓 {p['symbol']} {p['positionAmt']} @ {p['entryPrice']}｜杠杆 {p['leverage']}x"
                  f"｜未实现 {float(p['unrealizedProfit']):+.4f}")
        algos = bx.open_algo_orders()
        for o in algos:
            print(f"- 保护单 {o['symbol']} {o['orderType']} 触发价 {o['triggerPrice']} 状态 {o['algoStatus']}")
        if not pos:
            print("- 当前空仓")
    except Exception as e:                                          # noqa: BLE001
        print(f"- 读取账户失败：{type(e).__name__}: {str(e)[:80]}")

    print()
    print("## 五、样本充足度评估")
    n_closed = 0   # 尚无已了结交易（TP/SL 未触发）
    print(f"- 已了结交易（触发 TP/SL 或主动平仓）：{n_closed} 笔 → **胜率尚无法统计**")
    print("- 结论口径：在样本 <20 笔了结交易前，无法判断系统是否有正边际；此阶段任何仓位放大都缺乏证据支持。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
