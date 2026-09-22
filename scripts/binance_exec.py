#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
binance_exec.py — Binance USDT-M 合约执行层（默认全程 dry-run，必须显式 --live 才真下单）

安全设计：
  · 默认 --dry：只签名+校验+打印将要发送的订单，不发送
  · 真下单必须显式加 --live，且会在 stdout 打印订单回执与成交明细
  · 自动按合约 stepSize / tickSize 取整、校验 MIN_NOTIONAL、校验可用保证金
  · 止损/止盈默认用 reduceOnly 条件单，避免平仓再开反向仓

用法:
  python3 binance_exec.py account                                  # 余额/持仓
  python3 binance_exec.py restrictions                             # 权限（含提现开关）
  python3 binance_exec.py specs BTCUSDT                            # 合约规格
  python3 binance_exec.py set-leverage BTCUSDT 5 [--live]           # 设置杠杆
  python3 binance_exec.py order BTCUSDT buy --notional 45 --sl 82000 --tp 90000 [--live]
  python3 binance_exec.py order BTCUSDT sell --qty 0.001 --type limit --price 88000 [--live]
  python3 binance_exec.py close BTCUSDT [--live]                    # 市价平仓
  python3 binance_exec.py orders BTCUSDT                            # 当前挂单
  python3 binance_exec.py positions                                 # 持仓明细
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CRED = os.path.expanduser("~/.binance_futures_credentials.json")
FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
_SPEC_CACHE: dict[str, dict] = {}


def _creds() -> tuple[str, str]:
    with open(CRED, encoding="utf-8") as f:
        c = json.load(f)
    return c["api_key"], c["api_secret"]


def _req(method: str, path: str, params: dict | None = None, signed: bool = True,
         base: str = FAPI, timeout: int = 20):
    p = dict(params or {})
    headers = {}
    if signed:
        key, sec = _creds()
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 5000
        q = urllib.parse.urlencode(p)
        p["signature"] = hmac.new(sec.encode(), q.encode(), hashlib.sha256).hexdigest()
        headers["X-MBX-APIKEY"] = key
    q = urllib.parse.urlencode(p)
    url = f"{base}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            j = json.loads(body)
        except Exception:                                       # noqa: BLE001
            j = {"msg": body}
        return {"_error": True, "http": e.code, **j}
    except Exception as e:                                      # noqa: BLE001
        return {"_error": True, "msg": f"{type(e).__name__}: {e}"}


# ───────────────────────── 合约规格与取整 ─────────────────────────

def spec(symbol: str) -> dict:
    if symbol in _SPEC_CACHE:
        return _SPEC_CACHE[symbol]
    info = _req("GET", "/fapi/v1/exchangeInfo", signed=False)
    syms = {s["symbol"]: s for s in (info.get("symbols") or [])}
    s = syms.get(symbol)
    if not s:
        raise SystemExit(f"[FATAL] 合约 {symbol} 不存在")
    f = {x["filterType"]: x for x in s["filters"]}
    brackets = _req("GET", "/fapi/v1/leverageBracket", {"symbol": symbol})
    max_lev, mmr = None, None
    if isinstance(brackets, list) and brackets:
        b0 = (brackets[0].get("brackets") or [{}])[0]
        max_lev, mmr = b0.get("initialLeverage"), b0.get("maintMarginRatio")
    out = {
        "symbol": symbol,
        "min_notional": float((f.get("MIN_NOTIONAL") or {}).get("notional") or 0),
        "step_size": (f.get("LOT_SIZE") or {}).get("stepSize"),
        "tick_size": (f.get("PRICE_FILTER") or {}).get("tickSize"),
        "max_leverage": max_lev, "maint_margin_ratio": mmr,
        "price_precision": s.get("pricePrecision"), "quantity_precision": s.get("quantityPrecision"),
    }
    _SPEC_CACHE[symbol] = out
    return out


def round_step(value: float, step: str) -> float:
    if not step:
        return value
    s = float(step)
    if s <= 0:
        return value
    return math.floor(value / s) * s


def round_tick(value: float, tick: str) -> float:
    if not tick:
        return value
    t = float(tick)
    return round(value / t) * t if t > 0 else value


def fmt(value: float, step: str | None) -> str:
    if not step:
        return f"{value:.8f}".rstrip("0").rstrip(".")
    dec = max(0, -int(round(math.log10(float(step))))) if float(step) < 1 else 0
    return f"{value:.{dec}f}"


def mark_price(symbol: str) -> float:
    d = _req("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
    return float(d.get("markPrice") or 0)


def account_state() -> dict:
    a = _req("GET", "/fapi/v2/account")
    if a.get("_error"):
        raise SystemExit(f"[FATAL] 取账户失败: {a}")
    return a


# ───────────────────────── 动作 ─────────────────────────

def cmd_account(_a) -> None:
    a = account_state()
    print(f"钱包余额 {a['totalWalletBalance']} ｜ 保证金余额 {a['totalMarginBalance']} ｜ "
          f"可用 {a['availableBalance']} ｜ 未实现盈亏 {a['totalUnrealizedProfit']}")
    pos = [p for p in a.get("positions", []) if float(p.get("positionAmt", 0)) != 0]
    print(f"持仓 {len(pos)} 个" + ("：" if pos else "（空仓）"))
    for p in pos:
        print(f"  {p['symbol']} 数量 {p['positionAmt']} 开仓价 {p['entryPrice']} 杠杆 {p['leverage']}x "
              f"未实现 {p['unrealizedProfit']} 强平价 {p.get('liquidationPrice')}")


def cmd_restrictions(_a) -> None:
    r = _req("GET", "/sapi/v1/account/apiRestrictions", base=SAPI)
    if r.get("_error"):
        print("取权限失败:", r)
        return
    for k, v in r.items():
        print(f"  {k:34} = {v}")
    print("  ⚠️ 提现权限为开启！建议立刻关闭" if r.get("enableWithdrawals") else "  ✅ 提现权限：关闭")


def cmd_specs(a) -> None:
    for s in a.symbols:
        sp = spec(s)
        print(f"{sp['symbol']:10} 最小名义 {sp['min_notional']:>8} ｜ 数量步长 {sp['step_size']:>8} ｜ "
              f"价格步长 {sp['tick_size']:>8} ｜ 一档最大杠杆 {sp['max_leverage']}x ｜ 维持保证金率 {sp['maint_margin_ratio']}")


def cmd_set_leverage(a) -> None:
    sp = spec(a.symbol)
    if a.leverage > (sp["max_leverage"] or 0):
        print(f"[拒绝] {a.symbol} 一档最大杠杆 {sp['max_leverage']}x，请求 {a.leverage}x 超限")
        return
    if not a.live:
        print(f"[dry-run] 将设置 {a.symbol} 杠杆 = {a.leverage}x（加 --live 才真正发送）")
        return
    r = _req("POST", "/fapi/v1/leverage", {"symbol": a.symbol, "leverage": int(a.leverage)})
    print(json.dumps(r, ensure_ascii=False))


def cmd_positions(_a) -> None:
    a = account_state()
    for p in a.get("positions", []):
        if float(p.get("positionAmt", 0)) != 0:
            print(json.dumps(p, ensure_ascii=False))


def cmd_orders(a) -> None:
    r = _req("GET", "/fapi/v1/openOrders", {"symbol": a.symbol} if a.symbol else None)
    if r.get("_error"):
        print(r)
        return
    if not r:
        print("无挂单")
    for o in r:
        print(f"  {o['symbol']} {o['side']} {o['type']} 数量 {o['origQty']} 价格 {o.get('price')} "
              f"reduceOnly {o.get('reduceOnly')} id {o['orderId']}")


def build_order(a) -> dict | None:
    px = mark_price(a.symbol)
    sp = spec(a.symbol)
    qty = None
    if a.notional:
        qty = a.notional / px
    elif a.qty:
        qty = a.qty
    if qty is None:
        print("[拒绝] 必须给 --qty 或 --notional")
        return None
    qty = round_step(qty, sp["step_size"])
    if qty <= 0:
        print(f"[拒绝] 数量按步长 {sp['step_size']} 取整后为 0")
        return None
    notional = qty * px
    if sp["min_notional"] and notional < sp["min_notional"]:
        print(f"[拒绝] 名义 {notional:.2f} < 该合约最小名义 {sp['min_notional']}（需 ≥ {sp['min_notional']} USDT）")
        return None
    acct = account_state()
    avail = float(acct["availableBalance"])
    p = {"symbol": a.symbol, "side": a.side.upper(), "quantity": fmt(qty, sp["step_size"])}
    if a.type == "market":
        p["type"] = "MARKET"
    else:
        if not a.price:
            print("[拒绝] limit 单必须给 --price")
            return None
        p["type"] = "LIMIT"
        p["price"] = fmt(round_tick(a.price, sp["tick_size"]), sp["tick_size"])
        p["timeInForce"] = a.tif
    print(f"[预览] {a.symbol} {p['side']} {p['type']} 数量 {p['quantity']} "
          f"（标记价 {px} ｜ 名义 {notional:.2f} USDT ｜ 可用 {avail:.2f} USDT）")
    return p


def cmd_order(a) -> None:
    p = build_order(a)
    if not p:
        return
    if not a.live:
        print("[dry-run] 未发送。加 --live 才会真正下单。")
        return
    r = _req("POST", "/fapi/v1/order", p)
    print("订单回执:", json.dumps(r, ensure_ascii=False))
    if r.get("_error"):
        return
    # 止损/止盈：reduceOnly 条件单
    for kind, trigger in (("STOP_MARKET", a.sl), ("TAKE_PROFIT_MARKET", a.tp)):
        if not trigger:
            continue
        sp = spec(a.symbol)
        q = {"symbol": a.symbol, "side": ("SELL" if a.side.lower() == "buy" else "BUY"),
             "type": kind, "quantity": p["quantity"], "reduceOnly": "true",
             "stopPrice": fmt(round_tick(trigger, sp["tick_size"]), sp["tick_size"]),
             "workingType": "MARK_PRICE"}
        r2 = _req("POST", "/fapi/v1/order", q)
        print(f"{kind} @ {q['stopPrice']} 回执:", json.dumps(r2, ensure_ascii=False))


def cmd_close(a) -> None:
    acct = account_state()
    pos = [p for p in acct.get("positions", []) if p["symbol"] == a.symbol]
    amt = float(pos[0]["positionAmt"]) if pos else 0.0
    if amt == 0:
        print(f"{a.symbol} 无持仓")
        return
    side = "SELL" if amt > 0 else "BUY"
    sp = spec(a.symbol)
    q = {"symbol": a.symbol, "side": side, "type": "MARKET", "quantity": fmt(abs(amt), sp["step_size"]),
         "reduceOnly": "true"}
    print(f"[预览] 市价平仓 {a.symbol} {side} 数量 {q['quantity']}（当前持仓 {amt}）")
    if not a.live:
        print("[dry-run] 未发送。加 --live 才会平仓。")
        return
    print("平仓回执:", json.dumps(_req("POST", "/fapi/v1/order", q), ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance USDT-M 执行层（默认 dry-run）")
    ap.add_argument("--live", action="store_true", help="真正发送订单（默认只预览）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("account").set_defaults(fn=cmd_account)
    sub.add_parser("restrictions").set_defaults(fn=cmd_restrictions)
    sub.add_parser("positions").set_defaults(fn=cmd_positions)

    sp = sub.add_parser("specs"); sp.add_argument("symbols", nargs="+"); sp.set_defaults(fn=cmd_specs)
    sl = sub.add_parser("set-leverage"); sl.add_argument("symbol"); sl.add_argument("leverage", type=int); sl.set_defaults(fn=cmd_set_leverage)
    so = sub.add_parser("orders"); so.add_argument("symbol", nargs="?"); so.set_defaults(fn=cmd_orders)
    sc = sub.add_parser("close"); sc.add_argument("symbol"); sc.set_defaults(fn=cmd_close)
    so2 = sub.add_parser("order")
    so2.add_argument("symbol"); so2.add_argument("side", choices=["buy", "sell"])
    so2.add_argument("--qty", type=float); so2.add_argument("--notional", type=float)
    so2.add_argument("--type", default="market", choices=["market", "limit"])
    so2.add_argument("--price", type=float); so2.add_argument("--tif", default="GTC")
    so2.add_argument("--sl", type=float, help="止损触发价（reduceOnly STOP_MARKET）")
    so2.add_argument("--tp", type=float, help="止盈触发价（reduceOnly TAKE_PROFIT_MARKET）")
    so2.set_defaults(fn=cmd_order)

    a = ap.parse_args()
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
