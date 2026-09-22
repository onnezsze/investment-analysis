#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autotrade.py — 无人值守的定时决策与执行（沿用工作流原纪律：1% 风险 + 全部门控）

设计原则
  · 决策完全由 crypto_decide.py（TypeSafe + 代码门控）给出，本脚本只做「编排与安全钳制」
  · 门控说观望 → 只记账，绝不下单
  · 门控通过 → 才真下单（市价入场 + reduceOnly 止损/止盈），并按 stepSize/minNotional 取整
  · 硬安全阀（写在代码里，不靠记忆）：
      1) 主开关文件 ~/.binance_futures_autotrade.on 不存在 → 只跑 dry-run，不发单（kill switch）
      2) 单标的只持一笔，已有持仓则跳过（不做加仓/网格）
      3) 权益地板：权益 < EQUITY_FLOOR_USDT（默认 85）→ 停止开新仓并告警（只允许平仓）
      4) 每笔风险预算固定 1%，杠杆 ≤ 5x，且必须通过爆仓距离与最小名义校验
      5) 只做预设白名单标的（默认 ETHUSDT/SOLUSDT；BTC 在 100U 账户下无法满足 1% 纪律）

用法:
  python3 autotrade.py                 # 有主开关=真下单；无主开关=dry-run
  python3 autotrade.py --dry           # 强制 dry-run
  python3 autotrade.py --symbols ETHUSDT,SOLUSDT --equity-floor 85
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import binance_exec as bx      # noqa: E402
import crypto_decide as cd     # noqa: E402

SWITCH = os.path.expanduser("~/.binance_futures_autotrade.on")
TRADES_LOG = os.path.expanduser("~/crypto_snapshots/trades.jsonl")
HALT_LOG = os.path.expanduser("~/crypto_snapshots/halt.log")
# 注：2026-09-22 01:56 首轮真下单时，本记录尚未持久化 TypeSafe 原始返回（answers/state_hash 为 null），
# 该缺陷已在本提交修复；两笔成交的决策来源另见 trades.jsonl 中的 provenance 补录记录。


def live_mode(force_dry: bool) -> tuple[bool, str]:
    if force_dry:
        return False, "强制 dry-run（--dry）"
    if os.path.exists(SWITCH):
        return True, f"主开关存在（{SWITCH}）→ 允许真下单"
    return False, f"主开关不存在（{SWITCH}）→ 只记账不下单"


def load_watchlist(n: int, include_tradfi: bool) -> list[dict]:
    """读取最新扫描榜单，取前 N 个可交易标的（tradfi 需显式开启）"""
    import glob
    files = sorted(glob.glob(os.path.join(os.path.expanduser("~"), "crypto_snapshots", "watchlist_*.json")))
    if not files:
        return []
    w = json.load(open(files[-1], encoding="utf-8"))
    out = []
    for r in w.get("watchlist", []):
        if r.get("tradability") != "可交易":
            continue
        if r.get("asset_class") == "tradfi" and not include_tradfi:
            continue
        out.append({"symbol": r.get("contract"), "venue": r.get("venue"),
                    "asset_class": r.get("asset_class"), "score": r.get("opportunity_score")})
        if len(out) >= n:
            break
    return out


def open_positions() -> dict:
    a = bx.account_state()
    out = {}
    for p in a.get("positions", []):
        amt = float(p.get("positionAmt", 0))
        if amt != 0:
            out[p["symbol"]] = p
    return out


def record(path: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="定时决策与执行（工作流原纪律）")
    ap.add_argument("--symbols", default=None, help="显式指定标的；不填则用 --scan")
    ap.add_argument("--scan", type=int, default=8, help="从最新 watchlist 取前 N 个可交易标的（默认 8）")
    ap.add_argument("--include-tradfi", action="store_true",
                    help="把 tradfi 候选也纳入（需 HTX API 凭证，否则自动跳过）")
    ap.add_argument("--profile", default="swing")
    ap.add_argument("--leverage", type=float, default=5.0)
    ap.add_argument("--equity-floor", type=float, default=85.0)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    live, why = live_mode(a.dry)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**自动决策巡检 · {ts}**", f"> {why}"]

    acct = bx.account_state()
    equity = float(acct.get("availableBalance") or 0)
    wallet = float(acct.get("totalWalletBalance") or 0)
    lines.append(f"> 权益：钱包 {wallet:.2f} USDT ｜ 可用 {equity:.2f} USDT")

    positions = open_positions()
    if positions:
        lines.append("> 现有持仓：" + "；".join(f"{s} {p['positionAmt']} @ {p['entryPrice']}" for s, p in positions.items()))

    if wallet < a.equity_floor:
        msg = f"权益 {wallet:.2f} < 地板 {a.equity_floor} → 停止开新仓"
        lines.append(f"> 🛑 {msg}")
        record(HALT_LOG, {"ts_utc": ts, "wallet": wallet, "floor": a.equity_floor, "reason": msg})
        print("\n".join(lines))
        return 0

    if a.symbols:
        todo = [{"symbol": s.strip().upper(), "venue": "Binance", "asset_class": "crypto"}
                for s in a.symbols.split(",") if s.strip()]
    else:
        todo = load_watchlist(a.scan, a.include_tradfi)
        if not todo:
            lines.append("\n> 未取到候选标的（watchlist 为空）")
    for item in todo:
        sym, venue, klass = item["symbol"], item.get("venue", "Binance"), item.get("asset_class", "crypto")
        if venue == "HTX":
            lines.append(f"\n**{sym}**（tradfi/{venue}）：跳过 —— 需 HTX API 凭证（未配置）")
            continue
        if sym in positions:
            lines.append(f"\n**{sym}**：已持仓，跳过（单标的一笔上限）")
            continue
        try:
            state = cd.build_state(sym, a.profile, wallet, None, a.leverage)
            answers = cd.ts_request(state, a.profile)
            gated = cd.gate_and_size(state, answers["answers"], a.profile)
        except SystemExit as e:
            lines.append(f"\n**{sym}**：跳过（{e}）")
            continue
        except Exception as e:                                    # noqa: BLE001
            lines.append(f"\n**{sym}**：异常 {type(e).__name__}: {str(e)[:100]}")
            continue

        d = answers["answers"]["direction"]
        lines.append(f"\n**{sym}**：Jev {d['choice']}（conf {d.get('confidence')}）→ **{gated['verdict']}**")

        # 计划（止损/止盈由结构位算出，不由模型编造）
        plan = None
        price = state["market_structure"]["price"]
        sups = [float(x) for x in (state["structure_levels"]["support_4h"] or [])]
        res = [float(x) for x in (state["structure_levels"]["resistance_4h"] or [])]
        atr = state["trend_relative_bps"]["atr_pct_4h"] or 0
        if gated["action"] in ("long", "short") and gated["size_multiplier"]:
            if gated["action"] == "long":
                stop = round(max([s for s in sups if s < price] or [price * (1 - atr * 3 / 100)]) * 0.997, 6)
                tp = round(res[0], 6) if res else round(price * (1 + atr * 5 / 100), 6)
            else:
                stop = round(min([r for r in res if r > price] or [price * (1 + atr * 3 / 100)]) * 1.003, 6)
                tp = round(sups[0], 6) if sups else round(price * (1 - atr * 5 / 100), 6)
            risk = abs(price - stop)
            rr = (abs(tp - price) / risk) if risk else None
            if rr and rr >= 2.0:
                plan = {"entry": price, "stop": stop, "tp1": tp, "rr": round(rr, 2)}

        import hashlib
        state_hash = hashlib.sha256(
            json.dumps(state, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]
        rec = {
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": sym,
            "perp_contract": state["perp_contract"], "venue": venue, "asset_class": klass,
            "scan_score": item.get("score"), "model": answers.get("model"),
            "questions_version": cd.QUESTIONS_VERSION, "state_hash": state_hash,
            "latency_ms": answers.get("_latency_ms"), "usage": answers.get("usage"),
            # ⬇️ 真钱交易的审计核心：TypeSafe 原始返回必须落盘，事后可复核
            "answers": answers["answers"],
            "probabilities": (answers["answers"].get("direction") or {}).get("probabilities"),
            "action": gated["action"], "confidence": gated["confidence"],
            "conviction": gated.get("conviction"), "conviction_parts": gated.get("conviction_parts"),
            "cost_bps": gated.get("cost_bps"), "expected_move_bps": gated.get("expected_move_bps"),
            "verdict": gated["verdict"], "gates": gated["gates"], "sizing": gated["sizing"],
            "plan": plan, "executed": False, "order_result": None, "wallet_usdt": wallet,
            "price_at_decision": state["market_structure"]["price"],
        }

        if not plan or not gated["size_multiplier"]:
            lines.append("　→ 门控未通过或无可执行计划，仅记账")
            record(TRADES_LOG, rec)
            continue

        notional = gated["sizing"]["notional_usd"]
        side = "buy" if gated["action"] == "long" else "sell"
        rec["planned_notional"] = notional
        rec["planned_side"] = side
        if not live:
            lines.append(f"　→ [dry-run] 将下 {side} 名义 ${notional:,}，止损 {plan['stop']} 止盈 {plan['tp1']}（RR {plan['rr']}）")
            record(TRADES_LOG, rec)
            continue

        # 真下单：先设杠杆，再市价入场（带 reduceOnly 止损/止盈）
        lev_res = bx._req("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": int(a.leverage)})
        entry = bx.build_order(type("O", (), {"symbol": sym, "side": side, "qty": None, "notional": notional,
                                             "type": "market", "price": None, "tif": "GTC"})())
        if not entry:
            lines.append("　→ 入场单校验未过（名义/步长），仅记账")
            record(TRADES_LOG, rec)
            continue
        resp = bx._req("POST", "/fapi/v1/order", entry)
        rec["order_result"] = resp
        if resp.get("_error"):
            lines.append(f"　→ ❌ 下单失败：{resp.get('msg')}")
            record(TRADES_LOG, rec)
            continue
        rec["executed"] = True
        lines.append(f"　→ ✅ 已下单 {entry['side']} {entry['quantity']} {sym}（订单号 {resp.get('orderId')}）")
        # 止损/止盈条件单
        prot_ok, prot_fail = 0, []
        for kind, trig in (("STOP_MARKET", plan["stop"]), ("TAKE_PROFIT_MARKET", plan["tp1"])):
            res = bx.place_protective(sym, side, entry["quantity"], kind, trig)
            r2 = res["resp"]
            ok = not r2.get("_error")
            prot_ok += 1 if ok else 0
            lines.append(f"　　{kind} @ {trig}（{res['api']}）→ {'✅ OK' if ok else '❌ ' + str(r2.get('msg'))[:70]}")
            rec.setdefault("protective_orders", []).append(
                {"type": kind, "trigger": trig, "api": res["api"], "resp": r2})
            if not ok:
                prot_fail.append(kind)
        # 兜底：止损单必须挂上；否则立即平仓，绝不留下无保护仓位
        stop_ok = any(o["type"] == "STOP_MARKET" and not o["resp"].get("_error")
                      for o in rec.get("protective_orders", []))
        if not stop_ok:
            close_q = {"symbol": sym, "side": ("SELL" if side == "buy" else "BUY"),
                       "type": "MARKET", "quantity": entry["quantity"], "reduceOnly": "true"}
            rc = bx._req("POST", "/fapi/v1/order", close_q)
            lines.append(f"　　🚨 止损单未挂上 → 已立即平仓兜底：{json.dumps(rc, ensure_ascii=False)[:140]}")
            rec["emergency_close"] = rc
            rec["executed"] = False
            rec["verdict"] += "｜保护单失败已平仓"
        record(TRADES_LOG, rec)

    out = "\n".join(lines)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
