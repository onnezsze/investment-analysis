#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_decide.py — 单次交易决策引擎（融合 jarrodwatts/jev-trader 的 TypeSafe 范式）

借鉴来源：https://github.com/jarrodwatts/jev-trader
  · buildState()      → 代码构建「紧凑、相对化、可读」的市场状态（bps 收益 / 盘口失衡 /
                        深度分档 / CVD / 近期成交 / allowed 可执行标记），模型只做判断
  · 结构化 instructions {question, goal, timing, inputs} + criteria 写明成本阈值
  · 一次请求并行问多个原子问题（composite scoring），权重与归一化在代码里
  · 置信度门控（confidence-gated routing）：动作风险越高要求置信度越高，不足则观望
  · 代码拥有执行：模型给意图，代码做仓位/杠杆/强平距离/流动性上限的钳制
  · 决策账本（data/events.jsonl 的同构做法）→ decisions.jsonl，可回填结果、统计胜率

与备忘录流程的分工：
  · crypto_snapshot.py  → 事实层（真实数据快照 + 摘要仪表盘）
  · crypto_decide.py    → 决策层（一次 TypeSafe 请求 → 门控 → 仓位 → 记账）
  · 备忘录（references/crypto.md）→ 叙事层（把事实与决策写成可审计的报告）

用法:
  python3 crypto_decide.py BTCUSDT --equity 10000                      # 真实 TypeSafe 决策
  python3 crypto_decide.py BTCUSDT --model mock                        # 无 API 的启发式替身（管线自测）
  python3 crypto_decide.py BTCUSDT --profile scalp --horizon-hours 6   # 短打档
  python3 crypto_decide.py --resolve                                   # 回填历史决策的结果并统计
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics as st
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto_snapshot as cs  # noqa: E402  复用事实层
try:                                                              # 交易所规格（存在凭证时）
    import binance_exec as bx                                      # noqa: E402
except Exception:                                                 # noqa: BLE001
    bx = None


def venue_constraints(symbol: str) -> dict:
    """该合约的真实可执行约束：最小名义/步长/一档最大杠杆/维持保证金率（账户层）"""
    if bx is None:
        return {}
    try:
        sp = bx.spec(symbol)
        acct = bx.account_state()
        return {
            "min_notional_usdt": sp["min_notional"], "step_size": sp["step_size"],
            "tick_size": sp["tick_size"], "venue_max_leverage_x": sp["max_leverage"],
            "maint_margin_ratio": sp["maint_margin_ratio"],
            "account_available_usdt": float(acct.get("availableBalance") or 0),
            "account_wallet_usdt": float(acct.get("totalWalletBalance") or 0),
        }
    except Exception as e:                                        # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"[:80]}

LEDGER = os.path.join(os.path.expanduser("~"), "crypto_snapshots", "decisions.jsonl")
QUESTIONS_VERSION = "monad-style-v1"


# ───────────────────────── 状态构建（借 buildState 的思路） ─────────────────────────

def _returns_bps(closes: list[float], k: int, bars_ago: int = 0) -> float | None:
    """k 根 K 线前的收益率（bps）。bars_ago 用于取更早的截面。"""
    i = len(closes) - 1 - bars_ago
    if i - k < 0:
        return None
    return round((closes[i] - closes[i - k]) / closes[i - k] * 10000, 2)


def book_state(symbol: str, px: float, market: str = "futures") -> dict:
    """盘口：失衡 / 深度分档 / 顶部档位 / 计划名义的滑点（都是「相对」量，不是绝对价格）"""
    base = cs.BIN_FUT if market == "futures" else cs.BIN_SPOT
    path = "/fapi/v1/depth" if market == "futures" else "/api/v3/depth"
    d = cs.soft(lambda: cs.get(f"{base}{path}?symbol={symbol}&limit=500")) or {}
    bids = [(float(p), float(q)) for p, q in d.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
    if not bids or not asks:
        return {}
    out = {"best_bid": bids[0][0], "best_ask": asks[0][0],
           "spread_bps": round((asks[0][0] - bids[0][0]) / ((asks[0][0] + bids[0][0]) / 2) * 10000, 2),
           "levels": {"bids": [f"{p:g} x {q:g}" for p, q in bids[:5]],
                      "asks": [f"{p:g} x {q:g}" for p, q in asks[:5]]}}
    bands = {}
    for bps in (5, 10, 25, 50):
        lim_b, lim_a = px * (1 - bps / 10000), px * (1 + bps / 10000)
        vb = sum(p * q for p, q in bids if p >= lim_b)
        va = sum(p * q for p, q in asks if p <= lim_a)
        bands[f"{bps}bps"] = {"bid_usd": round(vb, 0), "ask_usd": round(va, 0)}
    out["depth_bands"] = bands
    tot_b = bands["10bps"]["bid_usd"]
    tot_a = bands["10bps"]["ask_usd"]
    out["imbalance_10bps"] = round((tot_b - tot_a) / (tot_b + tot_a), 3) if (tot_b + tot_a) else None
    return out


def flow_state(symbol: str, minutes: int = 30) -> dict:
    """主动成交流：CVD（主动买−主动卖）、VWAP、最近成交（借 jev-trader 的 trades 摘要）"""
    cur = None
    for mk, base in (("futures", cs.BIN_FUT), ("spot", cs.BIN_SPOT)):
        path = "/fapi/v1/aggTrades" if mk == "futures" else "/api/v3/aggTrades"
        r = cs.soft(lambda b=base, p=path: cs.get(f"{b}{p}?symbol={symbol}&limit=1000"))
        if isinstance(r, list) and r:
            cur = (mk, r)
            break
    if not cur:
        return {}
    mk, trades = cur
    now_ms = trades[-1].get("T") or int(time.time() * 1000)
    cut = now_ms - minutes * 60_000
    sel = [t for t in trades if (t.get("T") or 0) >= cut] or trades[-200:]
    buy = sum(float(t["p"]) * float(t["q"]) for t in sel if not t.get("m"))
    sell = sum(float(t["p"]) * float(t["q"]) for t in sel if t.get("m"))
    vol = sum(float(t["p"]) * float(t["q"]) for t in sel)
    all_qty = sum(float(t["q"]) for t in sel)
    vwap = (vol / all_qty) if all_qty else None
    return {
        "market": mk, "window_minutes": minutes, "count": len(sel),
        "buy_usd": round(buy, 0), "sell_usd": round(sell, 0),
        "cvd_usd": round(buy - sell, 0),
        "cvd_to_volume": round((buy - sell) / vol, 3) if vol else None,
        "vwap": round(vwap, 6) if vwap else None,
        "last_side": ("sell" if sel[-1].get("m") else "buy") if sel else None,
        "recent_trades": [f"{datetime.fromtimestamp((t.get('T') or 0)/1000, timezone.utc).strftime('%H:%M:%S')} "
                          f"{'sell' if t.get('m') else 'buy'} {float(t['q']):g} @ {float(t['p']):g}"
                          for t in sel[-8:]],
    }


def cost_state(symbol: str, px: float, notional: float, horizon_hours: float,
               funding_annual_pct: float | None, book: dict, fee_bps_side: float = 5.0) -> dict:
    """总成本（bps）：往返手续费 + 预计滑点 + 资金费率的持有成本 —— 模型的判断必须跨过它"""
    slip = None
    base = cs.BIN_FUT
    d = cs.soft(lambda: cs.get(f"{base}/api/v3/depth?symbol={symbol}&limit=500"))
    d = cs.soft(lambda: cs.get(f"{base}/fapi/v1/depth?symbol={symbol}&limit=500")) or d
    if d:
        asks = [(float(p), float(q)) for p, q in d.get("asks", [])]
        bids = [(float(p), float(q)) for p, q in d.get("bids", [])]

        def walk(side, notional):
            book = asks if side == "buy" else bids
            remaining, qty, cost = notional, 0.0, 0.0
            for p, q in book:
                take = min(p * q, remaining)
                cost += take
                qty += take / p
                remaining -= take
                if remaining <= 1e-9:
                    break
            if qty <= 0 or remaining > 0:
                return None
            ref = asks[0][0] if side == "buy" else bids[0][0]
            return (cost / qty / ref - 1) * 10000 * (1 if side == "buy" else -1)
        eb, es = walk("buy", notional), walk("sell", notional)
        slip = {"entry_slippage_bps": round(eb, 2) if eb is not None else None,
                "exit_slippage_bps": round(es, 2) if es is not None else None,
                "fillable": eb is not None and es is not None}
    carry_bps = (funding_annual_pct or 0) / 365 * horizon_hours / 100 * 100 if funding_annual_pct is not None else 0.0
    fees = fee_bps_side * 2
    slip_bps = ((slip or {}).get("entry_slippage_bps") or 0) + abs((slip or {}).get("exit_slippage_bps") or 0) if slip else None
    total = fees + (slip_bps or 0) + abs(carry_bps)
    return {
        "fee_bps_roundtrip": fees, "slippage_bps_roundtrip": round(slip_bps, 2) if slip_bps is not None else None,
        "funding_carry_bps": round(carry_bps, 2), "horizon_hours": horizon_hours,
        "total_cost_bps": round(total, 2),
        "detail": slip, "fee_assumption_side_bps": fee_bps_side,
        "_note": "模型判断的预期收益必须**大于** total_cost_bps，否则应选 no_trade",
    }


PROFILES = {
    "swing": {"label": "波段（4H 主决策，持仓 1-3 天）", "horizon_hours": 24,
              "weights": {"direction": 0.45, "crowding": 0.20, "executability": 0.15, "invalidation": 0.20},
              "conf_min_trade": 0.55, "conf_min_full": 0.75},
    "scalp": {"label": "短打（1H 主决策，持仓 2-8 小时）", "horizon_hours": 4,
              "weights": {"direction": 0.55, "crowding": 0.10, "executability": 0.25, "invalidation": 0.10},
              "conf_min_trade": 0.65, "conf_min_full": 0.85},
}


def build_state(symbol: str, profile: str, equity: float, horizon_hours: float | None,
                leverage: float) -> dict:
    """组装 TypeSafe 的 state：紧凑、相对化、可读（借 jev-trader buildState）"""
    snap = cs.build(symbol, "both", equity, 1.0, True, leverage)
    px = snap["price"]["spot_price"]
    perp = snap["meta"]["perp_symbol"]
    h4 = snap["technicals"]["h4"]
    def _k(interval: str, n: int):
        """tradfi（股票/ETF/商品）无现货K线，自动回退到永续"""
        for mk in ("spot", "futures"):
            try:
                d = cs.klines("binance", symbol, interval, n, mk)
                if len(d):
                    return d
            except Exception:                                  # noqa: BLE001
                continue
        return cs.pd.DataFrame()

    bars4 = _k("4h", 400)
    closes4 = [float(x) for x in bars4["Close"].tolist()] if len(bars4) else []
    h1 = _k("1h", 400)
    closes1 = [float(x) for x in h1["Close"].tolist()] if len(h1) else []
    fund = snap["funding"].get("binance", {})
    hz = horizon_hours or PROFILES[profile]["horizon_hours"]
    prof = PROFILES[profile]

    book = book_state(perp, px, "futures") or book_state(symbol, px, "spot")
    flow = flow_state(perp if snap["meta"]["perp_symbol"] != symbol else symbol, 30)
    planned_notional = round(equity * 0.34, 0)   # 与快照仓位模型同一量级，用于滑点预算
    cost = cost_state(perp, px, planned_notional, hz, fund.get("funding_annualized_pct"), book)

    liq = snap["technicals"]["liquidation_magnets"]
    liq_summary = {
        "below_pct": [round(x["dist_pct"], 2) for x in liq.get("magnet_below", [])[:2]],
        "above_pct": [round(x["dist_pct"], 2) for x in liq.get("magnet_above", [])[:2]],
        "position_in_range_pct": liq.get("recent_range", {}).get("position_in_range_pct"),
    }
    agg = snap.get("aggregator", {}) or {}
    sm = (agg.get("sentiment") or {}).get("matched") or {}

    state = {
        "symbol": symbol,
        "perp_contract": snap["meta"]["perp_symbol"],
        "venue": "Binance（HTX 作对照）",
        "as_of_utc": snap["meta"]["as_of"],
        "last_4h_bar_utc": snap["meta"]["last_bar_4h_utc"],
        "profile": prof["label"],
        "horizon_hours": hz,
        "market_structure": {
            "price": px, "mark": snap["price"]["mark_price"], "basis_bps": snap["price"]["basis_bps"],
            "change_24h_pct": snap["price"]["change_24h_pct"],
            "high_24h": snap["price"]["high_24h"], "low_24h": snap["price"]["low_24h"],
            "spread_bps": (book or {}).get("spread_bps"),
            "book_imbalance_10bps": (book or {}).get("imbalance_10bps"),
            "depth_bands_usd": (book or {}).get("depth_bands"),
            "book_top5": (book or {}).get("levels"),
        },
        "trend_relative_bps": {
            "4h_bars": {f"last{k}": _returns_bps(closes4, k) for k in (1, 5, 20, 100)},
            "1h_bars": {f"last{k}": _returns_bps(closes1, k) for k in (1, 5, 20, 100)},
            "ema_position_pct": {"vs_ema20": h4.get("dist_ema20_pct"), "vs_ema50": h4.get("dist_ema50_pct")},
            "rsi": {"4h": h4.get("rsi14"), "1h": snap["technicals"]["h1"].get("rsi14"),
                    "1d": snap["technicals"]["d1"].get("rsi14")},
            "atr_pct_4h": h4.get("atr_pct"), "bb_width_percentile_1y": h4.get("bb_width_percentile"),
            "vol_annualized_pct": snap["volatility"]["realized_vol_annualized_365_pct"],
            "vol_percentile_1y": snap["volatility"]["vol_percentile_1y"],
        },
        "taker_flow": flow,
        "positioning": {
            "funding_annualized_pct": fund.get("funding_annualized_pct"),
            "funding_history_mean_annualized_pct": fund.get("mean_annualized_pct"),
            "funding_pct_periods_positive": fund.get("pct_periods_positive"),
            "funding_percentile_of_current": fund.get("percentile_of_current"),
            "open_interest_usd": snap["positioning"].get("open_interest", {}).get("current_usd"),
            "oi_change_24h_pct": snap["positioning"].get("open_interest", {}).get("change_24h_pct"),
            "oi_percentile_30d": snap["positioning"].get("open_interest", {}).get("percentile_30d"),
            "oi_price_quadrant": snap["positioning"].get("oi_price_quadrant", {}).get("reading"),
            "long_account_pct": snap["positioning"].get("long_short_account_ratio", {}).get("current_long_pct"),
            "long_account_percentile": snap["positioning"].get("long_short_account_ratio", {}).get("percentile"),
            "top_trader_long_pct": snap["positioning"].get("top_trader_position_ratio", {}).get("current_long_pct"),
            "oi_divergence_ratio": (agg.get("oi_divergence") or {}).get("divergence_ratio"),
        },
        "sentiment": {
            "fear_greed": (agg.get("fear_greed") or {}).get("value"),
            "square_hype_rank": (agg.get("square_hype") or {}).get("rank"),
            "square_hype_of_total": (agg.get("square_hype") or {}).get("of_total"),
            "ai_sentiment": sm.get("sentiment"),
            "ai_sentiment_summary": (sm.get("summary") or "")[:240],
        },
        "structure_levels": {
            "support_4h": snap["technicals"]["levels_4h"].get("support_zones"),
            "resistance_4h": snap["technicals"]["levels_4h"].get("resistance_zones"),
            "liquidation_magnets": liq_summary,
        },
        "cost": cost,
        "execution_constraints": {
            "venue": venue_constraints(snap["meta"]["perp_symbol"]),
            "account_equity_usd": equity,
            "planned_notional_usd": planned_notional,
            "exchange_leverage_setting": leverage,
            "max_safe_leverage_x": (snap["risk_framework"]["table"][0]["max_safe_leverage_x"]
                                    if snap["risk_framework"].get("table") else None),
            "atr_based_stop_2x_pct": round((h4.get("atr_pct") or 0) * 2, 2),
            "weekend_volume_vs_weekday_pct": snap["liquidity"]["session_profile"].get("weekend_vs_weekday_pct"),
            "thin_hours_utc": [x["hour_utc"] for x in snap["liquidity"]["session_profile"].get("thinnest_hours_utc", [])],
        },
        "_brief": {
            "goal": f"判断在未来约 {hz} 小时内，{symbol} 是否值得开一笔方向性仓位（含成本），"
                    f"以及应选 long / short / no_trade",
            "timing": "决策后以限价或市价在数分钟内建仓，止损与止盈按结构位设置",
            "inputs_priority": "最重要的是 taker_flow（CVD 与近期主动成交）与 positioning（资金费率/OI 拥挤度）；"
                               "其次 market_structure 的盘口失衡与深度；cost.total_cost_bps 是必须跨过的门槛；"
                               "structure_levels 决定止损位置，execution_constraints 决定杠杆与仓位上限",
        },
    }
    return state


# ───────────────────────── TypeSafe 请求（结构化 instructions + 并行原子问题） ─────────────────────────

def ts_request(state: dict, profile: str, timeout: int = 120, model: str = "jev-latest") -> dict:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise SystemExit("[FATAL] 未设置 TYPESAFE_API_KEY")
    prof = PROFILES[profile]
    hz = state["horizon_hours"]
    c = state["cost"]["total_cost_bps"]

    questions = {
        # ① 方向（Choice，criteria 里写明成本阈值）
        "direction": {
            "type": "choice",
            "instructions": {
                "question": f"未来约 {hz} 小时内，{state['symbol']} 更可能走出哪一种结果？",
                "goal": f"为 {state['symbol']}（永续合约 {state['perp_contract']}）决定开仓方向或观望。"
                        f"交易需要往返成本约 {c} bps（手续费+滑点+资金费率），"
                        f"因此预期波动必须**显著超过**该成本才值得动手。",
                "timing": f"决策即刻执行，持仓约 {hz} 小时，止损按 4H 结构位设置。",
                "inputs": "`taker_flow`（CVD、买卖额、最近主动成交方向）是最强信号；"
                          "`positioning`（资金费率方向与分位、OI 变化与四象限、多空账户比、OI 背离）反映杠杆拥挤度；"
                          "`market_structure`（盘口失衡、深度分档、基差）反映即时供需；"
                          "`trend_relative_bps`（各周期 bps 收益、RSI、ATR%、波动率分位）反映趋势与波动状态；"
                          "`structure_levels` 给出止损/目标可放的位置；"
                          "`cost.total_cost_bps` 是必须跨过的门槛。",
            },
            "criteria": {
                "long": f"做多：预期上行幅度**明显超过** {c} bps 的往返成本，且上行空间到最近阻力/清算簇的距离"
                        f"大于到 4H 结构止损的距离。",
                "short": f"做空：预期下行幅度**明显超过** {c} bps 的往返成本，且下行空间到最近支撑/清算簇的距离"
                         f"大于到 4H 结构止损的距离。",
                "no_trade": f"观望：预期波动小于或接近 {c} bps 成本、或信号相互冲突、"
                            f"或方向概率与置信度不足以支撑一笔正期望交易。",
            },
        },
        # ② 拥挤度（Score，level 自洽描述）
        "crowding": {
            "type": "score",
            "instructions": "当前杠杆与情绪的拥挤程度如何（对**新增**仓位而言）？依据资金费率及其历史分位、"
                            "OI 变化与 30 日分位、多空账户比极值、社交热度排名与恐惧贪婪指数。",
            "criteria": [
                "极不拥挤：费率接近历史低位或为负，OI 处低分位，情绪冷淡——反向机会多",
                "偏不拥挤：拥挤指标偏冷，新增仓位不易被挤压",
                "中性：杠杆与情绪处历史中位，无可利用的极端",
                "偏拥挤：费率/OI/情绪偏热，新仓位易被双向挤压",
                "极度拥挤：费率与 OI 处高分位、情绪极端——新增同向仓位风险显著，反向或观望更优",
            ],
        },
        # ③ 可执行性（Score）
        "executability": {
            "type": "score",
            "instructions": "按当前流动性与成本，这笔交易的可执行性如何？依据盘口失衡与深度分档、"
                            "计划名义的滑点、成本占预期波动的比例、低流动性时段与周末因素。",
            "criteria": [
                "极差：深度薄/滑点高/成本吃掉大部分预期收益，不具备可执行性",
                "偏差：成本偏高或深度不足，只能极小仓位",
                "一般：成本与深度可接受",
                "好：深度充足、滑点低、成本占预期收益比例小",
                "极好：深度厚、价差窄、滑点可忽略，可正常建仓",
            ],
        },
        # ④ 失效条件清晰度（Noul）
        "invalidation_clarity": {
            "type": "noul",
            "instructions": "基于给定的 `structure_levels`，是否存在**清晰、可观测、可执行**的失效条件"
                            "（例如某个价格收盘跌破/站上即离场），而不是模糊的“感觉不对就走”？",
            "criteria": {"true": "存在明确价位与判定方式", "false": "结构模糊，无法设定清晰失效条件"},
        },
    }
    body = json.dumps({"state": state, "model": model, "questions": questions}).encode()
    req = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = json.load(r)
            out["_latency_ms"] = round((time.time() - t0) * 1000)
            return out
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise SystemExit(f"[FATAL] TypeSafe HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
        except Exception as e:                       # noqa: BLE001
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise SystemExit(f"[FATAL] TypeSafe 请求失败: {type(e).__name__} {e}")


def mock_request(state: dict, profile: str) -> dict:
    """启发式替身（借 jev-trader 的 MockModel 思路）：无 API 也能跑通管线并自测"""
    t = state["trend_relative_bps"]
    flow = state.get("taker_flow") or {}
    pos = state["positioning"]
    rsi = t["rsi"]["4h"] or 50
    momo = (t["4h_bars"].get("last5") or 0) / 200 + (t["1h_bars"].get("last20") or 0) / 300
    cvd = (flow.get("cvd_to_volume") or 0) * 2
    imb = (state["market_structure"]["book_imbalance_10bps"] or 0) * 1.5
    crowd = -((pos.get("funding_percentile_of_current") or 50) - 50) / 100 - \
            ((pos.get("oi_percentile_30d") or 50) - 50) / 100
    rev = (50 - rsi) / 50
    sig = momo + cvd + imb + crowd + rev
    p_long = 1 / (1 + pow(2.718281828, -sig))
    p_no = 0.25 if abs(p_long - 0.5) < 0.12 else 0.12
    rest = 1 - p_no
    probs = {"long": round(p_long * rest, 3), "short": round((1 - p_long) * rest, 3), "no_trade": round(p_no, 3)}
    choice = max(probs, key=probs.get)
    conf = round(max(probs.values()), 2)
    crowding = 3.5 if (pos.get("oi_percentile_30d") or 0) > 90 else 3.0
    execu = 4.0 if (state["cost"]["total_cost_bps"] or 99) < 25 else 2.5
    return {"model": "mock-heuristic", "_latency_ms": 0,
            "answers": {
                "direction": {"type": "choice", "choice": choice, "probabilities": probs, "confidence": conf},
                "crowding": {"type": "score", "score": crowding, "confidence": 0.5,
                             "legend": {"0": "极不拥挤", "1": "偏不拥挤", "2": "中性", "3": "偏拥挤", "4": "极度拥挤"},
                             "probabilities": {}},
                "executability": {"type": "score", "score": execu, "confidence": 0.5,
                                  "legend": {"0": "极差", "1": "偏差", "2": "一般", "3": "好", "4": "极好"},
                                  "probabilities": {}},
                "invalidation_clarity": {"type": "noul",
                                         "noul": 1.0 if state["structure_levels"]["support_4h"] else 0.5},
            },
            "usage": {"input_tokens": 0, "output_tokens": 0}}


# ───────────────────────── 代码侧：归一化 + 门控 + 仓位（代码拥有执行） ─────────────────────────

def gate_and_size(state: dict, answers: dict, profile: str) -> dict:
    symbol_upper = state.get("symbol", "")
    """composite scoring + confidence-gated routing + 波动率目标仓位 + 爆仓距离校验"""
    prof = PROFILES[profile]
    w = prof["weights"]
    d = answers.get("direction", {})
    probs = d.get("probabilities") or {}
    action = d.get("choice")
    conf = d.get("confidence")
    def norm(s, mx):        # 归一化到 0-1（composite scoring 的代码侧环节）
        return (s / mx) if s is not None else None
    crowd = norm((answers.get("crowding") or {}).get("score"), 4)
    execu = norm((answers.get("executability") or {}).get("score"), 4)
    inval = (answers.get("invalidation_clarity") or {}).get("noul")
    dirv = probs.get(action or "", None)
    # 方向分：所选动作概率相对 1/n 的抬升；质量分：拥挤度反向 + 可执行性 + 失效清晰度
    q_dir = dirv if dirv is not None else None
    q_crowd = (1 - crowd) if crowd is not None else None
    q_exec = execu
    q_inval = inval
    parts = {("direction", w["direction"], q_dir), ("crowding", w["crowding"], q_crowd),
             ("executability", w["executability"], q_exec), ("invalidation", w["invalidation"], q_inval)}
    have = [(k, wt, v) for k, wt, v in parts if v is not None]
    conviction = (sum(wt * v for _, wt, v in have) / sum(wt for _, wt, _ in have)) if have else None

    # 门控（confidence-gated routing）
    gates, verdict, size_mult = [], None, 0.0
    if action == "no_trade":
        verdict = "观望（模型给出的动作即 no_trade）"
    elif conf is None or dirv is None:
        verdict = "观望（缺少概率/置信度）"
    elif conf < prof["conf_min_trade"]:
        verdict = f"观望（置信度 {conf:.2f} < {prof['conf_min_trade']}：高风险动作要求更高置信度）"
    elif dirv < 0.5:
        verdict = f"观望（所选方向概率 {dirv:.2f} < 0.5，非正期望）"
    else:
        size_mult = 1.0 if conf >= prof["conf_min_full"] else 0.5
        verdict = ("按标准仓执行" if size_mult == 1.0 else
                   f"按半仓执行（置信度 {conf:.2f} 在 {prof['conf_min_trade']}~{prof['conf_min_full']} 之间）")
        gates.append("模型方向 + 置信度门控通过")

    # 代码侧硬约束（借 jev-trader 的 allowed 思路）：成本、流动性、爆仓距离
    cost_bps = state["cost"]["total_cost_bps"]
    exp_move_bps = (state["trend_relative_bps"]["atr_pct_4h"] or 0) * 100 * 1.5  # 1.5×ATR 的典型幅度
    if verdict.startswith("按") and cost_bps and exp_move_bps and cost_bps > exp_move_bps / 3:
        gates.append(f"成本约束未过：成本 {cost_bps} bps > 典型波幅{exp_move_bps:.0f}bps 的 1/3 → 降级为观望")
        verdict, size_mult = f"观望（成本 {cost_bps} bps 相对预期波幅过高）", 0.0
    if state["cost"].get("detail") and not state["cost"]["detail"].get("fillable"):
        gates.append("流动性与计划名义不匹配 → 降级为观望")
        verdict, size_mult = "观望（盘口深度不足以支撑计划名义）", 0.0

    sizing = None
    if size_mult > 0:
        atr = state["trend_relative_bps"]["atr_pct_4h"] or 0
        stop_pct = round(atr * 2, 2) / 100
        equity = state["execution_constraints"]["account_equity_usd"]
        risk_usd = equity * 0.01 * size_mult
        notional = round(risk_usd / stop_pct) if stop_pct > 0 else None
        lev = state["execution_constraints"]["exchange_leverage_setting"]
        liq_dist = (1 / lev) - 0.005 - 0.001 if lev > 1 else None
        liq_ok = liq_dist is not None and liq_dist >= 1.5 * stop_pct
        vc = state["execution_constraints"].get("venue") or {}
        min_notional = vc.get("min_notional_usdt")
        if min_notional and notional and notional < min_notional:
            gates.append(f"**仓位低于交易所最小名义**：按 1% 风险纪律算出的名义 ${notional:,} < {symbol_upper} 最小名义 "
                         f"${min_notional} → 该合约在 {equity:.0f} USDT 账户上无法执行此纪律")
            verdict, size_mult, notional = (f"观望（{symbol_upper} 最小名义 ${min_notional} > 纪律仓位 ${notional:,}；"
                                            f"需提高风险预算或换更小名义的合约）"), 0.0, None
            liq_ok = False
        if vc.get("venue_max_leverage_x") and lev > vc["venue_max_leverage_x"]:
            gates.append(f"交易所杠杆 {lev}x 超过该合约一档上限 {vc['venue_max_leverage_x']}x → 降至上限")
            lev = float(vc["venue_max_leverage_x"])
        if not liq_ok and notional:
            gates.append(f"爆仓距离校验未过（{liq_dist and round(liq_dist*100,2)}% < 1.5×止损 {round(stop_pct*100*1.5,2)}%）→ 需降低杠杆")
            verdict, size_mult, notional = "观望（杠杆过高，强平先于止损）", 0.0, None
        elif notional:
            sizing = {"risk_budget_pct": round(1.0 * size_mult, 2), "stop_atr_mult": 2.0,
                      "stop_distance_pct": round(stop_pct * 100, 2), "notional_usd": notional,
                      "implied_leverage_x": round(notional / equity, 3) if notional else None,
                      "exchange_leverage_x": lev,
                      "margin_usd": round(notional / lev, 2) if notional else None,
                      "est_liquidation_price": round(state["market_structure"]["price"] * (1 - liq_dist), 2) if liq_dist else None,
                      "liq_distance_pct": round(liq_dist * 100, 2) if liq_dist else None,
                      "liq_check": "通过（强平距离 ≥ 1.5×止损距离）",
                      "funding_carry_bps_over_horizon": state["cost"]["funding_carry_bps"],
                      "venue_min_notional_usdt": (state["execution_constraints"].get("venue") or {}).get("min_notional_usdt")}
    return {"action": action, "probabilities": probs, "confidence": conf,
            "conviction": round(conviction, 3) if conviction is not None else None,
            "conviction_parts": {k: (round(v, 3) if v is not None else None) for k, _, v in parts},
            "verdict": verdict, "gates": gates, "size_multiplier": size_mult, "sizing": sizing,
            "cost_bps": cost_bps, "expected_move_bps": round(exp_move_bps, 0)}


def append_ledger(path: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ───────────────────────── 结果回填（借 jev-trader 的 totals/PnL 闭环） ─────────────────────────

def resolve_ledger(path: str, symbol: str | None = None) -> None:
    if not os.path.exists(path):
        raise SystemExit(f"[FATAL] 账本不存在：{path}")
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    updated, stats = [], {}
    for r in rows:
        if (r.get("outcome") or r.get("action") == "no_trade" or not r.get("plan")
                or not r.get("executed", True)):            # 被门控否决的决策不参与胜率统计
            updated.append(r)
            continue
        sym, ts, hz = r["symbol"], r["ts_utc"], r.get("horizon_hours") or 24
        t0 = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        if time.time() - t0 < hz * 3600:
            updated.append(r)
            continue
        try:
            k = cs.klines("binance", sym, "1h", 500, "spot")
        except Exception:                                     # noqa: BLE001
            updated.append(r)
            continue
        win = k[[i for i in k.index if i.timestamp() >= t0]]
        if len(win) < 2:
            updated.append(r)
            continue
        highs, lows = [float(x) for x in win["High"].tolist()], [float(x) for x in win["Low"].tolist()]
        plan = r["plan"]
        ent, sl, tp1 = plan["entry"], plan["stop"], plan["tp1"]
        long = r["action"] == "long"
        hit_tp = any(h >= tp1 for h in highs) if long else any(l <= tp1 for l in lows)
        hit_sl = any(l <= sl for l in lows) if long else any(h >= sl for h in highs)
        first = ("tp" if (hit_tp and not hit_sl) else "sl" if (hit_sl and not hit_tp) else
                 "both" if (hit_tp and hit_sl) else "neither")
        r = dict(r)
        r["outcome"] = {"resolved_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "hit_tp1": hit_tp, "hit_sl": hit_sl, "first_touch": first,
                        "max_favorable_pct": round((max(highs) / ent - 1) * 100, 2) if long else round((min(lows) / ent - 1) * 100, 2),
                        "max_adverse_pct": round((min(lows) / ent - 1) * 100, 2) if long else round((max(highs) / ent - 1) * 100, 2)}
        updated.append(r)
    with open(path, "w", encoding="utf-8") as f:
        for r in updated:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    for r in updated:
        o = r.get("outcome")
        if not o:
            continue
        key = (r["action"], r.get("confidence_bucket"))
        s = stats.setdefault(key, {"n": 0, "tp": 0, "sl": 0})
        s["n"] += 1
        s["tp"] += 1 if o["first_touch"] == "tp" else 0
        s["sl"] += 1 if o["first_touch"] == "sl" else 0
    n_exec = sum(1 for r in updated if r.get("executed", True) and r.get("action") != "no_trade")
    n_gated = len(updated) - n_exec
    print(f"账本共 {len(updated)} 条（其中已执行 {n_exec} 条、被门控否决 {n_gated} 条）"
          f"，已回填结果 {sum(1 for r in updated if r.get('outcome'))} 条")
    if stats:
        print("\n命中率统计（按 动作 × 置信度桶）：")
        print("| 动作 | 置信度桶 | 样本 | 先触 TP | 先触 SL | TP 占比 |")
        print("|---|---|---|---|---|---|")
        for (a, cb), s in sorted(stats.items()):
            print(f"| {a} | {cb} | {s['n']} | {s['tp']} | {s['sl']} | {round(s['tp']/s['n']*100,1) if s['n'] else 0}% |")
    else:
        print("暂无可统计样本（需已到期的决策）")


# ───────────────────────── 主流程 ─────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="单次交易决策引擎（TypeSafe 单点决策，融合 jev-trader 范式）")
    ap.add_argument("symbol", nargs="?", help="交易对，如 BTCUSDT")
    ap.add_argument("--profile", default="swing", choices=list(PROFILES))
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--leverage", type=float, default=5.0)
    ap.add_argument("--horizon-hours", type=float, default=None)
    ap.add_argument("--model", default="ts", choices=["ts", "mock"], help="ts=真实 TypeSafe；mock=启发式替身")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--resolve", action="store_true", help="回填历史决策结果并统计命中率")
    ap.add_argument("--dry-run", action="store_true", help="不写入账本")
    a = ap.parse_args()

    if a.resolve:
        resolve_ledger(a.ledger)
        return 0
    if not a.symbol:
        raise SystemExit("请给出交易对，或用 --resolve 回填账本")

    t0 = time.time()
    state = build_state(a.symbol.upper(), a.profile, a.equity, a.horizon_hours, a.leverage)
    answers = mock_request(state, a.profile) if a.model == "mock" else ts_request(state, a.profile)
    gated = gate_and_size(state, answers["answers"], a.profile)
    price = state["market_structure"]["price"]

    # 计划：止损取「结构位」但受 3×ATR 上限约束；TP1 取「最近阻力/支撑」但必须 ≥1.5×止损距离，
    # 否则退到量度目标（近期区间幅度）；随后用 RR≥2 硬门控（与备忘录 CIO 规则一致）
    sups = [float(x) for x in (state["structure_levels"]["support_4h"] or [])]
    res = [float(x) for x in (state["structure_levels"]["resistance_4h"] or [])]
    atr = state["trend_relative_bps"]["atr_pct_4h"] or 0
    rng = state["structure_levels"]["liquidation_magnets"]
    hi24, lo24 = state["market_structure"]["high_24h"], state["market_structure"]["low_24h"]
    measured = abs((hi24 or price) - (lo24 or price))
    plan, rr_gate = None, None
    if gated["action"] in ("long", "short"):
        max_stop = atr * 3 / 100
        if gated["action"] == "long":
            cand = [s for s in sups if 0 < (price - s) / price <= max_stop]
            stop = round((max(cand) * 0.997) if cand else price * (1 - atr * 2 / 100), 2)
            risk = price - stop
            tp_cands = [r for r in res if (r - price) >= 1.5 * risk] +                        [x for x in (hi24, price + measured, price + 3 * risk) if x and (x - price) >= 1.5 * risk]
            tp1 = round(min(tp_cands), 2) if tp_cands else None
            rr = round((tp1 - price) / risk, 2) if (tp1 and risk > 0) else None
        else:
            cand = [r for r in res if 0 < (r - price) / price <= max_stop]
            stop = round((min(cand) * 1.003) if cand else price * (1 + atr * 2 / 100), 2)
            risk = stop - price
            tp_cands = [s for s in sups if (price - s) >= 1.5 * risk] +                        [x for x in (lo24, price - measured, price - 3 * risk) if x and (price - x) >= 1.5 * risk]
            tp1 = round(max(tp_cands), 2) if tp_cands else None
            rr = round((price - tp1) / risk, 2) if (tp1 and risk > 0) else None
        plan = {"entry": price, "stop": stop, "tp1": tp1, "rr": rr,
                "stop_basis": "结构位(≤3×ATR)" if cand else "2×ATR(无合规结构位)",
                "tp_basis": "最近阻力/支撑" if (res or sups) and tp1 else "量度目标/3R"}
        if rr is None:
            rr_gate = "无可达目标位（近期区间无量度空间）→ 观望"
        elif rr < 2.0:
            rr_gate = f"风险回报比 {rr} < 2.0 → 观望（等更好的入场位，而非降低标准）"
        if rr_gate:
            gated["verdict"] = f"观望（{rr_gate}）"
            gated["gates"].append(rr_gate)
            gated["size_multiplier"] = 0.0
            gated["sizing"] = None
            plan["rejected"] = True
        else:
            gated["gates"].append(f"风险回报比 {rr} ≥ 2.0 通过")
    # 回踩参考位（把备忘录的「不追高」纪律带进单次决策）
    if plan:
        plan["pullback_reference"] = "若现价入场 RR 不足，等回踩至 4H EMA20 / 结构支撑区再评估"
    gated["plan"] = plan

    state_hash = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": a.symbol.upper(), "perp_contract": state["perp_contract"],
        "profile": a.profile, "horizon_hours": state["horizon_hours"],
        "model": answers.get("model"), "questions_version": QUESTIONS_VERSION,
        "state_hash": state_hash, "latency_ms": answers.get("_latency_ms"),
        "usage": answers.get("usage"),
        "answers": answers["answers"],
        "action": gated["action"], "confidence": gated["confidence"],
        "confidence_bucket": ("<0.55" if (gated["confidence"] or 0) < 0.55 else
                              "0.55-0.75" if (gated["confidence"] or 0) < 0.75 else ">=0.75"),
        "conviction": gated["conviction"], "conviction_parts": gated["conviction_parts"],
        "verdict": gated["verdict"], "gates": gated["gates"], "size_multiplier": gated["size_multiplier"],
        "executed": bool(gated["size_multiplier"] and gated["size_multiplier"] > 0),
        "sizing": gated["sizing"], "cost_bps": gated["cost_bps"], "expected_move_bps": gated["expected_move_bps"],
        "plan": plan, "price_at_decision": price, "outcome": None,
    }
    if not a.dry_run:
        append_ledger(a.ledger, rec)

    print(f"# 单次决策 · {rec['symbol']}（{state['perp_contract']}）· {PROFILES[a.profile]['label']}")
    print(f"> 数据时间 {state['as_of_utc']}｜模型 {answers.get('model')}｜耗时 {answers.get('_latency_ms')}ms"
          f"｜state_hash {state_hash}｜决策周期 {state['horizon_hours']}h")
    print()
    print("| 原子判断 | 结果 | 置信度/概率 |")
    print("|---|---|---|")
    d = answers["answers"]["direction"]
    print(f"| 方向 direction | **{d['choice']}** | " +
          " / ".join(f"{k} {v:.2f}" for k, v in sorted((d.get('probabilities') or {}).items(), key=lambda x: -x[1])) +
          f"（conf {d.get('confidence')}）|")
    cg = answers["answers"]["crowding"]
    print(f"| 拥挤度 crowding | {cg.get('score')} / 4 | conf {cg.get('confidence')} |")
    ex = answers["answers"]["executability"]
    print(f"| 可执行性 executability | {ex.get('score')} / 4 | conf {ex.get('confidence')} |")
    iv = answers["answers"]["invalidation_clarity"]
    print(f"| 失效条件清晰度 | noul {iv.get('noul')} | — |")
    print()
    print("**代码侧组合与门控**")
    if gated["conviction"] is not None:
        print(f"- 综合信心度 conviction = **{gated['conviction']}**（权重档：{a.profile}）")
    print(f"- 成本门槛：往返总成本 **{gated['cost_bps']} bps** ｜ 典型波幅（1.5×ATR4H）≈ {gated['expected_move_bps']:.0f} bps")
    for g in gated["gates"]:
        print(f"- {g}")
    print(f"\n**决议：{gated['verdict']}**")
    if gated["sizing"]:
        s = gated["sizing"]
        print(f"\n仓位：风险 {s['risk_budget_pct']}% 权益 ｜ 名义 **${s['notional_usd']:,}** ｜ 隐含杠杆 {s['implied_leverage_x']}x"
              f" ｜ 交易所杠杆 {s['exchange_leverage_x']}x ｜ 保证金 ${s['margin_usd']:,} ｜ 止损 {s['stop_distance_pct']}%"
              f" ｜ 估强平价 {s['est_liquidation_price']}（{s['liq_check']}）")
    if plan:
        print(f"计划：入场 {plan['entry']} ｜ 止损 {plan['stop']}（{plan.get('stop_basis')}）"
              f" ｜ TP1 {plan['tp1']}（{plan.get('tp_basis')}） ｜ **RR {plan['rr']}**"
              f"{'  ← 已否决' if plan.get('rejected') else ''}")
        print(f"纪律：{plan.get('pullback_reference')}")
    print(f"\n[账本] {'未写入(--dry-run)' if a.dry_run else a.ledger}")
    print(f"[耗时] 全流程 {round((time.time()-t0)*1000)} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
