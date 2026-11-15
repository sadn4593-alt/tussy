#!/usr/bin/env python3
"""
DERIV GOLD & FOREX SCALPER  (self-host, runs on your own computer)
==================================================================
Symbols : frxXAUUSD (gold), frxEURUSD, frxGBPUSD, frxUSDJPY, ... (forex only)
Product : Deriv multipliers (loss can never exceed the stake; TP/SL are set on every trade)
Style   : quick-profit scalping on 1-minute candles, many small wins, hard risk limits

Skills built in
  1. Confluence entry   : needs 3 of 4 signals to agree (trend stack, RSI pullback, breakout, higher-timeframe trend)
  2. Regime filter      : skips choppy markets (efficiency ratio) and dead markets (ATR floor)
  3. Session filter     : trades only London/NewYork hours (UTC), never rollover or thin Asia hours
  4. ATR-based TP / SL  : exits scale with current volatility
  5. Break-even + trail : once in profit the trade can no longer turn into a full loss; profit is locked
  6. Time stop          : a scalp that goes nowhere is closed
  7. Risk manager       : daily loss limit, DAILY PROFIT TARGET (cash out and stop), loss-streak pause,
                          max open trades, cooldown after each trade
  8. Trade journal      : every trade saved to trades.csv, optional Telegram alerts

Setup
  pip install numpy pandas websockets requests
  export DERIV_TOKEN="your personal access token"     (Windows: setx DERIV_TOKEN "...")
  export DERIV_APP_ID="your app id"                   (from the app you are registering on developers.deriv.com)

Run
  python deriv_bot.py backtest                        # synthetic data, tests the logic
  python deriv_bot.py backtest --csv xauusd_1m.csv    # your own 1-minute candles (time,open,high,low,close)
  python deriv_bot.py accounts                        # list accounts / create a demo account
  python deriv_bot.py symbols                         # which forex / gold symbols offer multipliers right now
  python deriv_bot.py live --symbols frxXAUUSD,frxEURUSD            # DEMO account (default)
  python deriv_bot.py live --symbols frxXAUUSD --real               # REAL money (asks for confirmation)

No bot can promise "never lose". Gold and forex move on news. Scalpers win often and lose rarely but bigger;
the risk manager exists to keep those losses small. Educational software, not financial advice.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

REST = "https://api.derivws.com/trading/v1/options"
PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
@dataclass
class Cfg:
    stake: float = 1.0                # USD per trade (your max loss per trade is this)
    multiplier: int = 100             # must be one Deriv allows for the symbol
    tp_atr: float = 1.0               # take profit = 1.0 ATR move
    sl_atr: float = 1.5               # stop loss   = 1.5 ATR move
    min_score: int = 3                # signals that must agree (of 4)
    min_er: float = 0.20              # efficiency ratio floor (skip chop)
    session_start: int = 7            # UTC hours allowed
    session_end: int = 17
    be_trigger: float = 0.5           # at 50% of TP: move to break-even
    trail_giveback: float = 0.35      # then sell if profit falls 35% from its peak
    max_hold_bars: int = 25           # time stop (minutes)
    cooldown_bars: int = 3            # pause after each trade on that symbol
    max_open: int = 2
    daily_loss_pct: float = 5.0       # stop for the day at -5% of starting balance
    daily_profit_pct: float = 3.0     # CASH OUT and stop for the day at +3%
    loss_streak: int = 3              # pause after 3 losses in a row
    pause_minutes: int = 30
    spread_bps: float = 3.0           # cost assumption used in backtest


# ----------------------------------------------------------------------------
# INDICATORS + SIGNAL
# ----------------------------------------------------------------------------
def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=7):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def efficiency(c, n=20):
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum()


MIN_BARS = 160


def signal(df: pd.DataFrame, cfg: Cfg):
    """Returns (direction +1/-1/0, score, atr). Uses only completed bars."""
    if len(df) < MIN_BARS:
        return 0, 0, 0.0
    c = df.close
    a = float(atr(df).iloc[-1])
    er = float(efficiency(c).iloc[-1])
    if not np.isfinite(a) or a <= 0 or not np.isfinite(er) or er < cfg.min_er:
        return 0, 0, a                                   # chop or dead market -> stand aside
    e9, e21, e50 = (float(ema(c, n).iloc[-1]) for n in (9, 21, 50))
    r = rsi(c, 7)
    r_now, r_prev, r_lo, r_hi = float(r.iloc[-1]), float(r.iloc[-2]), float(r.iloc[-4:-1].min()), float(r.iloc[-4:-1].max())
    hi12, lo12 = float(df.high.iloc[-13:-1].max()), float(df.low.iloc[-13:-1].min())
    htf_up = float(ema(c, 60).iloc[-1]) > float(ema(c, 150).iloc[-1])
    last = float(c.iloc[-1])
    big_bar = (df.high.iloc[-1] - df.low.iloc[-1]) > 0.9 * a

    long_pts = sum([e9 > e21 > e50,
                    r_lo < 40 and r_now > r_prev,
                    last > hi12 and big_bar,
                    htf_up])
    short_pts = sum([e9 < e21 < e50,
                     r_hi > 60 and r_now < r_prev,
                     last < lo12 and big_bar,
                     not htf_up])
    if long_pts >= cfg.min_score and long_pts > short_pts:
        return 1, long_pts, a
    if short_pts >= cfg.min_score and short_pts > long_pts:
        return -1, short_pts, a
    return 0, 0, a


def dollar_levels(price: float, a: float, cfg: Cfg):
    """Convert an ATR move into dollar TP / SL for a multiplier contract."""
    per_atr = cfg.stake * cfg.multiplier * a / price
    tp = min(max(per_atr * cfg.tp_atr, 0.10 * cfg.stake), 2.0 * cfg.stake)
    sl = min(max(per_atr * cfg.sl_atr, 0.10 * cfg.stake), 0.90 * cfg.stake)   # SL must be below the stake
    return round(tp, 2), round(sl, 2)


# ----------------------------------------------------------------------------
# RISK MANAGER
# ----------------------------------------------------------------------------
class Risk:
    def __init__(self, cfg: Cfg, balance: float):
        self.cfg, self.start_balance = cfg, balance
        self.day, self.day_pnl, self.streak, self.paused_until = None, 0.0, 0, 0.0

    def _roll(self, ts: float):
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        if d != self.day:
            self.day, self.day_pnl, self.streak = d, 0.0, 0

    def can_trade(self, ts: float, open_count: int):
        self._roll(ts)
        pct = self.day_pnl / self.start_balance * 100
        if pct <= -self.cfg.daily_loss_pct:
            return False, "daily loss limit hit"
        if pct >= self.cfg.daily_profit_pct:
            return False, "daily profit target hit - cashed out for the day"
        if ts < self.paused_until:
            return False, "loss-streak pause"
        if open_count >= self.cfg.max_open:
            return False, "max open trades"
        return True, ""

    def on_close(self, pnl: float, ts: float):
        self._roll(ts)
        self.day_pnl += pnl
        self.streak = self.streak + 1 if pnl < 0 else 0
        if self.streak >= self.cfg.loss_streak:
            self.paused_until, self.streak = ts + self.cfg.pause_minutes * 60, 0


def manage_exit(profit: float, peak: float, tp: float, bars_held: int, cfg: Cfg):
    """Break-even + trailing profit lock + time stop. Returns True if the trade should be sold now."""
    if peak >= cfg.be_trigger * tp:
        if profit <= 0.03 * cfg.stake:                       # back to break-even -> take it
            return True
        if profit <= peak * (1 - cfg.trail_giveback):         # gave back too much of the peak
            return True
    return bars_held >= cfg.max_hold_bars


# ----------------------------------------------------------------------------
# BACKTEST (tests the exact same signal / levels / exits / risk rules)
# ----------------------------------------------------------------------------
def synthetic_candles(days=40, seed=3, start_price=2300.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-05", periods=days * 1440, freq="min", tz="UTC")
    idx = idx[idx.dayofweek < 5]
    n = len(idx)
    hour = idx.hour.values
    vol = np.where((hour >= 7) & (hour < 17), 0.00022, 0.00010)
    drift = np.zeros(n)
    reg, d = 0, 0.0
    for i in range(n):
        if i % 180 == 0:
            reg = rng.integers(0, 3)
            d = [0.00004, -0.00004, 0.0][reg]
        drift[i] = d
    ret = drift + rng.normal(0, vol)
    for i in range(1, n):
        ret[i] += 0.05 * ret[i - 1]
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    wig = np.abs(rng.normal(0, vol * 0.8)) * close
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wig,
                         "low": np.minimum(open_, close) - wig, "close": close}, index=idx)


def backtest(df: pd.DataFrame, cfg: Cfg, balance=50.0):
    risk = Risk(cfg, balance)
    trades, pos, cooldown = [], None, 0
    O, H, L, C = df.open.values, df.high.values, df.low.values, df.close.values
    ts = (df.index.astype("int64") // 10**9).values
    hrs = df.index.hour.values
    for i in range(MIN_BARS, len(df) - 1):
        if pos:
            d, e = pos["dir"], pos["entry"]
            k = cfg.stake * cfg.multiplier / e
            hi_p, lo_p = (H[i] - e) * d * k, (L[i] - e) * d * k
            best, worst = (hi_p, lo_p) if d > 0 else (lo_p, hi_p)
            worst, best = min(best, worst), max(best, worst)
            cost = cfg.stake * cfg.multiplier * cfg.spread_bps / 1e4
            pnl, done = None, False
            if worst <= -pos["sl"]:                          # worst case first: stop before target
                pnl, done = -pos["sl"] - cost, True
            elif best >= pos["tp"]:
                pnl, done = pos["tp"] - cost, True
            else:
                cur = (C[i] - e) * d * k
                pos["peak"] = max(pos["peak"], cur)
                pos["bars"] += 1
                if manage_exit(cur, pos["peak"], pos["tp"], pos["bars"], cfg):
                    pnl, done = cur - cost, True
            if done:
                trades.append(pnl)
                risk.on_close(pnl, ts[i])
                pos, cooldown = None, cfg.cooldown_bars
            continue
        if cooldown > 0:
            cooldown -= 1
            continue
        if not (cfg.session_start <= hrs[i] < cfg.session_end):
            continue
        ok, _ = risk.can_trade(ts[i], 0)
        if not ok:
            continue
        d, score, a = signal(df.iloc[max(0, i - 399): i + 1], cfg)
        if d:
            tp, sl = dollar_levels(C[i], a, cfg)
            pos = {"dir": d, "entry": O[i + 1], "tp": tp, "sl": sl, "peak": 0.0, "bars": 0}
    t = np.array(trades) if trades else np.array([0.0])
    eq = balance + np.cumsum(t)
    return {
        "trades": len(trades),
        "win_rate_pct": round(float((t > 0).mean() * 100), 1),
        "net_profit_usd": round(float(t.sum()), 2),
        "avg_win": round(float(t[t > 0].mean()), 3) if (t > 0).any() else 0,
        "avg_loss": round(float(t[t <= 0].mean()), 3) if (t <= 0).any() else 0,
        "profit_factor": round(float(t[t > 0].sum() / abs(t[t <= 0].sum())), 2) if (t <= 0).any() and t[t <= 0].sum() != 0 else None,
        "max_drawdown_usd": round(float((np.maximum.accumulate(eq) - eq).max()), 2),
        "worst_trade": round(float(t.min()), 2),
    }


# ----------------------------------------------------------------------------
# DERIV CONNECTION
# ----------------------------------------------------------------------------
def headers():
    token, app = os.environ.get("DERIV_TOKEN"), os.environ.get("DERIV_APP_ID")
    if not token or not app:
        raise SystemExit("Set DERIV_TOKEN and DERIV_APP_ID first (see top of file).")
    return {"Authorization": f"Bearer {token}", "Deriv-App-ID": app, "Content-Type": "application/json"}


def _aid(a): return a["account_id"]                       # documented field


def _is_demo(a): return a.get("account_type") == "demo"     # documented: "demo" | "real"


def pick_multiplier(avail, want: int) -> int:
    """Read the allowed multipliers from contracts_for; use `want` if allowed, else the closest allowed one."""
    allowed = []
    for c in avail:
        if isinstance(c, dict) and c.get("contract_type") == "MULTUP":
            mr = c.get("multiplier_range") or c.get("multipliers") or []
            allowed = [int(x) for x in mr if str(x).isdigit()]
    if not allowed:
        return want
    return min(allowed, key=lambda x: abs(x - want))


def list_accounts():
    import requests
    r = requests.get(f"{REST}/accounts", headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("data", [])


def cmd_symbols():
    """Lists forex / commodity symbols that offer multipliers, and the raw multiplier info for the first one."""
    async def go():
        c = Conn(PUBLIC_WS, "public")
        await c.start()
        r = await c.call({"active_symbols": "brief", "contract_type": ["MULTUP"]})
        rows = [s for s in r["active_symbols"] if s.get("market") in ("forex", "commodities") or "XAU" in s["underlying_symbol"]]
        for s in rows:
            print(s["underlying_symbol"], "|", s.get("underlying_symbol_name"), "| open" if s.get("exchange_is_open") else "| closed")
        if rows:
            cf = await c.call({"contracts_for": rows[0]["underlying_symbol"]})
            print("\nMultiplier info for", rows[0]["underlying_symbol"])
            print(json.dumps([x for x in cf["contracts_for"]["available"] if str(x.get("contract_type", "")).startswith("MULT")][:1], indent=2))
    asyncio.run(go())


def cmd_accounts():
    import requests
    accts = list_accounts()
    if not accts:
        r = requests.post(f"{REST}/accounts", headers=headers(), timeout=20,
                          json={"account_type": "demo", "currency": "USD", "group": "row"})
        print("Created demo account:", r.status_code, r.text)
        accts = list_accounts()
    for a in accts:
        print(("DEMO " if _is_demo(a) else "REAL ") + json.dumps(a))


class Conn:
    def __init__(self, url, name):
        self.url, self.name, self.pending, self.handlers, self.rid = url, name, {}, {}, 0

    async def start(self):
        import websockets
        self.ws = await websockets.connect(self.url, ping_interval=20, ping_timeout=20)
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            fut = self.pending.pop(m.get("req_id"), None)
            if fut and not fut.done():
                fut.set_result(m)
            h = self.handlers.get(m.get("msg_type"))
            if h:
                asyncio.create_task(h(m))

    async def call(self, payload, timeout=20):
        self.rid += 1
        payload = {**payload, "req_id": self.rid}
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.rid] = fut
        await self.ws.send(json.dumps(payload))
        m = await asyncio.wait_for(fut, timeout)
        if m.get("error"):
            raise RuntimeError(f"{self.name}: {m['error']}")
        return m


# ----------------------------------------------------------------------------
# LIVE BOT
# ----------------------------------------------------------------------------
class Bot:
    def __init__(self, cfg, symbols):
        self.cfg, self.symbols = cfg, symbols
        self.bars = {s: deque(maxlen=500) for s in symbols}
        self.cur = {s: None for s in symbols}                 # bar being built
        self.cool = {s: 0 for s in symbols}
        self.open = {}                                        # contract_id -> state

    def df(self, s):
        return pd.DataFrame(list(self.bars[s]))

    async def setup(self, real: bool, account_id: str | None):
        import requests
        accts = list_accounts()
        print("Accounts found:", [_aid(a) for a in accts])
        pool = [a for a in accts if _is_demo(a) != real]
        a = next((x for x in pool if not account_id or _aid(x) == account_id), None)
        if not a:
            raise SystemExit("No matching account. Run `python deriv_bot.py accounts` (creates a demo one).")
        print(f"Using {'REAL' if real else 'DEMO'} account {_aid(a)}")
        r = requests.post(f"{REST}/accounts/{_aid(a)}/otp", headers=headers(), timeout=20)
        r.raise_for_status()
        url = r.json()["data"]["url"]
        self.auth, self.pub = Conn(url, "trade"), Conn(PUBLIC_WS, "public")
        await self.auth.start()
        await self.pub.start()
        self.auth.handlers["proposal_open_contract"] = self.on_poc
        self.pub.handlers["tick"] = self.on_tick
        bal = a.get("balance") or (await self.auth.call({"balance": 1}))["balance"]["balance"]
        self.risk = Risk(self.cfg, float(bal))
        print(f"Balance: {bal}")
        self.mult = {}
        for s in list(self.symbols):
            try:
                cf = await self.pub.call({"contracts_for": s})
                self.mult[s] = pick_multiplier(cf["contracts_for"]["available"], self.cfg.multiplier)
            except Exception as e:
                print(f"{s}: cannot read contracts ({e}); using multiplier {self.cfg.multiplier}")
                self.mult[s] = self.cfg.multiplier
            print(f"{s}: multiplier {self.mult[s]}")
            await self.warm_up(s)
            await self.pub.call({"ticks": s, "subscribe": 1})

    async def warm_up(self, s):
        try:
            m = await self.pub.call({"ticks_history": s, "count": 300, "end": "latest",
                                     "style": "candles", "granularity": 60, "adjust_start_time": 1})
            for c in m.get("candles", [])[:-1]:
                self.bars[s].append({k: float(c[k]) for k in ("open", "high", "low", "close")} | {"t": int(c["epoch"])})
            print(f"{s}: warmed up with {len(self.bars[s])} candles")
        except Exception as e:
            print(f"{s}: no history ({e}); will collect live candles (~{MIN_BARS} min before first trade)")

    async def on_tick(self, m):
        t = m["tick"]
        s, q, minute = t["symbol"], float(t["quote"]), int(t["epoch"]) // 60
        b = self.cur.get(s)
        if b and b["minute"] == minute:
            b["high"], b["low"], b["close"] = max(b["high"], q), min(b["low"], q), q
            return
        if b:
            self.bars[s].append({k: b[k] for k in ("open", "high", "low", "close")} | {"t": b["minute"] * 60})
            for oc in self.open.values():
                if oc["symbol"] == s:
                    oc["bars"] += 1
            await self.on_bar(s, int(t["epoch"]))
        self.cur[s] = {"minute": minute, "open": q, "high": q, "low": q, "close": q}

    async def on_bar(self, s, now):
        if self.cool[s] > 0:
            self.cool[s] -= 1
            return
        if any(o["symbol"] == s for o in self.open.values()):
            return
        t = datetime.fromtimestamp(now, timezone.utc)
        if t.weekday() >= 5 or not (self.cfg.session_start <= t.hour < self.cfg.session_end):
            return                                            # weekend or outside London/NY hours
        ok, why = self.risk.can_trade(now, len(self.open))
        if not ok:
            return
        d, score, a = signal(self.df(s), self.cfg)
        if d:
            await self.enter(s, d, score, a, float(self.bars[s][-1]["close"]))

    async def enter(self, s, d, score, a, price):
        mult = self.mult.get(s, self.cfg.multiplier)
        cfg = Cfg(**{**self.cfg.__dict__, "multiplier": mult})
        tp, sl = dollar_levels(price, a, cfg)
        params = {"amount": cfg.stake, "basis": "stake",
                  "contract_type": "MULTUP" if d > 0 else "MULTDOWN", "currency": "USD",
                  "multiplier": mult, "underlying_symbol": s,
                  "limit_order": {"take_profit": tp, "stop_loss": sl}}
        try:
            try:                                              # documented: buy "1" + parameters
                b = await self.auth.call({"buy": "1", "price": cfg.stake, "parameters": params})
            except RuntimeError as e1:                        # fallback: streaming proposal -> buy -> forget
                print(f"[{s}] direct buy refused ({e1}); trying proposal flow")
                p = await self.auth.call({"proposal": 1, "subscribe": 1, **params})
                b = await self.auth.call({"buy": p["proposal"]["id"], "price": p["proposal"]["ask_price"]})
                sub = (p.get("subscription") or {}).get("id")
                if sub:
                    await self.auth.call({"forget": sub})
            cid = b["buy"]["contract_id"]
        except Exception as e:
            print(f"[{s}] entry failed: {e}")
            return
        self.open[cid] = {"symbol": s, "dir": d, "tp": tp, "sl": sl, "peak": 0.0, "bars": 0, "selling": False}
        self.cool[s] = self.cfg.cooldown_bars
        await self.auth.call({"proposal_open_contract": 1, "contract_id": cid, "subscribe": 1})
        self.log(f"OPEN {'BUY' if d > 0 else 'SELL'} {s} score={score} tp=${tp} sl=${sl}")

    async def on_poc(self, m):
        poc = m.get("proposal_open_contract") or {}
        cid = poc.get("contract_id")
        st = self.open.get(cid)
        if not st:
            return
        profit = float(poc.get("profit", 0))
        if poc.get("is_sold") or poc.get("status") in ("sold", "won", "lost"):
            self.open.pop(cid, None)
            self.risk.on_close(profit, time.time())
            self.journal(st, profit)
            self.log(f"CLOSED {st['symbol']} pnl=${profit:+.2f} | day=${self.risk.day_pnl:+.2f}")
            return
        st["peak"] = max(st["peak"], profit)
        if not st["selling"] and manage_exit(profit, st["peak"], st["tp"], st["bars"], self.cfg):
            st["selling"] = True
            try:
                await self.auth.call({"sell": cid, "price": 0})
            except Exception as e:
                st["selling"] = False
                print(f"sell failed: {e}")

    def journal(self, st, pnl):
        new = not os.path.exists("trades.csv")
        with open("trades.csv", "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time_utc", "symbol", "dir", "tp", "sl", "peak", "pnl"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), st["symbol"],
                        st["dir"], st["tp"], st["sl"], round(st["peak"], 2), round(pnl, 2)])

    def log(self, msg):
        print(f"[{datetime.now(timezone.utc):%F %T}] {msg}", flush=True)
        tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
        if tok and chat:
            try:
                import requests
                requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": msg}, timeout=5)
            except Exception:
                pass


async def run_live(args, cfg):
    if args.real and input("REAL MONEY mode. Type YES to continue: ").strip() != "YES":
        raise SystemExit("Cancelled.")
    bot = Bot(cfg, [s.strip() for s in args.symbols.split(",")])
    while True:                                               # auto-reconnect
        try:
            await bot.setup(args.real, args.account_id)
            while True:
                await asyncio.sleep(30)
                if bot.auth.task.done() or bot.pub.task.done():
                    raise ConnectionError("socket closed")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            print(f"[reconnect] {e}; retrying in 10s. Open trades keep their TP/SL on Deriv's side.")
            await asyncio.sleep(10)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["backtest", "live", "accounts", "symbols"])
    ap.add_argument("--symbols", default="frxXAUUSD")
    ap.add_argument("--csv")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--account-id")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--multiplier", type=int, default=100)
    ap.add_argument("--tp-atr", type=float, default=1.0)
    ap.add_argument("--sl-atr", type=float, default=1.5)
    ap.add_argument("--min-score", type=int, default=3)
    a = ap.parse_args()
    cfg = Cfg(stake=a.stake, multiplier=a.multiplier, tp_atr=a.tp_atr, sl_atr=a.sl_atr, min_score=a.min_score)

    if a.mode == "accounts":
        cmd_accounts()
    elif a.mode == "symbols":
        cmd_symbols()
    elif a.mode == "backtest":
        if a.csv:
            df = pd.read_csv(a.csv, index_col=0, parse_dates=True)
            df.columns = [c.lower() for c in df.columns]
        else:
            df = synthetic_candles(seed=a.seed)
        print(f"{len(df)} one-minute bars, {df.index[0]:%F} -> {df.index[-1]:%F}")
        print(json.dumps(backtest(df, cfg), indent=2))
    else:
        asyncio.run(run_live(a, cfg))


if __name__ == "__main__":
    main()
