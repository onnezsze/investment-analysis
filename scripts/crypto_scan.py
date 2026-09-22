#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_scan.py — 标的发现器：从 dogdoing 挖交易机会，输出可交易候选榜（含 tradfi）

数据源（仅允许的两类）：dogdoing.ai 公开 JSON 接口 + 交易所公开 API（Binance / HTX）

候选来源（dogdoing）：
  · square-hype    币安广场社交热度（含热度分与 24h 涨跌）
  · oi-divergence  持仓量异动/背离监控
  · gainers/losers 涨跌幅榜（资金主线）
  · hotspots       Alpha/Meme 热点话题及其代币
  · us-stocks      tradfi 叙事来源（Mag7 等，配新闻）

可交易性校验：
  · 加密 → Binance USDT-M 永续（自动处理 1000/1000000 乘数）或 HTX USDT 本位永续
  · tradfi → 仅 HTX 有（股票/指数/商品/外汇）；Binance 无对应合约
  · 运行时补齐每个候选的 24h 名义成交额、资金费率、OI 变化，并给出流动性/成本粗判

输出：~/crypto_snapshots/watchlist_<YYYYMMDD>.json + 终端榜单
用法:
  python3 crypto_scan.py                       # 加密 + tradfi 全扫
  python3 crypto_scan.py --top 12               # 只留前 12 名
  python3 crypto_scan.py --only crypto|tradfi   # 只看某一类
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DD = "https://dogdoing.ai"
UA = {"User-Agent": "Mozilla/5.0"}
OUT_DIR = os.path.join(os.path.expanduser("~"), "crypto_snapshots")

TRADFI_HINTS = ("XAU", "XAG", "GOLD", "SILVER", "OIL", "BRENT", "SPX500", "NASDAQ", "US500",
                "AAPL", "GOOGL", "MSFT", "META", "NVDA", "AMZN", "TSLA", "COIN", "MSTR",
                "SAMSUNG", "EURUSD", "GBPUSD", "USDJPY", "PAXG", "XAUT", "USOIL")


def get(url: str, timeout: int = 25):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except Exception:                                        # noqa: BLE001
            time.sleep(1.0 * (i + 1))
    return {}


def dd(path: str) -> list:
    d = get(f"{DD}{path}")
    return (d or {}).get("data") or [] if isinstance(d, dict) else []


# ───────────────────── 可交易性 ─────────────────────

def binance_universe() -> dict:
    info = get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    out = {}
    for s in (info.get("symbols") or []):
        ct = s.get("contractType")
        if ct in ("PERPETUAL", "TRADIFI_PERPETUAL") and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            out[s["symbol"]] = {"venue": "Binance", "contract": s["symbol"], "base": s.get("baseAsset"),
                                "price_precision": s.get("pricePrecision"), "contract_type": ct,
                                "asset_class": "tradfi" if ct == "TRADIFI_PERPETUAL" else "crypto",
                                "underlying_type": s.get("underlyingType")}
    return out


def htx_universe() -> dict:
    d = get("https://api.hbdm.com/linear-swap-api/v1/swap_contract_info")
    out = {}
    for c in (d.get("data") or []):
        code = c.get("contract_code")
        if not code or not code.endswith("-USDT") or len(code.split("-")) != 2:
            continue          # 排除交割/季度合约
        out[code] = {"venue": "HTX", "contract": code, "base": c.get("symbol"),
                     "contract_size": c.get("contract_size"), "price_tick": c.get("price_tick"),
                     "min_order_vol": c.get("min_order_vol") or 1, "max_leverage": c.get("max_leverage")}
    return out


def resolve(base: str, binance: dict, htx: dict) -> list[dict]:
    """把一个代币/标的名解析成可交易合约（可能同时存在于两个场所，也可能带 1000 乘数）"""
    b = (base or "").upper().replace("1000", "", 1) if (base or "").upper().startswith("1000") else (base or "").upper()
    hits = []
    for cand in (f"{base.upper()}USDT", f"{b}USDT", f"1000{b}USDT", f"1000000{b}USDT"):
        if cand in binance:
            hits.append(binance[cand])
            break
    for cand in (f"{b}-USDT", f"{base.upper()}-USDT"):
        if cand in htx:
            hits.append(htx[cand])
            break
    return hits


def is_tradfi(sym: str) -> bool:
    s = sym.upper()
    return any(k in s for k in TRADFI_HINTS)


# ───────────────────── 候选聚合与打分 ─────────────────────

def binance_tradfi_candidates(binance: dict, min_volume_usd: float = 20_000_000) -> list[dict]:
    """Binance 的 TRADIFI_PERPETUAL（股票/ETF/商品/外汇/Pre-IPO），按成交额过滤"""
    us = {x.get("symbol"): x for x in dd("/api/us-stocks")}
    out = []
    for code, meta in binance.items():
        if meta.get("contract_type") != "TRADIFI_PERPETUAL":
            continue
        t = get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={code}")
        if not t or t.get("_err"):
            continue
        qv = float(t.get("quoteVolume") or 0)
        if qv < min_volume_usd:
            continue
        base = (meta.get("base") or "").upper()
        news = (us.get(base) or {}).get("news") or []
        pi = get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={code}")
        out.append({
            "raw_symbol": base, "sources": ["binance-tradfi"] + (["dogdoing-us-stocks"] if base in us else []),
            "metrics": {"hype_score": None, "us_stock_news_count": len(news),
                        "us_stock_latest_news": (news[0].get("title") if news else None)},
            "tradability": "可交易", "venue": "Binance", "contract": code, "base": base,
            "contract_type": "TRADIFI_PERPETUAL", "asset_class": "tradfi",
            "underlying_type": meta.get("underlying_type"),
            "quote_volume_24h_usd": qv, "change_24h_pct_ex": float(t.get("priceChangePercent") or 0),
            "funding_annualized_pct": float(pi.get("lastFundingRate") or 0) * 3 * 365 * 100,
        })
    return out


def tradfi_candidates(htx: dict, min_volume_usd: float = 200_000) -> list[dict]:
    """tradfi 候选：HTX 的股票/指数/商品/外汇合约（配 dogdoing 美股叙事）"""
    out = []
    us = {x.get("symbol"): x for x in dd("/api/us-stocks")}
    for code, meta in htx.items():
        if not is_tradfi(code):
            continue
        base = code.split("-")[0]
        m = get(f"https://api.hbdm.com/linear-swap-ex/market/detail/merged?contract_code={code}")
        tick = (m or {}).get("tick") or {}
        close = float(tick.get("close") or 0)
        amount = float(tick.get("amount") or 0)
        vol_usd = amount * close
        if vol_usd < min_volume_usd:
            continue
        fr = get(f"https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate?contract_code={code}")
        frr = ((fr or {}).get("data") or {}).get("funding_rate")
        op = float(tick.get("open") or 0)
        news = (us.get(base) or {}).get("news") or []
        out.append({
            "raw_symbol": base, "sources": ["htx-tradfi"] + (["dogdoing-us-stocks"] if base in us else []),
            "metrics": {"hype_score": None,
                        "change_24h_pct": (close / op - 1) * 100 if op else None,
                        "us_stock_news_count": len(news),
                        "us_stock_latest_news": (news[0].get("title") if news else None)},
            "tradability": "可交易", "venue": "HTX", "contract": code, "base": base,
            "asset_class": "tradfi", "quote_volume_24h_usd": vol_usd,
            "change_24h_pct_ex": (close / op - 1) * 100 if op else None,
            "funding_annualized_pct": float(frr) * 3 * 365 * 100 if frr is not None else None,
            "contract_size": meta.get("contract_size"), "price_tick": meta.get("price_tick"),
            "max_leverage": meta.get("max_leverage"),
        })
    return out


def gather_candidates() -> list[dict]:
    """从 dogdoing 各接口汇总候选，每条记录带来源与原始指标"""
    cand: dict[str, dict] = {}

    def add(sym, src, **kw):
        if not sym:
            return
        s = sym.upper()
        rec = cand.setdefault(s, {"raw_symbol": s, "sources": [], "metrics": {}})
        rec["sources"].append(src)
        rec["metrics"].update({k: v for k, v in kw.items() if v is not None})

    for x in dd("/api/square-hype"):
        add(x.get("symbol"), "square-hype", hype_score=x.get("score"),
            change_24h_pct=x.get("priceChangePct"), volume_24h_usd=x.get("volume24h"))
    for x in dd("/api/oi-divergence"):
        add(x.get("symbol"), "oi-divergence", oi_change_pct=x.get("oiChangePct"),
            oi_price_change_pct=x.get("priceChangePct"), oi_divergence_ratio=x.get("divergenceRatio"),
            volume_24h_usd=x.get("volume24h"))
    for x in dd("/api/gainers"):
        add(x.get("symbol"), "gainers", change_24h_pct=x.get("change"), volume_24h_usd=x.get("volume"))
    for x in dd("/api/losers"):
        add(x.get("symbol"), "losers", change_24h_pct=x.get("change"), volume_24h_usd=x.get("volume"))
    for h in dd("/api/hotspots?chainId=56"):
        for t in (h.get("tokens") or []):
            add(t.get("symbol"), "hotspots", change_24h_pct=t.get("priceChange"),
                alpha_market_cap=t.get("marketCap"), alpha_net_inflow=t.get("netInflow"), topic=h.get("name"))
    return list(cand.values())


def enrich(rec: dict, venue: dict) -> dict:
    """补齐成交额/资金费率/OI 变化（按场所取），用于流动性与拥挤度粗判"""
    c, v = rec["contract"], rec["venue"]
    try:
        if v == "Binance":
            t = get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={c}")
            pi = get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={c}")
            oi = get(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={c}&period=4h&limit=7")
            vals = [float(x["sumOpenInterestValue"]) for x in (oi or [])] if isinstance(oi, list) else []
            rec.update({
                "quote_volume_24h_usd": float(t.get("quoteVolume") or 0),
                "change_24h_pct_ex": float(t.get("priceChangePercent") or 0),
                "funding_annualized_pct": float(pi.get("lastFundingRate") or 0) * 3 * 365 * 100,
                "oi_usd": vals[-1] if vals else None,
                "oi_change_24h_pct_ex": ((vals[-1] / vals[-7] - 1) * 100) if len(vals) >= 7 and vals[-7] else None,
            })
        else:
            m = get(f"https://api.hbdm.com/linear-swap-ex/market/detail/merged?contract_code={c}")
            fr = get(f"https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate?contract_code={c}")
            oi = get(f"https://api.hbdm.com/linear-swap-api/v1/swap_open_interest?contract_code={c}")
            tick = (m or {}).get("tick") or {}
            frr = ((fr or {}).get("data") or {}).get("funding_rate")
            oid = ((oi or {}).get("data") or {})
            rec.update({
                "quote_volume_24h_usd": float(((tick.get("amount") or 0)) * float(tick.get("close") or 0)),
                "change_24h_pct_ex": float(tick.get("close") or 0) / float(tick.get("open") or 1) * 100 - 100 if tick.get("open") else None,
                "funding_annualized_pct": float(frr) * 3 * 365 * 100 if frr is not None else None,
                "oi_contracts": oid.get("open_interest"),
            })
    except Exception as e:                                        # noqa: BLE001
        rec["enrich_error"] = str(e)[:80]
    return rec


def score(rec: dict) -> float:
    """机会分：tradfi 与 crypto 用不同口径（tradfi 无社交热度与 OI 背离，看流动性/摆幅/新闻流）"""
    import math as _m
    if rec.get("asset_class") == "tradfi":
        s = 0.0
        qv = rec.get("quote_volume_24h_usd") or 0
        s += min(max(_m.log10(qv) - 5, 0), 3) / 3 * 40 if qv else 0      # 流动性（tradfi 最关键）
        ch = abs(rec.get("change_24h_pct_ex") or 0)
        s += min(ch, 4) / 4 * 25                                          # 日内摆幅（tradfi 4% 已很大）
        nw = (rec.get("metrics") or {}).get("us_stock_news_count") or 0
        s += min(nw, 10) / 10 * 20                                        # 新闻/事件流
        fa = abs(rec.get("funding_annualized_pct") or 0)
        s += min(fa, 20) / 20 * 15                                        # 基差/费率偏离
        return round(s if rec.get("tradability") == "可交易" else 0, 1)
    m = rec.get("metrics") or {}
    h = (rec.get("hype_score") or m.get("hype_score") or 0)
    s = h / 100 * 25
    dr = rec.get("oi_divergence_ratio")
    if dr is None:
        dr = m.get("oi_divergence_ratio")
    if dr:
        s += min(float(dr), 8) / 8 * 25
    ch = abs(rec.get("change_24h_pct_ex") or rec.get("change_24h_pct") or 0)
    s += min(ch, 60) / 60 * 20
    fa = abs(rec.get("funding_annualized_pct") or 0)
    s += min(fa, 60) / 60 * 15
    qv = rec.get("quote_volume_24h_usd") or 0
    s += min(max((qv and __import__("math").log10(qv) - 5) or 0, 0), 3) / 3 * 15
    if "hotspots" in rec.get("sources", []):
        s += 5
    if rec.get("tradability") != "可交易":
        s = 0
    return round(s, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="从 dogdoing 挖交易机会 → 可交易候选榜")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--only", default=None, choices=["crypto", "tradfi"])
    ap.add_argument("--no-enrich", action="store_true", help="跳过逐个补数据（更快，但流动性与费率列会空）")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    print("拉取场所合约清单…")
    b, h = binance_universe(), htx_universe()
    print(f"  Binance 永续 {len(b)} 个 ｜ HTX 永续 {len(h)} 个")

    cands = gather_candidates()
    print(f"dogdoing 候选 {len(cands)} 个（去重后）")
    tf_b = binance_tradfi_candidates(b)
    tf_h = tradfi_candidates(h)
    print(f"Binance tradfi 候选 {len(tf_b)} 个（合约 + 成交额≥$20M）｜ HTX tradfi 候选 {len(tf_h)} 个")
    have = {r["contract"] for r in tf_b}
    cands = cands + tf_b + [r for r in tf_h if r["contract"] not in have]

    rows = []
    for rec in cands:
        if rec.get("venue"):                 # tradfi 候选已带场所与合约
            rec["opportunity_score"] = score(rec)
            rows.append(rec)
            continue
        hits = resolve(rec["raw_symbol"], b, h)
        if not hits:
            rec.update({"tradability": "不可交易（两场所均无对应永续）", "venue": None, "contract": None})
            rows.append(rec)
            continue
        # 优先 Binance（费率更低/深度更好）；tradfi 类只能用 HTX
        pref = next((x for x in hits if x["venue"] == "Binance"), hits[0])
        rec.update({"tradability": "可交易", **{k: v for k, v in pref.items()}})
        rec["alt_venues"] = [f"{x['venue']}:{x['contract']}" for x in hits if x is not pref]
        rec["asset_class"] = pref.get("asset_class") or ("tradfi" if is_tradfi(rec["contract"]) else "crypto")
        rows.append(rec)

    if not a.no_enrich:
        tradable = [r for r in rows if r["tradability"] == "可交易"]
        print(f"补数据（{len(tradable)} 个，逐个取成交额/费率/OI）…")
        for r in tradable:
            enrich(r, None)
        # 元数据（热度分）从 metrics 提到顶层，便于打分
        for r in tradable:
            r["hype_score"] = r.get("hype_score") or (r.get("metrics") or {}).get("hype_score")
            if not r.get("oi_divergence_ratio"):
                r["oi_divergence_ratio"] = (r.get("metrics") or {}).get("oi_divergence_ratio")

    for r in rows:
        r["opportunity_score"] = score(r)
    rows.sort(key=lambda x: -x["opportunity_score"])
    if a.only:
        rows = [r for r in rows if r.get("asset_class") == a.only or r["tradability"] != "可交易"]
    top = rows[:a.top]

    print(f"\n{'标的':10} {'类别':7} {'场所':8} {'合约':16} {'机会分':>6} {'24h%':>7} {'成交额(USD)':>13} {'费率年化%':>9} {'来源'}")
    print("-" * 118)
    for r in top:
        print(f"{r['raw_symbol']:10} {r.get('asset_class','-'):7} {str(r.get('venue')):8} {str(r.get('contract')):16} "
              f"{r['opportunity_score']:>6} {(r.get('change_24h_pct_ex') or r.get('change_24h_pct') or 0):>7.2f} "
              f"{(r.get('quote_volume_24h_usd') or 0):>13,.0f} "
              f"{(r.get('funding_annualized_pct') if r.get('funding_annualized_pct') is not None else float('nan')):>9.2f} "
              f"{','.join(r['sources'])}")

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = a.out or os.path.join(OUT_DIR, f"watchlist_{day}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    payload = {"generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
               "counts": {"candidates": len(cands), "tradable": sum(1 for r in rows if r["tradability"] == "可交易"),
                          "binance_perps": len(b), "htx_perps": len(h)},
               "watchlist": top}
    json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n_tr = sum(1 for r in top if r.get("asset_class") == "tradfi")
    print(f"\n[OK] {out}｜可交易 {payload['counts']['tradable']} 个｜榜内 tradfi {n_tr} 个")
    n_cr = sum(1 for r in top if r.get("asset_class") == "crypto")
    print(f"说明：加密 {n_cr} 个（Binance/HTX 均可执行）｜tradfi {n_tr} 个（**Binance TRADIFI_PERPETUAL，用同一套 Binance 凭证即可交易**）")
    print("     tradfi 覆盖：股票 163 / 港股 15 / 韩股 8 / 商品 8 / 外汇 1 / Pre-IPO 2；且 24/7 无休市（实测 168 根 1H K线零缺口）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
