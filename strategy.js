/* Strategy + risk engine. Same logic as deriv_bot.py (works in the browser and in Node for testing). */
(function (root) {
  const DEFAULTS = {
    stake: 1.0, multiplier: 100, tp_atr: 3.0, sl_atr: 3.0, min_score: 3, min_er: 0.20,
    session_start: 7, session_end: 17, be_trigger: 0.5, trail_giveback: 0.35,
    max_hold_bars: 25, cooldown_bars: 3, max_open: 2,
    daily_loss_pct: 5.0, daily_profit_pct: 3.0, loss_streak: 3, pause_minutes: 30,
  };
  const MIN_BARS = 160;

  function ema(a, n) {
    const k = 2 / (n + 1), out = new Array(a.length);
    out[0] = a[0];
    for (let i = 1; i < a.length; i++) out[i] = a[i] * k + out[i - 1] * (1 - k);
    return out;
  }

  function rsi(c, n) {
    const al = 1 / n, out = new Array(c.length).fill(NaN);
    let up = null, dn = null;
    for (let i = 1; i < c.length; i++) {
      const d = c[i] - c[i - 1], u = Math.max(d, 0), v = Math.max(-d, 0);
      if (up === null) { up = u; dn = v; } else { up = u * al + up * (1 - al); dn = v * al + dn * (1 - al); }
      out[i] = dn === 0 ? NaN : 100 - 100 / (1 + up / dn);
    }
    return out;
  }

  function atr(b, n = 14) {
    const al = 1 / n, out = new Array(b.length);
    let prev = null;
    for (let i = 0; i < b.length; i++) {
      let tr = b[i].h - b[i].l;
      if (i > 0) tr = Math.max(tr, Math.abs(b[i].h - b[i - 1].c), Math.abs(b[i].l - b[i - 1].c));
      prev = prev === null ? tr : tr * al + prev * (1 - al);
      out[i] = prev;
    }
    return out;
  }

  function efficiency(c, n = 20) {
    const i = c.length - 1;
    if (i < n) return NaN;
    let path = 0;
    for (let j = i - n + 1; j <= i; j++) path += Math.abs(c[j] - c[j - 1]);
    return path === 0 ? NaN : Math.abs(c[i] - c[i - n]) / path;
  }

  /* bars: [{t,o,h,l,c}] completed candles. Returns {dir, score, atr} */
  function signal(bars, cfg) {
    if (bars.length < MIN_BARS) return { dir: 0, score: 0, atr: 0 };
    const c = bars.map((x) => x.c), n = c.length;
    const a = atr(bars)[n - 1], er = efficiency(c);
    if (!isFinite(a) || a <= 0 || !isFinite(er) || er < cfg.min_er) return { dir: 0, score: 0, atr: a };
    const e9 = ema(c, 9)[n - 1], e21 = ema(c, 21)[n - 1], e50 = ema(c, 50)[n - 1];
    const r = rsi(c, 7);
    const rNow = r[n - 1], rPrev = r[n - 2];
    const win = r.slice(n - 4, n - 1);
    const rLo = Math.min(...win), rHi = Math.max(...win);
    const prior = bars.slice(n - 13, n - 1);
    const hi12 = Math.max(...prior.map((x) => x.h)), lo12 = Math.min(...prior.map((x) => x.l));
    const htfUp = ema(c, 60)[n - 1] > ema(c, 150)[n - 1];
    const last = c[n - 1];
    const bigBar = bars[n - 1].h - bars[n - 1].l > 0.9 * a;

    const longPts = [e9 > e21 && e21 > e50, rLo < 40 && rNow > rPrev, last > hi12 && bigBar, htfUp].filter(Boolean).length;
    const shortPts = [e9 < e21 && e21 < e50, rHi > 60 && rNow < rPrev, last < lo12 && bigBar, !htfUp].filter(Boolean).length;
    if (longPts >= cfg.min_score && longPts > shortPts) return { dir: 1, score: longPts, atr: a };
    if (shortPts >= cfg.min_score && shortPts > longPts) return { dir: -1, score: shortPts, atr: a };
    return { dir: 0, score: 0, atr: a };
  }

  function dollarLevels(price, a, cfg) {
    const perAtr = (cfg.stake * cfg.multiplier * a) / price;
    const tp = Math.min(Math.max(perAtr * cfg.tp_atr, 0.1 * cfg.stake), 2.0 * cfg.stake);
    const sl = Math.min(Math.max(perAtr * cfg.sl_atr, 0.1 * cfg.stake), 0.9 * cfg.stake);
    return { tp: Math.round(tp * 100) / 100, sl: Math.round(sl * 100) / 100 };
  }

  function manageExit(profit, peak, tp, barsHeld, cfg) {
    if (peak >= cfg.be_trigger * tp) {
      if (profit <= 0.03 * cfg.stake) return true;
      if (profit <= peak * (1 - cfg.trail_giveback)) return true;
    }
    return barsHeld >= cfg.max_hold_bars;
  }

  class Risk {
    constructor(cfg, balance) { this.cfg = cfg; this.start = balance; this.day = null; this.dayPnl = 0; this.streak = 0; this.pausedUntil = 0; }
    roll(ts) { const d = new Date(ts * 1000).toISOString().slice(0, 10); if (d !== this.day) { this.day = d; this.dayPnl = 0; this.streak = 0; } }
    canTrade(ts, openCount) {
      this.roll(ts);
      const pct = (this.dayPnl / this.start) * 100;
      if (pct <= -this.cfg.daily_loss_pct) return [false, 'Daily loss limit hit - stopped for today'];
      if (pct >= this.cfg.daily_profit_pct) return [false, 'Daily profit target hit - cashed out for today'];
      if (ts < this.pausedUntil) return [false, 'Paused after losing streak'];
      if (openCount >= this.cfg.max_open) return [false, 'Max open trades'];
      return [true, ''];
    }
    onClose(pnl, ts) {
      this.roll(ts);
      this.dayPnl += pnl;
      this.streak = pnl < 0 ? this.streak + 1 : 0;
      if (this.streak >= this.cfg.loss_streak) { this.pausedUntil = ts + this.cfg.pause_minutes * 60; this.streak = 0; }
    }
  }

  function pickMultiplier(avail, want) {
    let allowed = [];
    for (const c of avail || []) {
      if (c && c.contract_type === 'MULTUP') {
        const mr = c.multiplier_range || c.multipliers || [];
        allowed = mr.map(Number).filter((x) => Number.isFinite(x));
      }
    }
    if (!allowed.length) return want;
    return allowed.reduce((best, x) => (Math.abs(x - want) < Math.abs(best - want) ? x : best), allowed[0]);
  }

  const api = { DEFAULTS, MIN_BARS, signal, dollarLevels, manageExit, Risk, pickMultiplier, ema, rsi, atr };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.Strat = api;
})(typeof window !== 'undefined' ? window : globalThis);
