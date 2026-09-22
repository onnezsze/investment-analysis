#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
equity_snapshot.py — 投资分析 Prompt 合集 的「数据落地引擎」

给定一个股票代码，一次性抓取并计算两套 Prompt 需要的全部真实数据：
  · 基本面: 估值倍数 / 3年财务 / 盈利质量 / 财务健康 / 现金流 / 管理层线索
  · 技术面: 日线·周线·4小时 三周期指标 / ATR / 布林带宽度分位 / 量价分布 / 关键价位 / 斐波那契
  · 相对强弱: 对标本地市场基准指数
  · 情绪与资金: 分析师评级·目标价 / 空头兴趣 / 机构持仓
  · 同业对标: 自动发现同行业 Top 公司并取估值倍数

输出:
  <out>/snapshot_<TICKER>_<YYYYMMDD>.json   完整结构化数据(供 agent 写报告)
  <out>/dashboard_<TICKER>_<YYYYMMDD>.md    「关键数据仪表盘」Markdown(可直接粘进报告)

用法:
  python3 equity_snapshot.py NVDA
  python3 equity_snapshot.py 0700.HK --peers 9988.HK,3690.HK,JD
  python3 equity_snapshot.py 600519.SS --peers 000858.SZ,000568.SZ,600809.SH
  python3 equity_snapshot.py BTC-USD            # 加密资产也支持
  python3 equity_snapshot.py NVDA --no-peers    # 跳过同业抓取(更快)

数据源: yfinance (Yahoo Finance)。所有数字均为真实抓取，缺失字段进入 data_quality.missing，
绝不填 0、绝不编造。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

try:
    import numpy as np
    import pandas as pd
    import yfinance as yf
except ImportError as e:  # pragma: no cover
    sys.exit(f"[FATAL] 缺少依赖 {e}. 请先: pip install yfinance pandas numpy")


# ─────────────────────────── 工具 ───────────────────────────

def _f(x, nd=4):
    """安全转 float，NaN/None/非数 → None"""
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    except (TypeError, ValueError):
        return None


def _pct(a, b, nd=2):
    """(a/b - 1) * 100"""
    try:
        a, b = float(a), float(b)
        if b == 0 or math.isnan(a) or math.isnan(b):
            return None
        return round((a / b - 1) * 100, nd)
    except (TypeError, ValueError):
        return None


def _slug(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def market_of(ticker: str) -> dict:
    t = ticker.upper()
    if t.endswith(".HK"):
        return {"label": "港股 / HKEX", "benchmark": "^HSI", "bench_label": "恒生指数",
                "currency": "HKD", "peer_suffix": ".HK", "alt_suffixes": [".HK"]}
    if t.endswith((".SS", ".SZ", ".SH")):
        return {"label": "A股", "benchmark": "510300.SS", "bench_label": "沪深300ETF(510300)",
                "currency": "CNY", "peer_suffix": ".SS", "alt_suffixes": [".SS", ".SZ"]}
    if t.endswith(("-USD", "-USDT", "-EUR")):
        return {"label": "加密资产", "benchmark": "BTC-USD", "bench_label": "BTC",
                "currency": "USD", "peer_suffix": "-USD"}
    if t.endswith((".T", ".KS", ".TW", ".L", ".DE", ".PA", ".AS", ".AX", ".TO", ".SI", ".NS")):
        return {"label": f"海外市场 {t.rsplit('.',1)[-1]}", "benchmark": "ACWI", "bench_label": "MSCI全球(ACWI)",
                "currency": "USD", "peer_suffix": ""}
    return {"label": "美股 / US", "benchmark": "SPY", "bench_label": "标普500(SPY)",
            "currency": "USD", "peer_suffix": ""}


def _hist(tk_obj, **kw) -> pd.DataFrame:
    """取历史行情，带重试；自动丢掉尾部 NaN K线"""
    last_err = None
    for i in range(3):
        try:
            df = tk_obj.history(**kw)
            if df is not None and len(df):
                df = df.dropna(subset=["Close"])
                if len(df):
                    return df
            last_err = "empty frame"
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (i + 1))
    if last_err:
        raise RuntimeError(f"history failed: {last_err}")
    return pd.DataFrame()


# ─────────────────────── 技术指标计算 ───────────────────────

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return (100 - 100 / (1 + rs))


def macd(series: pd.Series, fast=12, slow=26, sig=9) -> pd.DataFrame:
    ef = series.ewm(span=fast, adjust=False).mean()
    es = series.ewm(span=slow, adjust=False).mean()
    line = ef - es
    signal = line.ewm(span=sig, adjust=False).mean()
    return pd.DataFrame({"line": line, "signal": signal, "hist": line - signal})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(series: pd.Series, n=20, k=2) -> pd.DataFrame:
    mid = series.rolling(n).mean()
    sd = series.rolling(n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    return pd.DataFrame({"upper": up, "mid": mid, "lower": lo,
                         "width": (up - lo) / mid.replace(0, np.nan)})


def _trend_label(price, ma_short, ma_long) -> str:
    if price is None or ma_short is None or ma_long is None:
        return "数据不足"
    if price > ma_short > ma_long:
        return "多头排列(价 > 短均 > 长均)"
    if price < ma_short < ma_long:
        return "空头排列(价 < 短均 < 长均)"
    if price > ma_long:
        return "长期趋势之上，短期震荡"
    return "长期趋势之下，短期震荡"


def tf_block(df: pd.DataFrame, label: str, ma_pairs=((20, 50), (50, 200)), n_bar=200) -> dict:
    """单周期指标块"""
    if df is None or len(df) < 30:
        return {"timeframe": label, "error": "样本不足"}
    d = df.tail(n_bar)
    c = d["Close"]
    out = {
        "timeframe": label,
        "bars": int(len(d)),
        "last_close": _f(c.iloc[-1], 4),
        "last_bar_date": str(d.index[-1])[:16],
        "rsi14": _f(rsi(c).iloc[-1], 2),
        "macd": {
            "line": _f(macd(c)["line"].iloc[-1], 4),
            "signal": _f(macd(c)["signal"].iloc[-1], 4),
            "hist": _f(macd(c)["hist"].iloc[-1], 4),
            "cross": ("金叉/柱体转正" if (macd(c)["hist"].iloc[-1] > 0 >= macd(c)["hist"].iloc[-2])
                      else "死叉/柱体转负" if (macd(c)["hist"].iloc[-1] < 0 <= macd(c)["hist"].iloc[-2])
                      else ("柱体为正(多头动能)" if macd(c)["hist"].iloc[-1] > 0 else "柱体为负(空头动能)")),
        },
        "atr14": _f(atr(d).iloc[-1], 4),
    }
    bb = bollinger(c)
    out["bollinger"] = {
        "upper": _f(bb["upper"].iloc[-1]), "mid": _f(bb["mid"].iloc[-1]),
        "lower": _f(bb["lower"].iloc[-1]), "width": _f(bb["width"].iloc[-1], 6),
    }
    for s, l in ma_pairs:
        if len(c) >= l:
            ms, ml = _f(c.rolling(s).mean().iloc[-1]), _f(c.rolling(l).mean().iloc[-1])
            out[f"ma{s}"] = ms
            out[f"ma{l}"] = ml
            if ms:
                out[f"dist_ma{s}_pct"] = _pct(c.iloc[-1], ms)
            if ml:
                out[f"dist_ma{l}_pct"] = _pct(c.iloc[-1], ml)
    out["trend"] = _trend_label(_f(c.iloc[-1], 4), out.get("ma20") or out.get("ma50"), out.get("ma50") or out.get("ma200"))
    return out


# ─────────────────────── 价位 / 量价结构 ───────────────────────

def swing_levels(df: pd.DataFrame, k: int = 5, lookback: int = 260) -> dict:
    d = df.tail(lookback)
    hi, lo, cl = d["High"], d["Low"], d["Close"]
    price = float(cl.iloc[-1])
    piv_h, piv_l = [], []
    for i in range(k, len(d) - k):
        if hi.iloc[i] == hi.iloc[i - k:i + k + 1].max():
            piv_h.append(float(hi.iloc[i]))
        if lo.iloc[i] == lo.iloc[i - k:i + k + 1].min():
            piv_l.append(float(lo.iloc[i]))

    def cluster(vals, tol=0.015):
        out = []
        for v in sorted(vals):
            if out and abs(v - out[-1][-1]) / max(out[-1][-1], 1e-9) < tol:
                out[-1].append(v)
            else:
                out.append([v])
        return [round(sum(g) / len(g), 2) for g in out]

    res = sorted([c for c in cluster(piv_h) if c > price])[:4]
    sup = sorted([c for c in cluster(piv_l) if c < price], reverse=True)[:4]
    return {"resistance_zones": res, "support_zones": sup,
            "note": "基于 260 根日线枢轴点(k=5)聚类，容差 1.5%"}


def volume_profile(df: pd.DataFrame, sessions: int = 126, bins: int = 40) -> dict:
    d = df.tail(sessions)
    if len(d) < 20:
        return {}
    lo, hi = float(d["Low"].min()), float(d["High"].max())
    if hi <= lo:
        return {}
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    idx = np.clip(np.digitize(typical.values, edges) - 1, 0, bins - 1)
    for i, v in zip(idx, d["Volume"].values):
        vol[i] += float(v)
    total = vol.sum()
    order = np.argsort(vol)[::-1]
    price = float(d["Close"].iloc[-1])

    def rng(i):
        return [round(float(edges[i]), 2), round(float(edges[i + 1]), 2)]

    nodes = [{"range": rng(int(i)), "share_pct": _f(vol[i] / total * 100, 2)} for i in order[:3]]
    vpoc = nodes[0]["range"] if nodes else None
    # 70% 价值区
    acc, sel = 0.0, []
    for i in order:
        sel.append(int(i)); acc += vol[i]
        if acc / total >= 0.7:
            break
    va = [round(float(edges[min(sel)]), 2), round(float(edges[max(sel) + 1]), 2)]
    return {"sessions": int(len(d)), "vpoc_range": vpoc, "high_volume_nodes": nodes,
            "value_area_70pct": va,
            "price_vs_vpoc": ("价格在密集区上方(密集区=潜在支撑)" if vpoc and price > vpoc[1]
                              else "价格在密集区下方(密集区=潜在阻力)" if vpoc and price < vpoc[0]
                              else "价格正处于密集区内(方向不明)")}


def fib_levels(df: pd.DataFrame, lookback: int = 252) -> dict:
    d = df.tail(lookback)
    if len(d) < 30:
        return {}
    hi, lo = float(d["High"].max()), float(d["Low"].min())
    price = float(d["Close"].iloc[-1])
    ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
    # 若价格更靠近高点 → 视为上升段回撤；否则视为下跌段反弹
    up_leg = (hi - price) < (price - lo)
    lv = {f"{r:.3f}": round(hi - (hi - lo) * r, 2) for r in ratios} if up_leg else \
         {f"{r:.3f}": round(lo + (hi - lo) * r, 2) for r in ratios}
    return {"window_bars": int(len(d)), "swing_low": round(lo, 2), "swing_high": round(hi, 2),
            "direction": "上升段回撤(高点在近期)" if up_leg else "下跌段反弹(低点在近期)",
            "levels": lv,
            "extension_127_2": round(lo + (hi - lo) * 1.272, 2) if up_leg else round(hi - (hi - lo) * 1.272, 2),
            "extension_161_8": round(lo + (hi - lo) * 1.618, 2) if up_leg else round(hi - (hi - lo) * 1.618, 2)}


# ─────────────────────── 基本面 ───────────────────────

def _row(df: pd.DataFrame, names, col) -> float | None:
    if df is None or df.empty or col not in df.columns:
        return None
    for n in names:
        if n in df.index:
            v = df.loc[n, col]
            if isinstance(v, pd.Series):
                v = v.iloc[0]
            f = _f(v, 2)
            if f is not None:
                return f
    return None


def financials_history(t: yf.Ticker, years: int = 3) -> list[dict]:
    try:
        inc = t.income_stmt
    except Exception:
        inc = pd.DataFrame()
    try:
        cf = t.cashflow
    except Exception:
        cf = pd.DataFrame()
    try:
        bs = t.balance_sheet
    except Exception:
        bs = pd.DataFrame()
    if inc is None or inc.empty:
        return []
    cols = list(inc.columns)[:years]
    out = []
    for i, col in enumerate(cols):
        fy = str(col)[:10]
        rev = _row(inc, ["Total Revenue", "Operating Revenue", "Revenue"], col)
        gp = _row(inc, ["Gross Profit"], col)
        op = _row(inc, ["Operating Income", "Total Operating Income As Reported", "EBIT"], col)
        ni = _row(inc, ["Net Income", "Net Income Common Stockholders",
                        "Net Income From Continuing Operation Net Minority Interest"], col)
        eps = _row(inc, ["Diluted EPS", "Basic EPS"], col)
        ocf = _row(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], col)
        capex = _row(cf, ["Capital Expenditure", "Capital Expenditures"], col)
        equity = _row(bs, ["Stockholders Equity", "Total Stockholder Equity",
                           "Common Stock Equity", "Total Equity Gross Minority Interest"], col)
        rec = {
            "fiscal_year": fy, "revenue": rev, "gross_profit": gp, "operating_income": op,
            "net_income": ni, "diluted_eps": _f(eps, 3) if eps is not None else None,
            "operating_cash_flow": ocf,
            "capex": _f(abs(capex), 2) if capex is not None else None,
            "free_cash_flow": _f((ocf - abs(capex)), 2) if (ocf is not None and capex is not None) else None,
            "stockholders_equity": equity,
            "gross_margin_pct": _f(gp / rev * 100, 2) if (gp is not None and rev) else None,
            "operating_margin_pct": _f(op / rev * 100, 2) if (op and rev) else None,
            "net_margin_pct": _f(ni / rev * 100, 2) if (ni and rev) else None,
            "capex_pct_of_revenue": _f(abs(capex) / rev * 100, 2) if (capex and rev) else None,
            "ocf_to_net_income": _f(ocf / ni, 2) if (ocf and ni) else None,
            "roe_pct": _f(ni / equity * 100, 2) if (ni and equity) else None,
        }
        # 同比
        if i + 1 < len(cols):
            nxt = cols[i + 1]
            prev_rev = _row(inc, ["Total Revenue", "Operating Revenue", "Revenue"], nxt)
            prev_ni = _row(inc, ["Net Income", "Net Income Common Stockholders"], nxt)
            rec["revenue_yoy_pct"] = _pct(rev, prev_rev)
            rec["net_income_yoy_pct"] = _pct(ni, prev_ni)
        out.append(rec)
    return out


def valuation_block(info: dict, price, hist: pd.DataFrame, fin: list[dict] | None = None) -> dict:
    def g(*keys):
        for k in keys:
            v = _f(info.get(k))
            if v is not None:
                return v
        return None
    ocf = g("operatingCashflow")
    cap = g("marketCap")
    # 股息率：优先用 dividendRate/价格（yfinance 的 dividendYield 字段口径随版本变化，不可信）
    rate = g("dividendRate")
    div_yield = _f(rate / price * 100, 2) if (rate and price) else (
        g("dividendYield") if g("dividendYield") is not None and g("dividendYield") < 20 else None)
    # TTM 自由现金流：用 OCF(TTM) − 最近一个财年 Capex，比 info.freeCashflow 更贴近口径
    capex_fy = (fin or [{}])[0].get("capex") if fin else None
    fcf_ttm = _f(ocf - capex_fy, 2) if (ocf is not None and capex_fy is not None) else None
    return {
        "market_cap": g("marketCap"),
        "market_cap_display": _human(g("marketCap")),
        "pe_ttm": g("trailingPE"),
        "pe_forward": g("forwardPE"),
        "peg": g("trailingPegRatio", "pegRatio"),
        "pb": g("priceToBook"),
        "ps_ttm": g("priceToSalesTrailing12Months"),
        "ev_ebitda": g("enterpriseToEbitda"),
        "ev_revenue": g("enterpriseToRevenue"),
        "enterprise_value": g("enterpriseValue"),
        "dividend_rate_per_share": rate,
        "dividend_yield_pct": div_yield,
        "fcf_ttm_ocf_minus_capex": fcf_ttm,
        "fcf_ttm_basis": "OCF(TTM) − 最近财年Capex（Yahoo 的 freeCashflow 字段与 OCF 口径不一致，故不用）",
        "fcf_yield_pct": _f(fcf_ttm / cap * 100, 2) if (fcf_ttm and cap) else None,
        "earnings_yield_pct": _f(100 / g("trailingPE"), 2) if g("trailingPE") else None,
        "ocf_ttm": ocf,
    }


def _human(v):
    if v is None:
        return None
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v/div:.2f}{unit}"
    return f"{v:.0f}"


def health_block(info: dict, fin: list[dict]) -> dict:
    def g(*keys):
        for k in keys:
            v = _f(info.get(k))
            if v is not None:
                return v
        return None
    return {
        "current_ratio": g("currentRatio"),
        "quick_ratio": g("quickRatio"),
        "debt_to_equity": g("debtToEquity"),
        "total_debt": g("totalDebt"),
        "total_cash": g("totalCash"),
        "net_debt": (_f((g("totalDebt") or 0) - (g("totalCash") or 0), 2)
                     if (g("totalDebt") is not None or g("totalCash") is not None) else None),
        "roe_ttm_pct": _f((g("returnOnEquity") or 0) * 100, 2) if g("returnOnEquity") is not None else None,
        "roa_ttm_pct": _f((g("returnOnAssets") or 0) * 100, 2) if g("returnOnAssets") is not None else None,
        "gross_margin_ttm_pct": _f((g("grossMargins") or 0) * 100, 2) if g("grossMargins") is not None else None,
        "operating_margin_ttm_pct": _f((g("operatingMargins") or 0) * 100, 2) if g("operatingMargins") is not None else None,
        "net_margin_ttm_pct": _f((g("profitMargins") or 0) * 100, 2) if g("profitMargins") is not None else None,
        "ebitda_margin_pct": _f((g("ebitdaMargins") or 0) * 100, 2) if g("ebitdaMargins") is not None else None,
        "revenue_growth_yoy_pct": _f((g("revenueGrowth") or 0) * 100, 2) if g("revenueGrowth") is not None else None,
        "earnings_growth_yoy_pct": _f((g("earningsGrowth") or 0) * 100, 2) if g("earningsGrowth") is not None else None,
        "shares_outstanding": g("sharesOutstanding"),
        "float_shares": g("floatShares"),
        "beta": g("beta"),
        "analyst_note_5y_growth": None,
    }


def roic_approx(fin: list[dict], info: dict) -> dict:
    """近似 ROIC = EBIT*(1-21%) / (总债务 + 股东权益 - 现金)"""
    out = {}
    cash = _f(info.get("totalCash"))
    debt = _f(info.get("totalDebt"))
    for r in fin[:1]:
        equity = r.get("stockholders_equity")
        ebit = r.get("operating_income")
        if None in (equity, ebit):
            continue
        invested = (debt or 0) + equity - (cash or 0)
        if invested > 0:
            out = {"fiscal_year": r["fiscal_year"], "ebit": ebit, "invested_capital_approx": _f(invested, 2),
                   "roic_approx_pct": _f(ebit * (1 - 0.21) / invested * 100, 2),
                   "method": "EBIT×(1-21%) / (总债务+股东权益-现金)，近似值"}
    return out


# ─────────────────────── 同业 / 情绪 ───────────────────────

def _yahoo_similar(ticker: str) -> list[str]:
    """Yahoo「同市场推荐」接口，返回同市场可比标的（港股给 .HK，A股给 .SS/.SZ）"""
    import urllib.request
    url = f"https://query1.finance.yahoo.com/v6/finance/recommendationsbysymbol/{ticker}"
    for i in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.load(urllib.request.urlopen(req, timeout=20))
            r = d.get("finance", {}).get("result", [])
            if r:
                return [x.get("symbol") for x in r[0].get("recommendedSymbols", []) if x.get("symbol")]
        except Exception:  # noqa: BLE001
            time.sleep(1.0 + i)
    return []


def discover_peers(t: yf.Ticker, info: dict, self_tk: str, limit: int = 6, explicit=None) -> tuple[list, str, dict]:
    """双源同业发现：①Yahoo 同市场推荐 ②行业 Top 公司(优先本市场)
    返回 (symbols, 说明, {symbol: 来源})"""
    if explicit:
        return list(explicit)[:8], "用户指定(--peers)", {s.upper(): "用户指定" for s in explicit}

    mk = market_of(self_tk)
    base = self_tk.upper().split(".")[0]
    suffixes = mk.get("alt_suffixes") or ([mk["peer_suffix"]] if mk.get("peer_suffix") else [])
    src: dict[str, str] = {}
    industry_first: list[str] = []
    similar_after: list[str] = []

    ind = info.get("industry") or info.get("sector")
    if ind:
        for key in [k for k in (_slug(info.get("industry")), _slug(info.get("sector"))) if k]:
            try:
                o = yf.Industry(key) if key == _slug(info.get("industry")) else yf.Sector(key)
                tc = getattr(o, "top_companies", None)
                if tc is None or not len(tc):
                    continue
                syms = [s for s in list(tc.index) if s.upper() not in (self_tk.upper(), base)]
                local = [s for s in syms if any(s.upper().endswith(x) for x in suffixes)] if suffixes else syms
                pick = local or syms
                for s in pick:
                    if s not in industry_first and s.upper() != base:
                        industry_first.append(s)
                        src[s] = "行业Top(本市场)" if local else "行业Top(跨市场·慎用)"
                break
            except Exception:  # noqa: BLE001
                continue

    for s in _yahoo_similar(self_tk):
        if s.upper() not in (self_tk.upper(), base) and s not in industry_first and s not in similar_after:
            similar_after.append(s)
            src[s] = "同市场推荐(用户也关注·非严格同业)"

    cands = industry_first + similar_after
    if not cands:
        return [], (f"同业自动发现失败：行业「{ind or '未知'}」无候选，请用 --peers 手工指定", {})
    cross = sum(1 for s in cands[:limit] if "跨市场" in src.get(s, ""))
    how = (f"双源合并：①行业Top公司(严格同业，排序在前，A股/港股优先本市场) ②Yahoo同市场推荐(非严格同业)。"
           f"跨市场候选 {cross} 个；写报告时只用①做估值比对，②仅作情绪参考，必要时用 --peers 覆盖")
    return cands[:limit], how, src


def peer_metrics(symbols: list[str], src: dict | None = None) -> list[dict]:
    rows = []
    for s in symbols:
        try:
            i = yf.Ticker(s).info or {}
            row = {
                "ticker": s,
                "source": (src or {}).get(s),
                "name": i.get("shortName") or i.get("longName"),
                "currency": i.get("currency"),
                "price": _f(i.get("currentPrice") or i.get("regularMarketPrice"), 2),
                "market_cap_display": _human(_f(i.get("marketCap"))),
                "pe_ttm": _f(i.get("trailingPE"), 2),
                "pe_forward": _f(i.get("forwardPE"), 2),
                "ps_ttm": _f(i.get("priceToSalesTrailing12Months"), 2),
                "ev_ebitda": _f(i.get("enterpriseToEbitda"), 2),
                "pb": _f(i.get("priceToBook"), 2),
                "net_margin_ttm_pct": _f((_f(i.get("profitMargins")) or 0) * 100, 2) if _f(i.get("profitMargins")) is not None else None,
                "revenue_growth_yoy_pct": _f((_f(i.get("revenueGrowth")) or 0) * 100, 2) if _f(i.get("revenueGrowth")) is not None else None,
                "roe_ttm_pct": _f((_f(i.get("returnOnEquity")) or 0) * 100, 2) if _f(i.get("returnOnEquity")) is not None else None,
            }
            flags = []
            if row["pe_ttm"] is not None and row["pe_ttm"] <= 0:
                flags.append("PE为负(亏损)")
            if row["ps_ttm"] is not None and 0 < row["ps_ttm"] < 1 and (row["net_margin_ttm_pct"] or 0) > 20:
                flags.append("PS与净利率矛盾(疑似币种/口径不一致,勿用于估值对比)")
            if row["ev_ebitda"] is not None and (row["ev_ebitda"] > 200 or row["ev_ebitda"] < 0):
                flags.append("EV/EBITDA异常(疑似币种/口径不一致,勿用于估值对比)")
            if row["ps_ttm"] is not None and 0 < row["ps_ttm"] < 0.2:
                flags.append("PS异常低(疑似币种/口径不一致,勿用于估值对比)")
            if row["revenue_growth_yoy_pct"] is not None and abs(row["revenue_growth_yoy_pct"]) > 200:
                flags.append("增速极端(需核实基数效应)")
            if row["pe_ttm"] is None and row["ps_ttm"] is None:
                flags.append("估值数据缺失")
            if flags:
                row["data_flags"] = flags
            rows.append(row)
            time.sleep(0.4)
        except Exception as e:  # noqa: BLE001
            rows.append({"ticker": s, "source": (src or {}).get(s), "error": str(e)[:80]})
    return rows


def summarize_peers(rows: list[dict]) -> dict:
    """剔除口径异常行后，给出可用于估值对标的中位数"""
    import statistics as st

    def clean(p):
        f = " ".join(p.get("data_flags") or [])
        return ("口径不一致" not in f) and ("估值数据缺失" not in f) and not p.get("error")

    good = [p for p in rows if clean(p)]

    def med(key, sample=None):
        vals = [p[key] for p in (sample or good) if p.get(key) is not None]
        return _f(st.median(vals), 2) if vals else None

    return {
        "clean_sample": [p.get("ticker") for p in good],
        "excluded_rows": [{"ticker": p.get("ticker"), "reason": p.get("data_flags") or p.get("error")}
                          for p in rows if p not in good],
        "median_pe_ttm": med("pe_ttm"),
        "median_pe_forward": med("pe_forward"),
        "median_ps_ttm": med("ps_ttm"),
        "median_ev_ebitda": med("ev_ebitda"),
        "median_pb": med("pb"),
        "median_net_margin_ttm_pct": med("net_margin_ttm_pct"),
        "median_revenue_growth_yoy_pct": med("revenue_growth_yoy_pct"),
        "median_roe_ttm_pct": med("roe_ttm_pct"),
        "note": "中位数已剔除带 data_flags 的异常行；报告里的估值对比只能用 clean_sample 内的公司。",
    }


def sentiment_block(t: yf.Ticker, info: dict) -> dict:
    out = {
        "recommendation_key": info.get("recommendationKey"),
        "recommendation_mean": _f(info.get("recommendationMean"), 2),
        "num_analysts": _f(info.get("numberOfAnalystOpinions"), 0),
        "target_mean": _f(info.get("targetMeanPrice"), 2),
        "target_high": _f(info.get("targetHighPrice"), 2),
        "target_low": _f(info.get("targetLowPrice")),
        "target_median": _f(info.get("targetMedianPrice"), 2),
        "held_pct_institutions": _f((_f(info.get("heldPercentInstitutions")) or 0) * 100, 2) if _f(info.get("heldPercentInstitutions")) is not None else None,
        "held_pct_insiders": _f((_f(info.get("heldPercentInsiders")) or 0) * 100, 2) if _f(info.get("heldPercentInsiders")) is not None else None,
        "shares_short": _f(info.get("sharesShort")),
        "short_pct_of_float": _f((_f(info.get("shortPercentOfFloat")) or 0) * 100, 2) if _f(info.get("shortPercentOfFloat")) is not None else None,
        "short_ratio_days_to_cover": _f(info.get("shortRatio"), 2),
        "date_short_interest": info.get("dateShortInterest"),
    }
    try:
        uh = t.upgrades_downgrades
        if uh is not None and len(uh):
            uh = uh.copy()
            uh.index = pd.to_datetime(uh.index)
            recent = uh[uh.index >= (pd.Timestamp.now(tz=uh.index.tz) - pd.Timedelta(days=90))]
            out["rating_actions_90d"] = [{"date": str(i)[:10], "firm": r.get("Firm"), "from": r.get("FromGrade"),
                                          "to": r.get("ToGrade"), "action": r.get("Action")}
                                         for i, r in recent.head(15).iterrows()]
    except Exception:  # noqa: BLE001
        out["rating_actions_90d"] = []
    try:
        ih = t.institutional_holders
        if ih is not None and len(ih):
            out["top_institutions"] = [{"holder": str(r.get("Holder")), "pct_held": _f(r.get("% Out"), 3),
                                        "shares": _f(r.get("Shares"), 0), "date": str(r.get("Date Reported"))[:10],
                                        "value": _human(_f(r.get("Value")))}
                                       for _, r in ih.head(10).iterrows()]
    except Exception:  # noqa: BLE001
        out["top_institutions"] = []
    return out


def relative_strength(stock_hist: pd.DataFrame, bench_tk: str) -> dict:
    out = {"benchmark": bench_tk}
    try:
        b = _hist(yf.Ticker(bench_tk), period="1y", interval="1d", auto_adjust=True)
    except Exception as e:  # noqa: BLE001
        return {"benchmark": bench_tk, "error": f"基准数据抓取失败: {e}"}
    windows = {"1m": 21, "3m": 63, "6m": 126, "1y": 252}
    for name, n in windows.items():
        try:
            sc = stock_hist["Close"]
            bc = b["Close"]
            if len(sc) > n and len(bc) > n:
                s_ret = _pct(sc.iloc[-1], sc.iloc[-1 - n])
                b_ret = _pct(bc.iloc[-1], bc.iloc[-1 - n])
                out[name] = {"stock_pct": s_ret, "bench_pct": b_ret,
                             "excess_pct": _f((s_ret or 0) - (b_ret or 0), 2) if (s_ret is not None and b_ret is not None) else None}
        except Exception:  # noqa: BLE001
            continue
    # 上涨/下跌日捕获率（Beta 行为）
    try:
        j = pd.concat([stock_hist["Close"].pct_change().rename("s"),
                       b["Close"].pct_change().rename("b")], axis=1).dropna().tail(120)
        up = j[j["b"] > 0]
        dn = j[j["b"] < 0]
        out["up_capture_pct"] = _f((up["s"].mean() / up["b"].mean()) * 100, 1) if len(up) > 10 else None
        out["down_capture_pct"] = _f((dn["s"].mean() / dn["b"].mean()) * 100, 1) if len(dn) > 10 else None
    except Exception:  # noqa: BLE001
        pass
    return out


# ─────────────────────── 仪表盘 Markdown ───────────────────────

def render_dashboard(d: dict) -> str:
    m = d["meta"]; q = d["quote"]; t = d["technicals"]["daily"]; v = d["valuation"]
    rs = d.get("relative_strength", {}); s = d.get("sentiment", {})
    L = []
    cur = m.get("currency", "")
    L.append(f"### 关键数据仪表盘 · {m['name']} ({m['ticker']})")
    L.append(f"> 数据时间: {m['as_of']} · 来源: Yahoo Finance (yfinance) · 市场: {m['market']['label']}")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 当前价格 | {cur} {q.get('price')} |")
    L.append(f"| 52周高 / 低 | {q.get('high_52w')} / {q.get('low_52w')} (距高点 {q.get('pct_from_52w_high')}%) |")
    L.append(f"| MA50 / MA200 | {t.get('ma50')} / {t.get('ma200')} (价格距 MA50 {t.get('dist_ma50_pct')}%, 距 MA200 {t.get('dist_ma200_pct')}%) |")
    L.append(f"| RSI(14) 日线 | {t.get('rsi14')} |")
    L.append(f"| MACD(12,26,9) | DIF {t['macd'].get('line')} / DEA {t['macd'].get('signal')} / 柱 {t['macd'].get('hist')} ({t['macd'].get('cross')}) |")
    L.append(f"| ATR(14) | {t.get('atr14')} (占价格 {_f((t.get('atr14') or 0)/q.get('price')*100,2) if q.get('price') else None}%) |")
    L.append(f"| 布林带宽度 | {(t.get('bollinger') or {}).get('width')} (1年分位 {d['technicals'].get('bb_width_percentile_1y')}%) |")
    L.append(f"| 20日均量 | {q.get('avg_volume_20d')} | 最新量 | {q.get('volume_latest')} (相对 {q.get('rel_volume_vs_20d')}x) |")
    if v:
        L.append(f"| 市值 | {v.get('market_cap_display')} {cur} |")
        L.append(f"| PE(TTM) / Forward PE | {v.get('pe_ttm')} / {v.get('pe_forward')} |")
        L.append(f"| PB / PS(TTM) / EV-EBITDA | {v.get('pb')} / {v.get('ps_ttm')} / {v.get('ev_ebitda')} |")
    if rs.get("3m"):
        L.append(f"| 相对强弱 vs {m['market']['bench_label']} (3个月) | 个股 {rs['3m']['stock_pct']}% vs 基准 {rs['3m']['bench_pct']}% → 超额 {rs['3m']['excess_pct']}% |")
    if s.get("target_mean"):
        L.append(f"| 分析师目标价(均值) | {s.get('target_mean')} ({s.get('num_analysts')}人, 评级 {s.get('recommendation_key')}) |")
    if s.get("short_pct_of_float") is not None:
        L.append(f"| 空头占流通股 | {s.get('short_pct_of_float')}% (回补天数 {s.get('short_ratio_days_to_cover')}) |")
    return "\n".join(L)


# ─────────────────────── 主流程 ───────────────────────

def build(ticker: str, peers=None, no_peers=False) -> dict:
    ticker = ticker.strip().upper()
    mk = market_of(ticker)
    t = yf.Ticker(ticker)
    missing = []
    try:
        info = t.info or {}
    except Exception as e:  # noqa: BLE001
        info = {}
        missing.append(f"info 全部字段抓取失败: {str(e)[:80]}")
    if not info.get("shortName") and not info.get("longName"):
        try:
            info = dict(info, **{k: v for k, v in (t.get_info() or {}).items()})
        except Exception:  # noqa: BLE001
            pass

    daily = _hist(t, period="2y", interval="1d", auto_adjust=True)
    price = _f(daily["Close"].iloc[-1], 4) or _f(info.get("currentPrice") or info.get("regularMarketPrice"), 4)
    if price is None:
        raise SystemExit(f"[FATAL] 无法获取 {ticker} 价格，请确认代码（美股直接代码 / 港股 0700.HK / A股 600519.SS）")

    win52 = daily.tail(252)
    high52, low52 = _f(win52["High"].max(), 2), _f(win52["Low"].min(), 2)

    # 周线 / 4小时
    weekly = daily.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min",
                                          "Close": "last", "Volume": "sum"}).dropna()
    try:
        h1 = _hist(t, period="3mo", interval="1h", auto_adjust=True)
        h4 = h1.resample("4h").agg({"Open": "first", "High": "max", "Low": "min",
                                    "Close": "last", "Volume": "sum"}).dropna()
    except Exception:  # noqa: BLE001
        h4 = pd.DataFrame()
        missing.append("4小时K线(部分市场不支持)")

    tech_daily = tf_block(daily, "日线(中期)")
    bb_series = bollinger(daily["Close"])["width"].dropna()
    bb_pct = _f((bb_series <= bb_series.iloc[-1]).mean() * 100, 0) if len(bb_series) > 60 else None

    fin = financials_history(t, 3)
    if not fin:
        missing.append("三年财务报表(income_stmt 为空)")

    blk = {
        "meta": {
            "ticker": ticker,
            "name": info.get("shortName") or info.get("longName") or ticker,
            "long_name": info.get("longName"),
            "market": mk,
            "exchange": info.get("exchange") or info.get("fullExchangeName"),
            "currency": info.get("currency") or mk.get("currency"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "business_summary": (info.get("longBusinessSummary") or "")[:1500] or None,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "last_trading_day": str(daily.index[-1])[:10],
            "data_source": "Yahoo Finance via yfinance — 真实抓取，未做任何人工润色",
        },
        "quote": {
            "price": price,
            "prev_close": _f(daily["Close"].iloc[-2], 4) if len(daily) > 1 else None,
            "day_change_pct": _pct(daily["Close"].iloc[-1], daily["Close"].iloc[-2]) if len(daily) > 1 else None,
            "high_52w": high52, "low_52w": low52,
            "pct_from_52w_high": _pct(price, high52),
            "pct_above_52w_low": _pct(price, low52),
            "avg_volume_20d": _f(daily["Volume"].tail(20).mean(), 0),
            "volume_latest": _f(daily["Volume"].iloc[-1], 0),
            "rel_volume_vs_20d": _f(daily["Volume"].iloc[-1] / max(daily["Volume"].tail(20).mean(), 1), 2),
            "avg_volume_3m": _f(daily["Volume"].tail(63).mean(), 0),
        },
        "valuation": valuation_block(info, price, daily, fin),
        "health_and_growth": health_block(info, fin),
        "roic_approx": roic_approx(fin, info),
        "financials_3y": fin,
        "technicals": {
            "daily": tech_daily,
            "weekly": tf_block(weekly, "周线(长期)", n_bar=104),
            "h4": (tf_block(h4, "4小时(短期)", ma_pairs=((20, 50),), n_bar=180) if len(h4) > 60 else {"timeframe": "4小时(短期)", "error": "数据不足"}),
            "bb_width_percentile_1y": bb_pct,
            "levels": swing_levels(daily),
            "volume_profile": volume_profile(daily),
            "fibonacci": fib_levels(daily),
            "atr_position_sizing": {
                "atr14": tech_daily.get("atr14"),
                "stop_2atr": _f((price or 0) - 2 * (tech_daily.get("atr14") or 0), 2) if tech_daily.get("atr14") else None,
                "stop_3atr": _f((price or 0) - 3 * (tech_daily.get("atr14") or 0), 2) if tech_daily.get("atr14") else None,
                "note": "以最新收盘价−2/3×ATR(14) 的机械止损参考，agent 需结合结构位调整",
            },
        },
        "relative_strength": relative_strength(daily, mk["benchmark"]),
        "sentiment": sentiment_block(t, info),
    }

    if not no_peers:
        syms, how, src = discover_peers(t, info, ticker, explicit=peers)
        prows = peer_metrics(syms, src) if syms else []
        blk["peers"] = {"discovery": how, "symbols": syms, "metrics": prows,
                        "summary": summarize_peers(prows) if prows else {}}
        if not syms:
            missing.append("同业对标(自动发现失败，请用 --peers 指定)")

    for k, path in (("pe_ttm", "valuation.pe_ttm"), ("rsi14", "technicals.daily.rsi14")):
        v = blk
        for p in path.split("."):
            v = (v or {}).get(p) if isinstance(v, dict) else None
        if v is None:
            missing.append(path)
    blk["data_quality"] = {
        "missing_or_degraded": missing,
        "free_float_pct": _f((_f(info.get("floatShares")) or 0) / (_f(info.get("sharesOutstanding")) or 1) * 100, 1) if info.get("floatShares") else None,
        "note": "本文件所有数字均来自 Yahoo Finance 实时抓取；missing_or_degraded 内的字段在报告中必须显式标注「数据缺失」，禁止用 0 或推测值替代。",
    }
    return blk


def main():
    ap = argparse.ArgumentParser(description="投资分析数据落地引擎")
    ap.add_argument("ticker", help="股票代码，如 NVDA / 0700.HK / 600519.SS / BTC-USD")
    ap.add_argument("--peers", default=None, help="手工指定同业，逗号分隔")
    ap.add_argument("--no-peers", action="store_true", help="跳过同业抓取")
    ap.add_argument("--out", default=None, help="输出目录，默认 <skill>/../reports_snapshot 或当前目录")
    args = ap.parse_args()

    out_dir = args.out or os.path.join(os.path.expanduser("~"), "investment_snapshots")
    os.makedirs(out_dir, exist_ok=True)

    peers = [p.strip().upper() for p in args.peers.split(",")] if args.peers else None
    d = build(args.ticker, peers=peers, no_peers=args.no_peers)

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe = d["meta"]["ticker"].replace("/", "_")
    jpath = os.path.join(out_dir, f"snapshot_{safe}_{day}.json")
    mpath = os.path.join(out_dir, f"dashboard_{safe}_{day}.md")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, default=str)
    md = render_dashboard(d)
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(md + "\n")

    print(md)
    print()
    print(f"[OK] JSON   → {jpath}")
    print(f"[OK] 仪表盘 → {mpath}")
    if d["data_quality"]["missing_or_degraded"]:
        print(f"[!] 缺失/降级字段: {d['data_quality']['missing_or_degraded']}")
    r = d["technicals"]["daily"]
    print(f"[i] 三周期趋势: 周线={d['technicals']['weekly'].get('trend')} | 日线={r.get('trend')} | "
          f"4H={(d['technicals']['h4'].get('trend') if 'trend' in d['technicals']['h4'] else 'N/A')}")


if __name__ == "__main__":
    main()
