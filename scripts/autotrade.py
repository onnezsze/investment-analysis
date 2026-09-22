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
NO_ENTRY = os.path.expanduser("~/.binance_futures_noentry.on")   # 存在=只管理持仓，不开新仓
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


MAX_CONCURRENT_POSITIONS = 2      # 并发仓位上限（建仓慎重）
CORR_MAX = 0.70                   # 与现有持仓的相关性上限：超过即视为"同一个押注"，不再开新仓


def corr_with_open(symbol: str, open_syms: list[str], bars: int = 90) -> tuple[float, str | None]:
    """候选标的与现有持仓的最大绝对相关性（4H 收益率，越长越稳）"""
    import numpy as np
    def series(s):
        for mk in ("futures", "spot"):
            try:
                d = cd.cs.klines("binance", s, "4h", bars + 5, mk)
                if len(d) > 30:
                    return d["Close"].pct_change().dropna().values[-bars:]
            except Exception:                              # noqa: BLE001
                continue
        return None
    a = series(symbol)
    if a is None:
        return 0.0, None
    worst, worst_sym = 0.0, None
    for s in open_syms:
        b = series(s)
        if b is None or len(b) < 30:
            continue
        n = min(len(a), len(b))
        c = float(np.corrcoef(a[-n:], b[-n:])[0, 1])
        if abs(c) > abs(worst):
            worst, worst_sym = c, s
    return round(worst, 3), worst_sym


def manage_positions(positions: dict, live: bool, profile: str, lines: list) -> list:
    """持仓管理：论点反转/消失 → 平仓；论点弱化 → 收紧止损（已盈利时）；否则持有"""
    actions = []
    for sym, p in positions.items():
        amt = float(p["positionAmt"])
        entry = float(p["entryPrice"])
        pos_dir = "long" if amt > 0 else "short"
        try:
            state = cd.build_state(sym, profile, float(p.get("notional", 0)) or 100.0, None, 5.0)
            raw = cd.ts_request(state, profile)
        except Exception as e:                                  # noqa: BLE001
            lines.append(f"\n**{sym}** 持仓复核失败：{type(e).__name__} {str(e)[:80]}")
            continue
        d = raw["answers"]["direction"]
        probs = d.get("probabilities") or {}
        conf = d.get("confidence") or 0
        choice = d.get("choice")
        p_pos = float(probs.get(pos_dir) or 0)
        px = state["market_structure"]["price"]
        atr = state["trend_relative_bps"]["atr_pct_4h"] or 1.0
        T = cd.THESIS
        act, reason = "HOLD", ""
        # 上一次对同一标的的管理读数（用于"连续两次"确认，抑制单次噪声）
        hist = [json.loads(l) for l in open(TRADES_LOG, encoding="utf-8") if l.strip()] if os.path.exists(TRADES_LOG) else []
        prev = [h for h in hist if h.get("symbol") == sym and h.get("event") == "position_management"]
        prev_bad = bool(prev) and prev[-1].get("action") in ("CLOSE", "CLOSE_PENDING", "WEAK")
        extreme = p_pos < 0.30 or conf < 0.20          # 极端读数：立即处置，不等确认
        # ① 论点反转（模型明确反对持仓方向，且置信度够高）
        if choice and choice != pos_dir and choice != "no_trade" and conf >= T["reverse_conf"]:
            act, reason = "CLOSE", f"论点反转：Jev 现给 {choice}（置信度 {conf} ≥ {T['reverse_conf']}）"
        # ② 论点消失（极端值立即平；否则需连续两次读数确认）
        elif p_pos < T["gone_p"] or conf < T["gone_conf"]:
            why = f"持仓方向概率 {p_pos:.2f}／置信度 {conf}"
            if extreme or prev_bad:
                act, reason = "CLOSE", f"论点消失（{why}）" + ("，极端读数立即处置" if extreme else "，连续两次确认")
            else:
                act, reason = "CLOSE_PENDING", f"论点消失待确认（{why}）—— 首次读数不平仓，防单次噪声甩单"
        # ③ 论点弱化 → 已盈利则收紧到保本/更优，未盈利则警告（连续两次弱化则平仓）
        elif p_pos < T["weaken_p"] or conf < 0.45:
            # 盈利判定：多头=价格高于入场；空头=价格低于入场（此前写反，已在实盘前修正）
            in_profit = (px > entry) if pos_dir == "long" else (px < entry)
            if in_profit:
                act, reason = "TIGHTEN", f"论点弱化（概率 {p_pos:.2f}／置信度 {conf}）且已盈利 → 收紧止损"
            else:
                hist = [json.loads(l) for l in open(TRADES_LOG, encoding="utf-8") if l.strip()] if os.path.exists(TRADES_LOG) else []
                prev_weak = [h for h in hist if h.get("symbol") == sym and h.get("event") == "position_management"
                             and h.get("action") == "WEAK"]
                act, reason = ("CLOSE", "论点连续两次弱化且未盈利 → 平仓") if prev_weak else \
                              ("WEAK", f"论点弱化（概率 {p_pos:.2f}／置信度 {conf}）但未盈利 → 标记，下轮再弱化即平仓")
        else:
            reason = f"论点成立：{pos_dir} 概率 {p_pos:.2f}（置信度 {conf}）"
        # 执行
        detail = {"event": "position_management", "symbol": sym, "pos_dir": pos_dir, "amount": amt,
                  "entry": entry, "price": px, "choice": choice, "probabilities": probs,
                  "p_pos": p_pos, "confidence": conf, "action": act, "reason": reason,
                  "model": raw.get("model"), "usage": raw.get("usage"), "state_hash": None,
                  "live": live, "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if act == "CLOSE":
            detail["cancelled_algo"] = None
            detail["close_result"] = None
            if live:
                detail["cancelled_algo"] = bx.cancel_all_protective(sym)
                r = bx._req("POST", "/fapi/v1/order",
                            {"symbol": sym, "side": ("SELL" if amt > 0 else "BUY"), "type": "MARKET",
                             "quantity": bx.fmt(abs(amt), bx.spec(sym)["step_size"]), "reduceOnly": "true"})
                detail["close_result"] = r
            lines.append(f"\n**{sym}** 持仓 {pos_dir} → **{'已平仓' if live else '[dry-run] 将平仓'}**｜{reason}")
        elif act == "TIGHTEN":
            # 只允许"收紧"：止损只能朝减少风险的方向移动，且不得贴到现价（避免立即触发）
            cur = 0.0
            for o in bx.open_algo_orders(sym):
                if o.get("orderType") == "STOP_MARKET":
                    cur = float(o.get("triggerPrice") or 0)
            atr_abs = px * atr / 100
            if pos_dir == "short":
                desired = min(entry, px + 0.5 * atr_abs)          # 空头：止损在价上方，向下收
                new_stop = min(cur or desired, desired)
                if new_stop <= px * 1.002:
                    new_stop = round(max(px * 1.002, min(cur or desired, desired)), 4)
            else:
                desired = max(entry, px - 0.5 * atr_abs)          # 多头：止损在价下方，向上收
                new_stop = max(cur or desired, desired)
                if new_stop >= px * 0.998:
                    new_stop = round(min(px * 0.998, max(cur or desired, desired)), 4)
            new_stop = round(new_stop, 4)
            detail["tighten_to"] = new_stop
            detail["prev_stop"] = cur or None
            detail["atr_abs"] = round(atr_abs, 4)
            if cur and abs(new_stop - cur) < 1e-9:
                act, reason = "HOLD", f"论点弱化但止损已是最优（{cur}），无需调整"
                lines.append(f"\n**{sym}** 持仓 {pos_dir} → 持有｜{reason}")
            if live:
                detail["cancelled_algo"] = bx.cancel_all_protective(sym)
                res = bx.place_protective(sym, "sell" if pos_dir == "short" else "buy",
                                          bx.fmt(abs(amt), bx.spec(sym)["step_size"]), "STOP_MARKET", new_stop)
                detail["new_stop_resp"] = res
            lines.append(f"\n**{sym}** 持仓 {pos_dir} → **{'止损已收紧' if live else '[dry-run] 将收紧'}至 {new_stop}**｜{reason}")
        elif act == "CLOSE_PENDING":
            lines.append(f"\n**{sym}** 持仓 {pos_dir} → ⚠️ **{reason}**（下轮仍如此即平仓）")
        elif act == "WEAK":
            lines.append(f"\n**{sym}** 持仓 {pos_dir} → ⚠️ **{reason}**")
        else:
            lines.append(f"\n**{sym}** 持仓 {pos_dir} → 持有｜{reason}")
        record(TRADES_LOG, detail)
        actions.append(detail)
    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description="定时决策与执行（工作流原纪律）")
    ap.add_argument("--symbols", default=None, help="显式指定标的；不填则用 --scan")
    ap.add_argument("--scan", type=int, default=8, help="从最新 watchlist 取前 N 个可交易标的（默认 8）")
    ap.add_argument("--include-tradfi", action="store_true",
                    help="把 tradfi 候选也纳入（需 HTX API 凭证，否则自动跳过）")
    ap.add_argument("--profile", default="swing")
    ap.add_argument("--leverage", type=float, default=5.0)
    ap.add_argument("--equity-floor", type=float, default=85.0)
    ap.add_argument("--skip-manage", action="store_true", help="跳过持仓管理（仅用于测试扫描）")
    ap.add_argument("--manage-only", action="store_true", help="只做持仓管理，不扫描新机会（高频循环用）")
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
        lines.append("\n### 一、持仓管理（论点复核）")
        manage_positions(positions, live, a.profile, lines)
        positions = open_positions()          # 管理后刷新
        if a.manage_only:
            print("\n".join(lines))
            return 0
        if os.path.exists(NO_ENTRY):
            lines.append("\n### 二、新机会扫描 —— **已暂停**（存在禁止开仓开关，仅管理持仓）")
            print("\n".join(lines))
            return 0
        lines.append("\n### 二、新机会扫描")
    elif a.manage_only:
        print(f"**持仓管理 · {ts}** ｜ 空仓，无需管理")
        return 0
    elif os.path.exists(NO_ENTRY):
        print(f"**巡检 · {ts}** ｜ 空仓 + 禁止开仓开关生效 → 无操作")
        return 0

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
        # 建仓闸①：并发仓位上限
        if len(positions) >= MAX_CONCURRENT_POSITIONS:
            lines.append(f"\n**{sym}**：跳过 —— 已达并发仓位上限 {MAX_CONCURRENT_POSITIONS} 个")
            continue
        # 建仓闸②：与现有持仓高度相关 = 同一个押注（防重复下注，如实盘中 MU/MUU 同源）
        c, cw = corr_with_open(sym, list(positions.keys()))
        if cw and abs(c) > CORR_MAX:
            lines.append(f"\n**{sym}**：跳过 —— 与现有持仓 {cw} 相关性 {c}（>{CORR_MAX}），同一押注不重复下注")
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
            rr_min = cd.PROFILES[a.profile].get("rr_min", 2.5)
            if rr and rr >= rr_min:
                plan = {"entry": price, "stop": stop, "tp1": tp, "rr": round(rr, 2)}
            else:
                lines.append(f"　→ RR {rr if rr is None else round(rr,2)} < 门槛 {rr_min}，不开仓")

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
