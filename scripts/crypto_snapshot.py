#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_snapshot.py — 加密货币交易工作流的数据引擎（美股引擎的加密原生替代）

由 TypeSafe 判决驱动（jev-1.13.0）：
  · 整体可迁移性 1.50 → 数据层必须整体替换（本脚本就是那个替换）
  · 基本面锚缺失 2.86(严重) → 不做"估值/护城河"，改做代币经济 + 资金面
  · 周期错配 0.80 → 主决策周期改为 4H，1H 执行，日线看结构
  · 衍生品盲区 0.94 → 资金费率 / OI / 多空比 / 主动买卖比 全部纳入
  · 场所风险 0.95 → 订单簿深度·滑点·跨场所资金费率差 全部量化
  · 仓位模型 0.98 → 输出波动率目标仓位 + 杠杆上限 + 爆仓距离校验
  · 清算数据 0.74 → 输出清算聚集代理价位
  · 链上必要性 0.68 → 输出流通/总量/FDV 与稳定币净流入代理

数据源（全部公开无需密钥）：
  · Binance 现货 + USDT 本位永续（行情/资金费率/OI/多空比/主动买卖/深度）
  · HTX 合约（资金费率/OI/深度/行情）—— 用户所在交易所，做跨场所对照
  · CoinGecko（市值/FDV/流通量/历史高低/全市场与稳定币）
  · alternative.me 恐惧贪婪指数

用法:
  python3 crypto_snapshot.py BTCUSDT
  python3 crypto_snapshot.py SOLUSDT --venue both --equity 10000 --risk 1.0
  python3 crypto_snapshot.py HYPEUSDT --venue binance --no-cg     # 跳过 CoinGecko
  python3 crypto_snapshot.py BTCUSDT --out ~/crypto_snapshots

输出: snapshot_<SYM>_<YYYYMMDD>.json + dashboard_<SYM>_<YYYYMMDD>.md
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (compatible; hermes-crypto-snapshot/1.0)"}
BIN_SPOT = "https://api.binance.com"
BIN_FUT = "https://fapi.binance.com"
HTX = "https://api.hbdm.com"
CG = "https://api.coingecko.com/api/v3"


# ───────────────────────── HTTP ─────────────────────────

def get(url: str, tries: int = 3, timeout: int = 25):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (418, 429):          # 限流退避
                time.sleep(2.0 * (i + 1))
                continue
            if 400 <= e.code < 500:
                break
        except Exception as e:                # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:60]}"
        time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"GET failed {url} :: {last}")


def soft(fn, default=None):
    """取数失败返回 default + 记录，不中断整条管线"""
    try:
        return fn()
    except Exception:                          # noqa: BLE001
        return default


def _f(x, nd=6):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    except (TypeError, ValueError):
        return None


def _pct(a, b, nd=3):
    try:
        a, b = float(a), float(b)
        if b == 0:
            return None
        return round((a / b - 1) * 100, nd)
    except (TypeError, ValueError):
        return None


# ───────────────────────── 行情 / 指标 ─────────────────────────

def klines(exchange: str, symbol: str, interval: str, limit: int = 1000, market: str = "spot") -> pd.DataFrame:
    """分页取 K 线（Binance 单次上限 1000 根）"""
    rows, end = [], None
    need = limit
    while need > 0:
        n = min(1000, need)
        base = BIN_FUT if market == "futures" else BIN_SPOT
        path = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"
        url = f"{base}{path}?symbol={symbol}&interval={interval}&limit={n}"
        if end:
            url += f"&endTime={end}"
        data = get(url)
        if not data:
            break
        rows = data + rows
        end = int(data[0][0]) - 1
        need -= len(data)
        if len(data) < n:
            break
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["open_time", "Open", "High", "Low", "Close", "Volume",
                                     "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore"])
    for c in ("Open", "High", "Low", "Close", "Volume", "quote_volume", "tb_quote"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("ts")[["Open", "High", "Low", "Close", "Volume", "quote_volume", "tb_quote"]]


def htx_klines(contract: str, period: str = "4hour", size: int = 1000) -> pd.DataFrame:
    d = get(f"{HTX}/linear-swap-ex/market/history/kline?contract_code={contract}&period={period}&size={min(size,2000)}")
    data = d.get("data") or []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    for c in ("open", "high", "low", "close", "vol", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["id"], unit="s", utc=True)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close",
                            "vol": "Volume", "amount": "quote_volume"})
    return df.sort_values("ts").set_index("ts")[["Open", "High", "Low", "Close", "Volume", "quote_volume"]]


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def macd(s: pd.Series, f=12, sl=26, sig=9) -> pd.DataFrame:
    line = s.ewm(span=f, adjust=False).mean() - s.ewm(span=sl, adjust=False).mean()
    sigl = line.ewm(span=sig, adjust=False).mean()
    return pd.DataFrame({"line": line, "signal": sigl, "hist": line - sigl})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def atr_pct_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return atr(df, n) / df["Close"] * 100


def bb(s: pd.Series, n=20, k=2) -> pd.DataFrame:
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std(ddof=0)
    return pd.DataFrame({"upper": mid + k * sd, "mid": mid, "lower": mid - k * sd,
                         "width": (4 * sd) / mid.replace(0, np.nan)})


def tf_block(df: pd.DataFrame, label: str, ema_pairs=((20, 50), (50, 200)), n=400) -> dict:
    if df is None or len(df) < 60:
        return {"timeframe": label, "error": "样本不足"}
    d = df.tail(n)
    c = d["Close"]
    out = {
        "timeframe": label, "bars": int(len(d)),
        "last_close": _f(c.iloc[-1], 6), "last_bar_utc": str(d.index[-1])[:19],
        "rsi14": _f(rsi(c).iloc[-1], 2),
        "macd": {k: _f(v, 6) for k, v in macd(c).iloc[-1].items()},
        "atr14": _f(atr(d).iloc[-1], 6),
        "atr_pct": _f(atr_pct_series(d).iloc[-1], 3),
    }
    m = macd(c)
    out["macd"]["state"] = ("柱转正(动能转多)" if (m["hist"].iloc[-1] > 0 >= m["hist"].iloc[-2])
                            else "柱转负(动能转空)" if (m["hist"].iloc[-1] < 0 <= m["hist"].iloc[-2])
                            else ("柱为正(多头动能)" if m["hist"].iloc[-1] > 0 else "柱为负(空头动能)"))
    b = bb(c)
    out["bollinger"] = {"upper": _f(b["upper"].iloc[-1]), "mid": _f(b["mid"].iloc[-1]),
                        "lower": _f(b["lower"].iloc[-1]), "width": _f(b["width"].iloc[-1], 5)}
    ws = b["width"].dropna()
    if len(ws) > 100:
        out["bb_width_percentile"] = _f((ws <= ws.iloc[-1]).mean() * 100, 0)
    for s_, l_ in ema_pairs:
        if len(c) >= l_:
            es, el = c.ewm(span=s_, adjust=False).mean().iloc[-1], c.ewm(span=l_, adjust=False).mean().iloc[-1]
            out[f"ema{s_}"] = _f(es); out[f"ema{l_}"] = _f(el)
            out[f"dist_ema{s_}_pct"] = _pct(c.iloc[-1], es)
    px = c.iloc[-1]
    if "ema20" in out and "ema50" in out:
        out["trend"] = ("多头排列(价>EMA20>EMA50)" if px > out["ema20"] > out["ema50"]
                        else "空头排列(价<EMA20<EMA50)" if px < out["ema20"] < out["ema50"]
                        else "均线缠绕(震荡)")
    return out


def swing_levels(df: pd.DataFrame, k: int = 4, lookback: int = 300) -> dict:
    d = df.tail(lookback)
    hi, lo, cl = d["High"], d["Low"], d["Close"]
    px = float(cl.iloc[-1])
    ph, pl = [], []
    for i in range(k, len(d) - k):
        if hi.iloc[i] == hi.iloc[i - k:i + k + 1].max():
            ph.append(float(hi.iloc[i]))
        if lo.iloc[i] == lo.iloc[i - k:i + k + 1].min():
            pl.append(float(lo.iloc[i]))

    def cluster(vals, tol=0.006):     # 加密价格密度更高，容差收紧到 0.6%
        out = []
        for v in sorted(vals):
            if out and abs(v - out[-1][-1]) / max(out[-1][-1], 1e-9) < tol:
                out[-1].append(v)
            else:
                out.append([v])
        return [round(sum(g) / len(g), 6) for g in out]

    return {"resistance_zones": sorted([c for c in cluster(ph) if c > px])[:4],
            "support_zones": sorted([c for c in cluster(pl) if c < px], reverse=True)[:4],
            "note": "基于 300 根 4H 枢轴点(k=4)聚类，容差 0.6%"}


def liquidation_magnet(df: pd.DataFrame, oi_now: float, lookback: int = 180) -> dict:
    """清算聚集代理：加密止损/强平簇通常集中在近期高低点与大整数关口。
    无免费清算明细接口，故用『高量成本区 + 结构极点 + 整数关口』构造代理并显式标注。"""
    d = df.tail(lookback)
    if len(d) < 40:
        return {"error": "样本不足"}
    px = float(d["Close"].iloc[-1])
    lo, hi = float(d["Low"].min()), float(d["High"].max())

    def round_level(v):
        for step in (1000, 500, 100, 50, 10, 5, 1, 0.5, 0.1, 0.01):
            if v > step * 10:
                return round(v / step) * step
        return v

    cands = []
    for lv in (lo, hi, round_level(lo), round_level(hi), round_level(px)):
        cands.append(lv)
    # 高量成本区
    vol = d["quote_volume"].values
    price = ((d["High"] + d["Low"] + d["Close"]) / 3).values
    edges = np.linspace(lo, hi, 25)
    buckets = np.zeros(len(edges) - 1)
    idx = np.clip(np.digitize(price, edges) - 1, 0, len(buckets) - 1)
    for i, v in zip(idx, vol):
        buckets[i] += float(v)
    top = np.argsort(buckets)[::-1][:2]
    for i in top:
        cands.append(float((edges[i] + edges[i + 1]) / 2))
    below = sorted({round(c, 6) for c in cands if c < px}, reverse=True)[:3]
    above = sorted({round(c, 6) for c in cands if c > px})[:3]
    return {
        "current_price": _f(px),
        "magnet_below": [{"price": p, "dist_pct": _pct(p, px)} for p in below],
        "magnet_above": [{"price": p, "dist_pct": _pct(p, px)} for p in above],
        "recent_range": {"low": _f(lo), "high": _f(hi),
                         "position_in_range_pct": _f((px - lo) / (hi - lo) * 100, 1) if hi > lo else None},
        "method": "代理指标：结构极点 + 高量成本区 + 整数关口；非交易所清算明细，仅用于判断止损簇可能位置",
        "oi_reference": _f(oi_now, 0),
    }


def book_metrics(bids, asks, px: float, sizes=(10_000, 100_000, 1_000_000)) -> dict:
    """订单簿深度与滑点（按名义金额吃单模拟）"""
    def lvl(book):
        out = []
        for p, q in book:
            out.append((float(p), float(q)))
        return out
    b, a = lvl(bids), lvl(asks)
    if not b or not a:
        return {}
    best_bid, best_ask = b[0][0], a[0][0]
    spread_bps = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 10000

    def depth_within(side, pct):
        lim = px * (1 + pct) if side == "ask" else px * (1 - pct)
        tot = 0.0
        for p, q in (a if side == "ask" else b):
            if (side == "ask" and p <= lim) or (side == "bid" and p >= lim):
                tot += p * q
        return tot

    def slip_bps(side, notional):
        book = a if side == "buy" else b
        remaining, qty, cost = notional, 0.0, 0.0
        for p, q in book:
            take = min(p * q, remaining)
            cost += take
            qty += take / p
            remaining -= take
            if remaining <= 1e-9:
                break
        if qty <= 0:
            return None
        avg = cost / qty
        ref = best_ask if side == "buy" else best_bid
        return {"avg_price": _f(avg, 6), "slippage_bps": _f((avg / ref - 1) * 10000, 1),
                "fillable": bool(remaining <= 1e-9), "unfilled_notional": _f(remaining, 0) if remaining > 0 else 0}

    return {
        "best_bid": _f(best_bid), "best_ask": _f(best_ask), "spread_bps": _f(spread_bps, 2),
        "depth_usd_within": {"0.5pct": {s: _f(depth_within(s, 0.005), 0) for s in ("bid", "ask")},
                             "1pct": {s: _f(depth_within(s, 0.01), 0) for s in ("bid", "ask")},
                             "2pct": {s: _f(depth_within(s, 0.02), 0) for s in ("bid", "ask")}},
        "slippage_sim": {f"{int(n/1000)}k": {"buy": slip_bps("buy", n), "sell": slip_bps("sell", n)} for n in sizes},
    }


def volume_by_hour(h1: pd.DataFrame) -> dict:
    """24/7 市场的流动性时段分布（UTC），用于避开低流动性开仓时点"""
    if h1 is None or len(h1) < 200:
        return {}
    q = h1["quote_volume"].copy()
    q.index = pd.to_datetime(q.index, utc=True)
    med = q.groupby(q.index.hour).median()
    wd = q.groupby(q.index.dayofweek).median()
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    thin = sorted(med.dropna().items(), key=lambda x: x[1])[:3]
    return {
        "median_usd_volume_by_hour_utc": {int(k): _f(v, 0) for k, v in med.dropna().items()},
        "thinnest_hours_utc": [{"hour_utc": int(h), "median_volume_usd": _f(v, 0),
                                "vs_daily_median_pct": _pct(v, med.median())} for h, v in thin],
        "median_usd_volume_by_weekday": {names[int(k)]: _f(v, 0) for k, v in wd.dropna().items()},
        "weekend_vs_weekday_pct": _pct(wd.iloc[5:].mean() if len(wd) >= 7 else None,
                                       wd.iloc[:5].mean() if len(wd) >= 5 else None)
        if len(wd) >= 7 else None,
    }


# ───────────────────────── 衍生品 / 仓位 ─────────────────────────

def funding_block(symbol: str, htx_contract: str | None) -> dict:
    out: dict = {}
    pi = soft(lambda: get(f"{BIN_FUT}/fapi/v1/premiumIndex?symbol={symbol}"))
    if pi:
        fr = _f(pi.get("lastFundingRate"), 8)
        out["binance"] = {
            "mark_price": _f(pi.get("markPrice"), 6), "index_price": _f(pi.get("indexPrice"), 6),
            "last_funding_rate_8h": fr,
            "funding_annualized_pct": _f((fr or 0) * 3 * 365 * 100, 2) if fr is not None else None,
            "funding_interval_hours": 8,
            "next_funding_time_utc": datetime.fromtimestamp(int(pi["nextFundingTime"]) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if pi.get("nextFundingTime") else None,
        }
    hist = soft(lambda: get(f"{BIN_FUT}/fapi/v1/fundingRate?symbol={symbol}&limit=200")) or []
    if hist:
        rates = [float(x["fundingRate"]) for x in hist]
        pos = sum(1 for r in rates if r > 0)
        out.setdefault("binance", {})
        out["binance"].update({
            "history_periods": len(rates),
            "history_days": round(len(rates) * 8 / 24, 1),
            "mean_8h_pct": _f(st.mean(rates) * 100, 5),
            "median_8h_pct": _f(st.median(rates) * 100, 5),
            "max_8h_pct": _f(max(rates) * 100, 5), "min_8h_pct": _f(min(rates) * 100, 5),
            "pct_periods_positive": _f(pos / len(rates) * 100, 1),
            "mean_annualized_pct": _f(st.mean(rates) * 3 * 365 * 100, 2),
            "percentile_of_current": _f((sum(1 for r in rates if r <= rates[-1]) / len(rates)) * 100, 0),
            "interpretation": ("多头向空头付费(拥挤多头/正基差)" if st.mean(rates) > 0 else "空头向多头付费(拥挤空头/负基差)"),
        })
        d = out["binance"]
        if d.get("mean_annualized_pct") is not None:
            for days in (7, 30):
                d[f"carry_cost_{days}d_pct_of_notional"] = _f(d["mean_annualized_pct"] / 100 / 365 * days * 100, 3)
    if htx_contract:
        h = soft(lambda: get(f"{HTX}/linear-swap-api/v1/swap_funding_rate?contract_code={htx_contract}"))
        hd = (h or {}).get("data") or {}
        if hd:
            fr = _f(hd.get("funding_rate"), 8)
            out["htx"] = {
                "funding_rate_8h": fr,
                "funding_annualized_pct": _f((fr or 0) * 3 * 365 * 100, 2) if fr is not None else None,
                "next_funding_time_utc": datetime.fromtimestamp(int(hd["next_funding_time"]) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if hd.get("next_funding_time") else None,
                "contract_code": htx_contract,
            }
        hh = soft(lambda: get(f"{HTX}/linear-swap-api/v1/swap_historical_funding_rate?contract_code={htx_contract}&page_size=100"))
        rows = ((hh or {}).get("data") or {}).get("data") or []
        if rows:
            rates = [float(r["funding_rate"]) for r in rows]
            out["htx"].update({
                "history_periods": len(rates),
                "mean_annualized_pct": _f(st.mean(rates) * 3 * 365 * 100, 2),
                "pct_periods_positive": _f(sum(1 for r in rates if r > 0) / len(rates) * 100, 1),
            })
    if out.get("binance") and out.get("htx"):
        ba, ha = out["binance"].get("mean_annualized_pct"), out["htx"].get("mean_annualized_pct")
        if ba is not None and ha is not None:
            out["cross_venue_spread_mean_annualized_pct"] = _f(ha - ba, 2)
            out["cross_venue_note"] = ("基于两地历史均值年化：HTX − Binance；正值=HTX 多头成本更高(或空头收益更高)。"
                                       "瞬时值见两地 last_funding_rate_8h / funding_rate_8h")
    return out


def positioning_block(symbol: str) -> dict:
    out = {}
    oi = soft(lambda: get(f"{BIN_FUT}/fapi/v1/openInterest?symbol={symbol}"))
    oih = soft(lambda: get(f"{BIN_FUT}/futures/data/openInterestHist?symbol={symbol}&period=4h&limit=180")) or []
    if oih:
        vals = [float(x["sumOpenInterestValue"]) for x in oih]
        coins = [float(x["sumOpenInterest"]) for x in oih]
        out["open_interest"] = {
            "current_usd": _f(vals[-1], 0), "current_coins": _f(coins[-1], 2),
            "change_1bar_pct": _pct(vals[-1], vals[-2]) if len(vals) > 1 else None,
            "change_24h_pct": _pct(vals[-1], vals[-7]) if len(vals) > 7 else None,
            "change_7d_pct": _pct(vals[-1], vals[-43]) if len(vals) > 43 else None,
            "change_30d_pct": _pct(vals[-1], vals[-181]) if len(vals) > 181 else None,
            "percentile_30d": _f(sum(1 for v in vals[-180:] if v <= vals[-1]) / len(vals[-180:]) * 100, 0),
        }
    if oi:
        out.setdefault("open_interest", {})["live_contracts"] = _f(oi.get("openInterest"), 2)

    # OI 与价格四象限
    kl = soft(lambda: klines("binance", symbol, "4h", 200, "futures"))
    if oih and kl is not None and len(kl) > 10:
        px_ch = _pct(float(kl["Close"].iloc[-1]), float(kl["Close"].iloc[-7]))
        oi_ch = out["open_interest"].get("change_24h_pct")
        if px_ch is not None and oi_ch is not None:
            quad = ("价涨+OI涨：新多头建仓(趋势可续)" if px_ch > 0 and oi_ch > 0 else
                    "价涨+OI减：空头回补(反弹质量弱)" if px_ch > 0 and oi_ch < 0 else
                    "价跌+OI涨：新空头建仓(下跌可续)" if px_ch < 0 and oi_ch > 0 else
                    "价跌+OI减：多头被平(去杠杆尾声)")
            out["oi_price_quadrant"] = {"price_24h_pct": px_ch, "oi_24h_pct": oi_ch, "reading": quad}

    for name, path in (("long_short_account_ratio", "globalLongShortAccountRatio"),
                       ("top_trader_position_ratio", "topLongShortPositionRatio"),
                       ("taker_buy_sell_ratio", "takerlongshortRatio")):
        d = soft(lambda p=path: get(f"{BIN_FUT}/futures/data/{p}?symbol={symbol}&period=4h&limit=42")) or []
        if d:
            key = [k for k in d[-1] if "Ratio" in k or "ratio" in k or "BuySell" in k]
            if name == "taker_buy_sell_ratio":
                vals = [float(x["buySellRatio"]) for x in d]
                out[name] = {"current": _f(vals[-1], 3), "mean_42bar": _f(st.mean(vals), 3),
                             "reading": "主动买盘占优" if vals[-1] > 1 else "主动卖盘占优",
                             "_legend": ">1 主动买量大于主动卖量"}
            else:
                # 用 longShortRatio 换算多头占比（下标名在不同 endpoint 不稳定），缺失时回退 longAccount
                longs = []
                for x in d:
                    if x.get("longShortRatio") not in (None, ""):
                        r = float(x["longShortRatio"])
                        longs.append(r / (1 + r))
                    elif x.get("longAccount") not in (None, ""):
                        longs.append(float(x["longAccount"]))
                if longs:
                    out[name] = {"current_long_pct": _f(longs[-1] * 100, 1),
                                 "mean_long_pct": _f(st.mean(longs) * 100, 1),
                                 "percentile": _f(sum(1 for v in longs if v <= longs[-1]) / len(longs) * 100, 0),
                                 "reading": ("多头拥挤(反向风险)" if longs[-1] > 0.6 else "空头拥挤(反向风险)" if longs[-1] < 0.4 else "多空相对均衡"),
                                 "_source_field": "longShortRatio→多头占比"}
    return out


# ───────────────────────── dogdoing.ai（社交热度 / OI背离 / 链上安全） ─────────────────────────

DD = "https://dogdoing.ai"
CN_NAMES = {"BTC": ("比特币",), "ETH": ("以太坊",), "SOL": ("Solana", "索拉纳"),
            "BNB": ("币安币",), "XRP": ("瑞波",), "DOGE": ("狗狗币",), "ADA": ("艾达",)}


def _sym_match(item_symbol: str, base: str) -> bool:
    s = (item_symbol or "").upper()
    b = base.upper()
    return s == b or s.endswith(b) or s.lstrip("1000").lstrip("1000000") == b or b.endswith(s)


def dogdoing_block(base: str, news_limit: int = 5) -> dict:
    """dogdoing.ai 聚合层：社交热度、OI 背离、情绪、资讯（全部为其公开 JSON 接口）"""
    out: dict = {"_source": "dogdoing.ai 公开 JSON 接口（Binance Skills Hub 聚合层）"}

    hype = soft(lambda: get(f"{DD}/api/square-hype")) or {}
    items = hype.get("data") or []
    if items:
        out["square_hype_leaderboard_size"] = len(items)
        mine = [x for x in items if _sym_match(x.get("symbol"), base)]
        if mine:
            m = mine[0]
            out["square_hype"] = {
                "score": m.get("score"), "sources": m.get("sources"),
                "rank": items.index(m) + 1, "of_total": len(items),
                "price_change_pct": _f(m.get("priceChangePct"), 2),
                "volume_24h_usd": _f(m.get("volume24h"), 0),
                "reading": ("社交热度居前(拥挤/情绪风险)" if (items.index(m) + 1) <= 5
                            else "社交热度中等" if (items.index(m) + 1) <= 15 else "社交热度靠后(关注度低)"),
            }
        else:
            out["square_hype"] = {"score": None, "rank": None,
                                  "reading": f"未进入广场热度榜 Top{len(items)}（低关注度）"}
        out["square_hype_top5"] = [{"symbol": x.get("symbol"), "score": x.get("score"),
                                    "change_pct": _f(x.get("priceChangePct"), 1)} for x in items[:5]]

    oid = soft(lambda: get(f"{DD}/api/oi-divergence")) or {}
    oitems = oid.get("data") or []
    if oitems:
        mine = [x for x in oitems if _sym_match(x.get("symbol"), base)]
        out["oi_divergence_market_top"] = [{"symbol": x.get("symbol"),
                                            "oi_change_pct": _f(x.get("oiChangePct"), 1),
                                            "price_change_pct": _f(x.get("priceChangePct"), 1),
                                            "divergence_ratio": _f(x.get("divergenceRatio"), 2)}
                                           for x in oitems[:5]]
        if mine:
            m = mine[0]
            dr = _f(m.get("divergenceRatio"), 2)
            out["oi_divergence"] = {
                "oi_change_pct": _f(m.get("oiChangePct"), 1), "price_change_pct": _f(m.get("priceChangePct"), 1),
                "divergence_ratio": dr,
                "reading": ("OI 增速远快于价格：杠杆快速堆积(易引发双向挤压)" if (dr or 0) > 3
                            else "OI 与价格同步：增仓健康" if (dr or 0) > 1 else "OI 变化小于价格：以平仓/换手为主"),
                "_legend": "divergenceRatio = OI变化% ÷ 价格变化%，>3 视为异常增仓",
            }
        else:
            out["oi_divergence"] = {"divergence_ratio": None, "reading": "未进入 OI 背离监控榜（无异常增仓）"}

    fg = soft(lambda: get(f"{DD}/api/fear-greed"))
    if isinstance(fg, dict) and fg.get("value") is not None:
        out["fear_greed_dogdoing"] = {"value": fg.get("value"), "label": fg.get("label")}

    ticks = soft(lambda: get(f"{DD}/api/market-tickers")) or {}
    tdata = ticks.get("data") or []
    mine = [x for x in tdata if _sym_match(x.get("symbol"), base)]
    if mine:
        out["price_cross_check"] = {"dogdoing_price": _f(mine[0].get("price"), 6),
                                    "dogdoing_change_24h_pct": _f(mine[0].get("change"), 2),
                                    "_purpose": "与交易所 API 现价交叉校验，偏差过大说明数据陈旧"}

    news = soft(lambda: get(f"{DD}/api/news")) or {}
    nlist = news.get("data") or []
    if nlist:
        aliases = CN_NAMES.get(base.upper(), ())
        def hit(n):
            t = f"{n.get('title','')} {n.get('body','')}"
            return base.upper() in t.upper() or any(al in t for al in aliases)
        mine_news = [n for n in nlist if hit(n)]
        out["news"] = [{"title": n.get("title"), "source": n.get("source"), "url": n.get("url"),
                        "published_utc": datetime.fromtimestamp(int(n["publishedAt"]) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if n.get("publishedAt") else None,
                        "summary": (n.get("body") or "")[:300]}
                       for n in (mine_news or nlist)[:news_limit]]
        out["news_scope"] = "标的直接相关" if mine_news else "无标的直接新闻，返回全市场要闻"
    return out


# ───────────────────────── 全局 / 代币经济 ─────────────────────────

CG_IDS = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "bnb": "binancecoin",
          "xrp": "ripple", "doge": "dogecoin", "ada": "cardano", "trx": "tron",
          "link": "chainlink", "avax": "avalanche-2", "ltc": "litecoin", "dot": "polkadot",
          "sui": "sui", "apt": "aptos", "arb": "arbitrum", "op": "optimism",
          "pepe": "pepe", "wif": "dogwifcoin", "ton": "the-open-network", "near": "near",
          "hype": "hyperliquid", "ena": "ethena", "ondo": "ondo-finance", "tao": "bittensor"}


def cg_block(symbol: str) -> dict:
    base = symbol.upper().replace("USDT", "").replace("USDC", "").lower()
    cid = CG_IDS.get(base, base)
    m = soft(lambda: get(f"{CG}/coins/markets?vs_currency=usd&symbols={base}"))
    out: dict = {}
    if isinstance(m, list) and m:
        c = m[0]
        circ, tot, maxs = c.get("circulating_supply"), c.get("total_supply"), c.get("max_supply")
        fdv = c.get("fully_diluted_valuation") or c.get("market_cap")
        out = {
            "coingecko_id": c.get("id"), "name": c.get("name"), "rank": c.get("market_cap_rank"),
            "market_cap_usd": _f(c.get("market_cap"), 0), "fdv_usd": _f(fdv, 0),
            "market_cap_to_fdv_pct": _f(c.get("market_cap", 0) / fdv * 100, 1) if fdv else None,
            "circulating_supply": _f(circ, 0), "total_supply": _f(tot, 0), "max_supply": _f(maxs, 0),
            "unlock_overhang_pct": _f((1 - (circ or 0) / maxs) * 100, 1) if (circ and maxs) else None,
            "ath_usd": _f(c.get("ath"), 6), "ath_change_pct": _f(c.get("ath_change_percentage"), 2),
            "atl_usd": _f(c.get("atl"), 6),
            "price_change_7d_pct": _f(c.get("price_change_percentage_7d_in_currency"), 2),
            "price_change_30d_pct": _f(c.get("price_change_percentage_30d_in_currency"), 2),
            "total_volume_usd_24h": _f(c.get("total_volume"), 0),
            "volume_to_mcap_pct": _f(c.get("total_volume", 0) / c.get("market_cap", 1) * 100, 2),
            "note": "unlock_overhang_pct = (1 − 流通量/最大供应量)×100，为解锁抛压代理（免费接口无精确解锁日历）",
        }
    return out


def global_block() -> dict:
    g = soft(lambda: get(f"{CG}/global"))
    d = (g or {}).get("data") or {}
    out = {}
    if d:
        out = {
            "total_market_cap_usd": _f((d.get("total_market_cap") or {}).get("usd"), 0),
            "total_market_cap_change_24h_pct": _f(d.get("market_cap_change_percentage_24h_usd"), 2),
            "btc_dominance_pct": _f((d.get("market_cap_percentage") or {}).get("btc"), 2),
            "eth_dominance_pct": _f((d.get("market_cap_percentage") or {}).get("eth"), 2),
            "total_volume_usd_24h": _f((d.get("total_volume") or {}).get("usd"), 0),
            "active_cryptocurrencies": d.get("active_cryptocurrencies"),
        }
    # 稳定币净流入代理：market_cap 变化 ≈ 净发行
    flow = {}
    for cid in ("tether", "usd-coin"):
        mc = soft(lambda c=cid: get(f"{CG}/coins/{c}/market_chart?vs_currency=usd&days=30&interval=daily"))
        pts = ((mc or {}).get("market_caps") or [])
        if len(pts) > 3:
            vals = [p[1] for p in pts]
            flow[cid] = {"mcap_usd": _f(vals[-1], 0), "change_7d_pct": _pct(vals[-1], vals[-8]) if len(vals) > 8 else None,
                         "change_30d_pct": _pct(vals[-1], vals[0]),
                         "net_issuance_30d_usd": _f(vals[-1] - vals[0], 0)}
    if flow:
        tot7 = sum(v["change_7d_pct"] for v in flow.values() if v.get("change_7d_pct") is not None) / max(len(flow), 1)
        tot30 = sum(v["net_issuance_30d_usd"] for v in flow.values() if v.get("net_issuance_30d_usd") is not None)
        out["stablecoin_proxy"] = {"per_asset": flow, "mean_7d_change_pct": _f(tot7, 3),
                                   "net_issuance_30d_usd": _f(tot30, 0),
                                   "interpretation": "稳定币市值=场内购买力代理：净增发=潜在增量买盘，缩水=资金离场或赎回",
                                   "method": "USDT+USDC 市值变化代理净发行（价格≈1 美元）"}
    return out


def fear_greed() -> dict:
    d = soft(lambda: get("https://api.alternative.me/fng/?limit=30"))
    data = (d or {}).get("data") or []
    if not data:
        return {}
    vals = [(int(x["value"]), x["value_classification"], x["timestamp"]) for x in data]
    cur = vals[0]
    arr = [v for v, _, _ in vals]
    return {"current": cur[0], "classification": cur[1],
            "date_utc": datetime.fromtimestamp(int(cur[2]), timezone.utc).strftime("%Y-%m-%d"),
            "mean_30d": _f(st.mean(arr), 1), "min_30d": min(arr), "max_30d": max(arr),
            "percentile_30d": _f(sum(1 for v in arr if v <= cur[0]) / len(arr) * 100, 0),
            "legend": "0-24 极度恐惧 / 25-49 恐惧 / 50-74 贪婪 / 75-100 极度贪婪"}


# ───────────────────────── 风险与仓位（TypeSafe 0.98 判定的模型） ─────────────────────────

def risk_framework(atr_pct_4h: float, price: float, equity: float, risks=(0.5, 1.0, 2.0),
                   stop_mults=(1.5, 2.0, 3.0), mmr: float = 0.005, fee_bps: float = 5.0,
                   exchange_leverage: float = 5.0) -> dict:
    """波动率目标仓位 + 交易所杠杆上限 + 爆仓距离校验

    两个杠杆必须分清：
      · 隐含杠杆 = 名义头寸 / 权益  → 由『风险预算 ÷ 止损距离』决定，是仓位大小的结果
      · 交易所杠杆 L_set          → 交易者在平台上设定的倍数，决定保证金占用与强平距离
    两者不是一回事：隐含杠杆 0.43x 时，仍可在平台上设 10x 杠杆（占用保证金更少），但强平距离随之缩短。
    """
    if not atr_pct_4h or not price:
        return {"error": "缺少 ATR(4H) 数据，无法构建仓位模型"}
    rows = []
    for rp in risks:
        for sm in stop_mults:
            stop_pct = atr_pct_4h * sm / 100                      # 止损距离（价格百分比）
            risk_usd = equity * rp / 100
            notional = risk_usd / stop_pct if stop_pct > 0 else None
            if not notional:
                continue
            implicit_lev = notional / equity
            max_lev = 1 / (1.5 * stop_pct + mmr + fee_bps / 10000 * 2)
            if exchange_leverage <= 1.0:
                liq_dist_pct, liq_price, safe = None, None, True
                liq_note = "交易所杠杆≤1x：无借入，不存在强平（公式不适用）"
                margin = notional  # 全额
            else:
                liq_dist_pct = (1 / exchange_leverage) - mmr - fee_bps / 10000 * 2
                liq_price = price * (1 - liq_dist_pct)
                safe = liq_dist_pct >= 1.5 * stop_pct
                liq_note = (f"强平距离{_f(liq_dist_pct*100,2)}% ≥ 1.5×止损距离({_f(stop_pct*100*1.5,2)}%)"
                            if safe else
                            f"强平距离{_f(liq_dist_pct*100,2)}% < 1.5×止损距离({_f(stop_pct*100*1.5,2)}%)：强平先于止损")
                margin = notional / exchange_leverage
            margin_over = margin > equity
            rows.append({
                "risk_budget_pct": rp, "stop_atr_mult": sm,
                "stop_distance_pct": _f(stop_pct * 100, 2),
                "stop_price": _f(price * (1 - stop_pct), 6),
                "risk_usd": _f(risk_usd, 2),
                "notional_usd": _f(notional, 0),
                "implied_leverage_x": _f(implicit_lev, 2),
                "exchange_leverage_x": _f(exchange_leverage, 2),
                "margin_required_usd": _f(margin, 2),
                "margin_check": "保证金超出权益，该组合不可执行" if margin_over else "保证金在权益内",
                "est_liquidation_price_long": _f(liq_price, 6),
                "liq_distance_pct": _f(liq_dist_pct * 100, 2) if liq_dist_pct else None,
                "liq_check": ("通过（" + liq_note + "）") if safe else "不通过：" + liq_note,
                "max_safe_leverage_x": _f(max_lev, 2),
            })
    return {
        "inputs": {"equity_usd": equity, "atr_pct_4h": _f(atr_pct_4h, 3), "ref_price": _f(price, 6),
                   "maintenance_margin_rate": mmr, "taker_fee_bps": fee_bps,
                   "exchange_leverage_assumed": exchange_leverage},
        "table": rows,
        "rules": [
            "名义头寸 = 账户权益 × 每笔风险预算 ÷ 止损距离（止损距离 = N×ATR(4H)%）",
            "隐含杠杆 = 名义头寸 ÷ 权益（仓位大小的结果）；交易所杠杆 = 平台设定值（决定保证金占用与强平距离）——两者不是一回事",
            "杠杆不是收益放大器而是强平距离决定项：必须满足 强平距离 ≥ 1.5 × 止损距离",
            "资金费率为持有成本：持仓成本 = 年化资金费率 ÷ 365 × 持有天数 × 名义头寸",
            "周末与 UTC 低流动时段的实际滑点会高于回测假设，开仓规模需按 dashboard 的滑点模拟折减",
        ],
    }


# ───────────────────────── 仪表盘 ─────────────────────────

def _n(v, fmt: str = ",.0f") -> str:
    """None 安全格式化：取数失败显示 — 而不是崩溃"""
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return "—"


def render_dashboard(d: dict) -> str:
    m, px = d["meta"], d["price"]
    L = [f"### 加密交易仪表盘 · {m['symbol']}（{m.get('cg_name') or m['base']}）",
         f"> 数据时间: {m['as_of']} · 场所: {', '.join(m['venues'])} · 24/7 连续交易（无收盘/无隔夜缺口）", ""]
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 标记价 / 现货价 | {px.get('mark_price')} / {px.get('spot_price')}（基差 {px.get('basis_bps')} bps） |")
    L.append(f"| 24h 涨跌 / 高低 | {px.get('change_24h_pct')}% · 高 {px.get('high_24h')} / 低 {px.get('low_24h')} |")
    t4, t1, tD = d["technicals"]["h4"], d["technicals"]["h1"], d["technicals"]["d1"]
    L.append(f"| 趋势 4H / 1H / 1D | {t4.get('trend')} / {t1.get('trend')} / {tD.get('trend')} |")
    L.append(f"| RSI14 4H / 1H / 1D | {t4.get('rsi14')} / {t1.get('rsi14')} / {tD.get('rsi14')} |")
    L.append(f"| ATR14 4H（占价）/ 1D | {t4.get('atr14')}（{t4.get('atr_pct')}%）/ {tD.get('atr_pct')}% |")
    L.append(f"| MACD 4H | {t4['macd'].get('state')}（柱 {t4['macd'].get('hist')}） |")
    L.append(f"| 布林带宽 4H / 分位 | {t4['bollinger'].get('width')} / {t4.get('bb_width_percentile')}% |")
    vol = d.get("volatility", {})
    L.append(f"| 已实现波动（日/年化） | {vol.get('realized_vol_daily_pct')}% / {vol.get('realized_vol_annualized_365_pct')}% |")
    L.append(f"| 波动率分位（1年） | {vol.get('vol_percentile_1y')}% |")
    fb = d.get("funding", {}).get("binance", {})
    if fb:
        L.append(f"| 资金费率（8h / 年化） | {fb.get('last_funding_rate_8h')} / {fb.get('funding_annualized_pct')}% |")
        L.append(f"| 资金费率历史均值（年化/正费率占比） | {fb.get('mean_annualized_pct')}% / {fb.get('pct_periods_positive')}% |")
    if d.get("funding", {}).get("htx"):
        L.append(f"| HTX 资金费率（8h / 年化） | {d['funding']['htx'].get('funding_rate_8h')} / {d['funding']['htx'].get('funding_annualized_pct')}% |")
    if d.get("funding", {}).get("cross_venue_spread_mean_annualized_pct") is not None:
        L.append(f"| 跨场所资金费率差（历史均值口径，HTX−Binance 年化） | {d['funding']['cross_venue_spread_mean_annualized_pct']}% |")
    oi = d.get("positioning", {}).get("open_interest", {})
    if oi:
        L.append(f"| 未平仓量（USD / 24h 变化 / 30日分位） | {_n(oi.get('current_usd'))} / {oi.get('change_24h_pct')}% / {oi.get('percentile_30d')}% |")
    if d.get("positioning", {}).get("oi_price_quadrant"):
        L.append(f"| 价量-OI 结构 | {d['positioning']['oi_price_quadrant']['reading']} |")
    if d.get("positioning", {}).get("long_short_account_ratio"):
        ls = d["positioning"]["long_short_account_ratio"]
        L.append(f"| 多空账户比（多头占比） | {ls.get('current_long_pct')}%（{ls.get('reading')}） |")
    if d.get("positioning", {}).get("taker_buy_sell_ratio"):
        L.append(f"| 主动买卖比 | {d['positioning']['taker_buy_sell_ratio'].get('current')}（{d['positioning']['taker_buy_sell_ratio'].get('reading')}） |")
    bm = d.get("liquidity", {}).get("binance_futures") or d.get("liquidity", {}).get("binance_spot") or {}
    if bm:
        L.append(f"| 买卖价差 / ±1% 深度（买/卖） | {bm.get('spread_bps')} bps / "
                 f"{_n((bm.get('depth_usd_within') or {}).get('1pct', {}).get('bid'))} · "
                 f"{_n((bm.get('depth_usd_within') or {}).get('1pct', {}).get('ask'))} USD |")
        s = (bm.get("slippage_sim") or {}).get("100k", {}).get("buy")
        if s:
            L.append(f"| 10万美元市价买入滑点 | {s.get('slippage_bps')} bps（均价 {s.get('avg_price')}） |")
    rs = d.get("relative_strength", {})
    if rs.get("vs_btc", {}).get("30d"):
        L.append(f"| 相对 BTC（30d 超额） | {rs['vs_btc']['30d'].get('excess_pct')}% |")
    if rs.get("corr_beta_btc_90d"):
        L.append(f"| 与 BTC 相关性 / Beta（90d） | {rs['corr_beta_btc_90d'].get('corr')} / {rs['corr_beta_btc_90d'].get('beta')} |")
    fg = d.get("fear_greed", {})
    if fg:
        L.append(f"| 恐惧贪婪指数 | {fg.get('current')}（{fg.get('classification')}，30日分位 {fg.get('percentile_30d')}%） |")
    g = d.get("global_market", {})
    if g:
        L.append(f"| 全市场市值 / BTC 占比 | {_n(g.get('total_market_cap_usd'))} USD / {g.get('btc_dominance_pct')}% |")
    if g.get("stablecoin_proxy"):
        L.append(f"| 稳定币 30 日净发行（代理） | {_n(g['stablecoin_proxy'].get('net_issuance_30d_usd'))} USD |")
    tkn = d.get("tokenomics", {})
    if tkn:
        L.append(f"| 市值 / FDV / 解锁悬顶 | {_n(tkn.get('market_cap_usd'))} / {_n(tkn.get('fdv_usd'))} / {tkn.get('unlock_overhang_pct')}% |")
    ag = d.get("aggregator", {})
    sh = ag.get("square_hype")
    if sh:
        L.append(f"| 币安广场社交热度（dogdoing） | 评分 {sh.get('score')} · 排名 {sh.get('rank')}/{sh.get('of_total')} · {sh.get('reading')} |")
    od = ag.get("oi_divergence")
    if od:
        L.append(f"| OI 背离（dogdoing） | 背离度 {od.get('divergence_ratio')}（OI {od.get('oi_change_pct')}% vs 价格 {od.get('price_change_pct')}%）· {od.get('reading')} |")
    cc = ag.get("price_cross_check")
    if cc and cc.get("verdict"):
        L.append(f"| 聚合源价格交叉校验 | 偏差 {cc.get('deviation_vs_exchange_pct')}%（{cc.get('verdict')}） |")
    if ag.get("news"):
        L.append(f"| 相关资讯 | {len(ag['news'])} 条（{ag.get('news_scope')}） |")
    rk = d.get("risk_framework", {})
    if rk.get("table"):
        r = [x for x in rk["table"] if x["risk_budget_pct"] == 1.0 and x["stop_atr_mult"] == 2.0]
        if r:
            r = r[0]
            L.append(f"| 仓位模型（风险1%、2×ATR止损） | 名义 {_n(r['notional_usd'])} USD · 杠杆 {r['implied_leverage_x']}x · 止损 {r['stop_distance_pct']}% · 强平校验 {r['liq_check']} |")
    return "\n".join(L)


# ───────────────────────── 主流程 ─────────────────────────

def build(symbol: str, venue: str, equity: float, use_cg: bool, risk: float, use_dd: bool = True,
          exchange_leverage: float = 5.0) -> dict:
    symbol = symbol.upper().replace("-", "").replace("/", "")
    base = symbol.replace("USDT", "").replace("USDC", "")
    htx_contract = f"{base}-USDT"
    gaps: list[str] = []

    spot_1h = spot_4h = d1 = pd.DataFrame()
    for mk in ("spot", "futures"):
        try:
            spot_1h = klines("binance", symbol, "1h", 900, mk)
            spot_4h = klines("binance", symbol, "4h", 1000, mk)
            d1 = klines("binance", symbol, "1d", 400, mk)
            if len(spot_4h):
                break
        except Exception:                                  # noqa: BLE001
            continue
    if not len(spot_4h):
        raise SystemExit(
            f"[FATAL] {symbol} 在币安现货与永续均无 K 线。请确认：\n"
            f"  · 代码格式（USDT 本位永续用 BTCUSDT 形式）\n"
            f"  · 该币是否只在币安 Alpha/链上/其他交易所（本引擎仅覆盖币安与 HTX 上线的交易对）")
    try:
        fut_4h = klines("binance", symbol, "4h", 400, "futures")
    except Exception:                                     # noqa: BLE001
        fut_4h = pd.DataFrame()
        gaps.append("币安永续 4H K线未取到（可能仅有现货）")

    price = _f(spot_4h["Close"].iloc[-1], 6) if len(spot_4h) else None
    if price is None:
        raise SystemExit(f"[FATAL] 无法获取 {symbol} 行情，请确认为币安/HTX 上线交易对（如 BTCUSDT）")

    pi = soft(lambda: get(f"{BIN_FUT}/fapi/v1/premiumIndex?symbol={symbol}")) or {}
    tk24 = soft(lambda: get(f"{BIN_SPOT}/api/v3/ticker/24hr?symbol={symbol}")) or {}
    mark = _f(pi.get("markPrice"), 6) or price
    basis_bps = _f((mark / price - 1) * 10000, 1) if price else None

    # 波动率
    ret = spot_4h["Close"].pct_change().dropna()
    daily_ret = d1["Close"].pct_change().dropna() if len(d1) > 30 else ret
    apct = atr_pct_series(spot_4h)
    vol = {
        "atr_pct_4h": _f(apct.iloc[-1], 3),
        "realized_vol_daily_pct": _f(daily_ret.tail(30).std() * 100, 3) if len(daily_ret) > 5 else None,
        "realized_vol_annualized_365_pct": _f(daily_ret.tail(30).std() * math.sqrt(365) * 100, 2) if len(daily_ret) > 5 else None,
        "realized_vol_7d_annualized_pct": _f(daily_ret.tail(7).std() * math.sqrt(365) * 100, 2) if len(daily_ret) > 7 else None,
        "vol_percentile_1y": None,
        "annualization_note": "加密 7x24，年化用 √365（股票用 √252）",
    }
    av = atr_pct_series(spot_4h).dropna()
    if len(av) > 100:
        vol["vol_percentile_1y"] = _f((av <= av.iloc[-1]).mean() * 100, 0)

    # 相对强弱 vs BTC
    rs = {}
    if base.upper() == "BTC":
        rs["_note"] = "标的即为 BTC，不做相对 BTC 强弱与相关性计算"
    try:
        btc = klines("binance", "BTCUSDT", "1d", 220, "spot")
        if base.upper() != "BTC" and len(btc) > 120 and len(d1) > 120:
            for w, n in (("7d", 7), ("30d", 30), ("90d", 90)):
                s_ = _pct(d1["Close"].iloc[-1], d1["Close"].iloc[-1 - n])
                b_ = _pct(btc["Close"].iloc[-1], btc["Close"].iloc[-1 - n])
                rs.setdefault("vs_btc", {})[w] = {"asset_pct": s_, "btc_pct": b_,
                                                  "excess_pct": _f((s_ or 0) - (b_ or 0), 2) if s_ is not None and b_ is not None else None}
            j = pd.concat([d1["Close"].pct_change().rename("a"), btc["Close"].pct_change().rename("b")], axis=1).dropna().tail(90)
            if len(j) > 20:
                rs["corr_beta_btc_90d"] = {"corr": _f(j["a"].corr(j["b"]), 3),
                                           "beta": _f(j["a"].cov(j["b"]) / j["b"].var(), 3) if j["b"].var() else None}
    except Exception:                                      # noqa: BLE001
        gaps.append("BTC 相对强弱计算失败")

    # 场所流动性
    liq = {}
    for name, url in (("binance_spot", f"{BIN_SPOT}/api/v3/depth?symbol={symbol}&limit=500"),
                      ("binance_futures", f"{BIN_FUT}/fapi/v1/depth?symbol={symbol}&limit=500")):
        dd = soft(lambda u=url: get(u))
        if dd:
            liq[name] = book_metrics(dd.get("bids", []), dd.get("asks", []), price)
    hd = soft(lambda: get(f"{HTX}/linear-swap-ex/market/depth?contract_code={htx_contract}&type=step0"))
    ticks = ((hd or {}).get("tick") or {})
    if ticks.get("bids") and ticks.get("asks"):
        liq["htx_futures"] = book_metrics(ticks["bids"], ticks["asks"], price)
    elif venue in ("both", "htx"):
        gaps.append(f"HTX 深度未取到（{htx_contract} 可能未上线）")

    vol_hour = volume_by_hour(spot_1h)
    if not vol_hour:
        gaps.append("分时段流动性分布未取到")

    positioning = positioning_block(symbol)
    oi_now = (positioning.get("open_interest") or {}).get("current_usd") or 0

    d = {
        "meta": {"symbol": symbol, "base": base, "venues": (["Binance"] if venue == "binance" else
                                                            ["HTX"] if venue == "htx" else ["Binance", "HTX"]),
                 "asset_class": "加密货币（现货 + USDT本位永续）",
                 "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                 "last_bar_4h_utc": str(spot_4h.index[-1])[:19] if len(spot_4h) else None,
                 "trading_hours": "7x24 连续，无收盘与隔夜缺口",
                 "data_sources": "Binance 公开API + HTX 公开API + CoinGecko + alternative.me",
                 "reviewed_by": "TypeSafe jev-1.13.0（可迁移性1.50 / 仓位模型0.98 / 场所风险0.95）"},
        "price": {"mark_price": mark, "spot_price": price, "basis_bps": basis_bps,
                  "change_24h_pct": _f(tk24.get("priceChangePercent"), 2),
                  "high_24h": _f(tk24.get("highPrice"), 6), "low_24h": _f(tk24.get("lowPrice"), 6),
                  "quote_volume_24h_usd": _f(tk24.get("quoteVolume"), 0),
                  "trades_24h": _f(tk24.get("count"), 0)},
        "technicals": {"h4": tf_block(spot_4h, "4H(主决策)"),
                       "h1": tf_block(spot_1h, "1H(执行)", ema_pairs=((20, 50),), n=600),
                       "d1": tf_block(d1, "1D(结构)", n=300),
                       "futures_h4": tf_block(fut_4h, "永续4H") if len(fut_4h) > 60 else {},
                       "levels_4h": swing_levels(spot_4h),
                       "liquidation_magnets": liquidation_magnet(spot_4h, oi_now)},
        "volatility": vol,
        "funding": funding_block(symbol, htx_contract if venue in ("both", "htx") else None),
        "positioning": positioning,
        "liquidity": {**liq, "session_profile": vol_hour},
        "relative_strength": rs,
        "fear_greed": fear_greed(),
        "risk_framework": risk_framework(vol.get("atr_pct_4h") or 0, price, equity, exchange_leverage=exchange_leverage),
    }
    if use_cg:
        d["tokenomics"] = cg_block(symbol) or {}
        d["global_market"] = global_block() or {}
        if not d["global_market"].get("total_market_cap_usd"):
            gaps.append("CoinGecko 全市场数据未取到（/global 失败，BTC 占比与稳定币流量缺失）")
        d["meta"]["cg_name"] = (d["tokenomics"] or {}).get("name")
        if not d["tokenomics"]:
            gaps.append("CoinGecko 代币经济未取到（该币可能无收录）")
    if use_dd:
        d["aggregator"] = dogdoing_block(base)
        ag = d["aggregator"]
        cc = ag.get("price_cross_check") or {}
        if cc.get("dogdoing_price") and price:
            dev = abs(cc["dogdoing_price"] / price - 1) * 100
            cc["deviation_vs_exchange_pct"] = _f(dev, 3)
            cc["verdict"] = "一致" if dev < 1 else "偏差>1%：该聚合源价格可能陈旧，以交易所价为准"
        if not ag.get("news"):
            gaps.append("dogdoing 资讯接口未返回内容")
    d["data_quality"] = {
        "missing_or_degraded": gaps,
        "no_fundamentals": "加密资产无财报/分析师覆盖/机构13F；本快照以『资金费率+OI+多空比+社交热度+流动性+代币经济』替代股票的基本面输入，禁止编造财报类结论。",
        "aggregator_source": "社交热度/OI背离/资讯来自 dogdoing.ai 公开 JSON 接口；价格类结论一律以交易所 API 为准。",
        "liquidation_data": "无免费清算明细接口，清算聚集区为代理指标（结构极点+高量区+整数关口）。",
        "onchain_data": "链上数据（交易所余额/巨鲸转账）需付费接口，本快照用稳定币净发行+流通量/解锁悬顶作代理。",
    }
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="加密交易数据引擎（TypeSafe 判决驱动）")
    ap.add_argument("symbol", help="交易对，如 BTCUSDT / SOLUSDT / PEPEUSDT")
    ap.add_argument("--venue", default="both", choices=["both", "binance", "htx"])
    ap.add_argument("--equity", type=float, default=10_000.0, help="账户权益(USD)，用于仓位模型")
    ap.add_argument("--risk", type=float, default=1.0, help="每笔风险预算百分比，仅用于打印摘要")
    ap.add_argument("--leverage", type=float, default=5.0, help="交易所杠杆设定值，用于保证金与强平距离计算（默认5x）")
    ap.add_argument("--no-cg", action="store_true", help="跳过 CoinGecko（更快）")
    ap.add_argument("--no-dogdoing", action="store_true", help="跳过 dogdoing.ai 聚合层（社交热度/OI背离/资讯）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    out_dir = a.out or os.path.join(os.path.expanduser("~"), "crypto_snapshots")
    os.makedirs(out_dir, exist_ok=True)
    d = build(a.symbol, a.venue, a.equity, not a.no_cg, a.risk, not a.no_dogdoing, a.leverage)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe = d["meta"]["symbol"]
    jp = os.path.join(out_dir, f"snapshot_{safe}_{day}.json")
    mp = os.path.join(out_dir, f"dashboard_{safe}_{day}.md")
    json.dump(d, open(jp, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
    md = render_dashboard(d)
    open(mp, "w", encoding="utf-8").write(md + "\n")
    print(md)
    print(f"\n[OK] JSON   → {jp}\n[OK] 仪表盘 → {mp}")
    if d["data_quality"]["missing_or_degraded"]:
        print(f"[!] 缺口: {d['data_quality']['missing_or_degraded']}")
    t4 = d["technicals"]["h4"]
    print(f"[i] 4H趋势={t4.get('trend')} | 4H ATR%={t4.get('atr_pct')} | "
          f"资金费率年化={d['funding'].get('binance',{}).get('funding_annualized_pct')}% | "
          f"OI 24h={d['positioning'].get('open_interest',{}).get('change_24h_pct')}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
