"""QQQ 10am model.

Rules (config.TENAM_*):
  1. Do nothing before 10:00 ET. O10 = open of the 10:00 1-minute bar.
  2. Watch for a wick into a liquidity level: the 9:30-10:00 high/low or the premarket high/low.
       wick below a low  -> long setup     wick above a high -> short setup
  3. Then a 1-minute close back through O10 (above it for longs, below it for shorts).
     The manipulation extreme is the furthest price reached from the sweep up to that close.
  4. Track the extreme after that close. When O10 sits between the 0.62 and 0.79 retracement
     of the manipulation-to-extreme range (checked at each 1-minute close), place ONE bracket:
     limit at O10, stop $0.05 beyond the manipulation extreme, take-profit at the nearer of the
     prior day's high/low and 3R. Size = $250 risk / stop distance, within the hard limits.
  5. Cancel an unfilled order at 11:00 ET. One trade per day. Flatten at 15:55 ET.
  Anything unclear (missing data, both sides firing at once, a jump past the zone, a hard
  limit in the way, shorting disabled) means no trade, and the reason is logged.

Definitions used where the rules leave room:
  - "Wick into" a level: a 1-minute bar (10:00 or later) trades beyond it (low below a low /
    high above a high). If several levels are taken at once, the furthest one is recorded.
  - Only untouched levels count: highs must be above O10 and lows below it, and a premarket
    level the 9:30-10:00 session already traded through is no longer liquidity.
  - The extreme starts at the close-through bar's close and then follows later bars.
  - If price goes back beyond the manipulation extreme before an order is placed, that side is done.
  - Live data is Alpaca's free IEX 1-minute feed, plus the consolidated (SIP) feed for the
    premarket (IEX has none; SIP is free once 15 minutes old). Replays use SIP throughout.

Run:  python tenam.py                  live (waits for 10:00, places at most one paper order)
      python tenam.py --dry-run        live data, logs what it would do, places nothing
      python tenam.py --scheduled      how GitHub runs it (9:40-10:30 start window, stops after 11:00)
      python tenam.py --flatten        close an open 10am trade (GitHub's 15:55 job; starts ~15:00 and waits)
      python tenam.py --replay 2026-11-06            replay one past day
      python tenam.py --replay 2025-01-01 2025-12-31  replay a date range and summarise
"""

import os
import sys

if sys.stderr is None:  # pythonw (Task Scheduler): no console
    sys.stdout = sys.stderr = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_errors.log"), "a", encoding="utf-8"
    )

import argparse
import math
import time
from datetime import timedelta, timezone

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, LimitOrderRequest, StopLossRequest, TakeProfitRequest

import bot
import config
import journal

log = bot.log
SYMBOL = config.TENAM_SYMBOL
ET = "America/New_York"


# --------------------------------------------------------------------------- setup engine

class Side:
    """One direction of the setup. long = sweep of a low, short = sweep of a high."""

    def __init__(self, direction, levels, o10):
        self.direction = direction
        self.levels = {name: price for name, price in levels.items() if price is not None}
        self.o10 = o10
        self.phase = "wait_sweep"
        self.manip = self.extreme = self.retrace = None
        self.sweep = {}
        self.reclaim_time = None
        self.note = ""

    def _beyond(self, a, b):
        """a is further in the manipulation direction than b."""
        return a < b if self.direction == "long" else a > b

    def on_bar(self, t, bar):
        """Feed one completed 1-minute bar. Returns 'ready', 'invalid', or None."""
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        grab = l if self.direction == "long" else h      # manipulation-side extreme of the bar
        run = h if self.direction == "long" else l       # displacement-side extreme of the bar
        rng = h - l
        if self.phase in ("ready", "done"):
            return None

        if self.phase == "wait_sweep":
            taken = {n: p for n, p in self.levels.items() if self._beyond(grab, p)}
            if not taken:
                return None
            name = (min if self.direction == "long" else max)(taken, key=taken.get)
            wick = (min(o, c) - l) if self.direction == "long" else (h - max(o, c))
            self.sweep = {"level": name, "price": taken[name], "time": t,
                          "wick_ratio": round(wick / rng, 2) if rng > 0 else 0.0}
            self.manip = grab
            self.phase = "wait_reclaim"
            self.note = f"wick {'below' if self.direction == 'long' else 'above'} the {name} ({taken[name]:.2f})"

        if self.phase == "wait_reclaim":
            if self._beyond(grab, self.manip):
                self.manip = grab
            if (c > self.o10) if self.direction == "long" else (c < self.o10):
                self.phase = "track"
                self.extreme = c
                self.reclaim_time = t
                self.note = f"1-min close {c:.2f} back {'above' if self.direction == 'long' else 'below'} O10"
            return None

        # phase == "track"
        if self._beyond(grab, self.manip):
            self.phase = "done"
            self.note = f"price went back beyond the manipulation extreme ({self.manip:.2f}) before an order; setup invalid"
            return "invalid"
        self.extreme = max(self.extreme, run) if self.direction == "long" else min(self.extreme, run)
        span = abs(self.extreme - self.manip)
        r = abs(self.extreme - self.o10) / span if span > 0 else 0.0
        if config.TENAM_FIB_LOW <= r <= config.TENAM_FIB_HIGH:
            self.phase, self.retrace = "ready", r
            self.note = f"O10 at a {r:.3f} retracement of {self.manip:.2f} -> {self.extreme:.2f}"
            return "ready"
        if r > config.TENAM_FIB_HIGH:
            self.phase = "done"
            self.note = (f"O10 retracement jumped to {r:.3f}, past {config.TENAM_FIB_HIGH} without a 1-min close "
                         f"inside the zone; no trade")
            return "invalid"
        return None


def live_levels(o10, rth_high, rth_low, pm_high, pm_low):
    """Liquidity still untouched at 10:00: highs above O10 and lows below it. A premarket level
    the 9:30-10:00 session already traded through has been taken and no longer counts."""
    highs, lows, dropped = {}, {}, []
    if rth_high > o10:
        highs["9:30-10:00 high"] = rth_high
    else:
        dropped.append(f"9:30-10:00 high {rth_high:.2f} (not above O10)")
    if rth_low < o10:
        lows["9:30-10:00 low"] = rth_low
    else:
        dropped.append(f"9:30-10:00 low {rth_low:.2f} (not below O10)")
    if pm_high is not None:
        if pm_high > max(rth_high, o10):
            highs["premarket high"] = pm_high
        else:
            dropped.append(f"premarket high {pm_high:.2f} (already taken before 10:00)")
    if pm_low is not None:
        if pm_low < min(rth_low, o10):
            lows["premarket low"] = pm_low
        else:
            dropped.append(f"premarket low {pm_low:.2f} (already taken before 10:00)")
    return highs, lows, dropped


class Model:
    def __init__(self, o10, rth_high, rth_low, pm_high, pm_low):
        self.o10 = o10
        highs, lows, self.dropped = live_levels(o10, rth_high, rth_low, pm_high, pm_low)
        self.sides = [Side("long", lows, o10), Side("short", highs, o10)]

    def on_bar(self, t, bar, say):
        ready = []
        for side in self.sides:
            before = side.phase
            result = side.on_bar(t, bar)
            if side.phase != before:
                say(f"{t:%H:%M} {side.direction}: {side.note}")
            if result == "ready":
                ready.append(side)
        return ready


def plan_trade(side, prior_high, prior_low):
    """Entry, stop, target for a ready side. Returns (plan, None) or (None, reason)."""
    entry = round(side.o10, 2)
    if side.direction == "long":
        stop = round(side.manip - config.TENAM_STOP_BUFFER, 2)
        risk = entry - stop
        r_target = entry + config.TENAM_TARGET_R * risk
        level, level_name = prior_high, "prior-day high"
        level_ok = level is not None and level > entry
        target = min(level, r_target) if level_ok else r_target
    else:
        stop = round(side.manip + config.TENAM_STOP_BUFFER, 2)
        risk = stop - entry
        r_target = entry - config.TENAM_TARGET_R * risk
        level, level_name = prior_low, "prior-day low"
        level_ok = level is not None and level < entry
        target = max(level, r_target) if level_ok else r_target
    if risk <= 0:
        return None, f"stop {stop} is not beyond entry {entry}"
    target = round(target, 2)
    if level_ok and target == round(level, 2):
        basis = f"{level_name} {level:.2f}"
    elif level_ok:
        basis = f"{config.TENAM_TARGET_R:g}R (nearer than the {level_name} {level:.2f})"
    else:
        basis = f"{config.TENAM_TARGET_R:g}R (the {level_name} is already behind entry)"
    rr = abs(target - entry) / risk
    return {"entry": entry, "stop": stop, "target": target, "risk_ps": round(risk, 2),
            "planned_rr": round(rr, 2), "tp_basis": basis}, None


def size(risk_ps, entry, risk_dollars):
    by_risk = math.floor(risk_dollars / risk_ps)
    by_value = math.floor(config.MAX_POSITION_VALUE / entry)
    return max(0, min(by_risk, config.MAX_SHARES_PER_TRADE, by_value))


# --------------------------------------------------------------------------- data

def minute_bars(clients, day, feed, end=None):
    start = pd.Timestamp(f"{day} 04:00", tz=ET)
    stop = end or pd.Timestamp(f"{day} 16:00", tz=ET)
    df = clients.data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Minute, feed=feed, adjustment=Adjustment.RAW,
        start=start.tz_convert("UTC").to_pydatetime(), end=stop.tz_convert("UTC").to_pydatetime(),
    )).df
    if df.empty:
        return df
    df = df.xs(SYMBOL, level="symbol")
    df.index = df.index.tz_convert(ET)
    return df


def premarket_bars(clients, day):
    """IEX has no premarket trading, so live runs read 4:00-9:30 from the consolidated (SIP)
    feed, which free accounts can use once it is 15+ minutes old (it is by 10:00)."""
    try:
        return minute_bars(clients, day, DataFeed.SIP, end=pd.Timestamp(f"{day} 09:30", tz=ET))
    except Exception as e:
        log.warning(f"10am: premarket bars unavailable ({e}); premarket levels not used.")
        return pd.DataFrame()


def opening_levels(bars, day, pre=None):
    """O10 and the liquidity levels. Returns (dict, None) or (None, reason)."""
    t930, t1000 = pd.Timestamp(f"{day} 09:30", tz=ET), pd.Timestamp(f"{day} 10:00", tz=ET)
    if pre is None:
        pre = bars[bars.index < t930]
    rth = bars[(bars.index >= t930) & (bars.index < t1000)]
    if t1000 not in bars.index:
        return None, "no 10:00 1-minute bar, so O10 is unknown"
    if len(rth) < 20:
        return None, f"only {len(rth)} of 30 bars between 9:30 and 10:00, so the opening range is unclear"
    return {
        "o10": float(bars.loc[t1000, "open"]),
        "rth_high": float(rth["high"].max()), "rth_low": float(rth["low"].min()),
        "pm_high": float(pre["high"].max()) if len(pre) else None,
        "pm_low": float(pre["low"].min()) if len(pre) else None,
    }, None


def describe_levels(lv):
    pm = (f"premarket {lv['pm_low']:.2f}-{lv['pm_high']:.2f}" if lv["pm_high"] is not None
          else "premarket: no bars (premarket levels not used)")
    return f"O10 {lv['o10']:.2f} | 9:30-10:00 {lv['rth_low']:.2f}-{lv['rth_high']:.2f} | {pm}"


# --------------------------------------------------------------------------- live trading

def et_time(day, hhmm):
    return pd.Timestamp(f"{day} {hhmm}", tz=ET)


def flatten_time(clients, day):
    """15:55 ET, or 5 minutes before the close on a half day (13:00 closes, e.g. Nov 27, Dec 24)."""
    normal = et_time(day, config.TENAM_FLATTEN_AT)
    try:
        cal = clients.trading.get_calendar(GetCalendarRequest(start=day, end=day))
        close = pd.Timestamp(cal[0].close).tz_localize(ET)  # Alpaca gives naive ET times
        return min(normal, close - pd.Timedelta(minutes=5))
    except Exception:
        log.warning("10am: could not read today's close from Alpaca; using the normal flatten time.")
        return normal


def sleep_until(ts):
    while (wait := (ts - bot.now_et()).total_seconds()) > 0:
        time.sleep(min(wait, 30))


def fresh_state():
    return journal.load_state()


def build_context(side, plan, lv, daily, headlines, placed_at):
    return {
        "session": journal.session_label(placed_at),
        "direction": side.direction,
        "o10": round(lv["o10"], 2),
        "swept_level": side.sweep["level"], "swept_price": round(side.sweep["price"], 2),
        "sweep_time": f"{side.sweep['time']:%H:%M}", "reclaim_time": f"{side.reclaim_time:%H:%M}",
        "manip": round(side.manip, 2), "extreme": round(side.extreme, 2),
        "fib_retrace": round(side.retrace, 3), "fib_level": journal.fib_label(side.retrace), "in_ote": True,
        "wick_ratio": side.sweep["wick_ratio"], "wick_strength": journal.wick_strength(side.sweep["wick_ratio"]),
        "liquidity": f"took the {side.sweep['level']} ({side.sweep['price']:.2f}) at {side.sweep['time']:%H:%M}",
        "prior_high": round(float(daily["High"].iloc[-1]), 2), "prior_low": round(float(daily["Low"].iloc[-1]), 2),
        "levels": {k: (round(v, 2) if v is not None else None) for k, v in lv.items()},
        "placed_at": f"{placed_at:%H:%M} ET", "placed_minute": placed_at.hour * 60 + placed_at.minute,
        "news_count": len(headlines), "headlines": headlines[:3],
        **{k: plan[k] for k in ("risk_ps", "planned_rr", "tp_basis")},
        **journal.daily_trend(daily, lv["o10"]),
    }


def place(clients, side, lv, daily, dry_run):
    """All gates, then the bracket order. Returns True if an order was placed."""
    client = clients.trading
    day = bot.now_et().date().isoformat()
    now = bot.now_et()
    state = fresh_state()
    plan, why = plan_trade(side, float(daily["High"].iloc[-1]), float(daily["Low"].iloc[-1]))
    if not plan:
        log.info(f"10am: NO TRADE: {why}.")
        return False

    account = client.get_account()
    today_pnl = bot.day_pnl(account)
    risk_dollars = min(config.TENAM_RISK_PER_TRADE, config.MAX_DAILY_LOSS + today_pnl)
    qty = size(plan["risk_ps"], plan["entry"], risk_dollars)
    context = build_context(side, plan, lv, daily, bot.recent_headlines(clients, SYMBOL), now)
    blocked = journal.blocking_rules(context, journal.approved_rules(), journal.TENAM_CONDITIONS)

    reasons = []
    if now >= et_time(day, config.TENAM_CANCEL_AT):
        reasons.append(f"it is past {config.TENAM_CANCEL_AT} ET")
    if state.get("tenam_trades_by_day", {}).get(day, 0) >= config.TENAM_MAX_TRADES_PER_DAY:
        reasons.append("the 10am model already traded today")
    if state["entries_by_day"].get(day, 0) >= config.MAX_TRADES_PER_DAY:
        reasons.append(f"max {config.MAX_TRADES_PER_DAY} trades per day reached")
    if today_pnl <= -config.MAX_DAILY_LOSS:
        reasons.append(f"daily loss limit hit ({today_pnl:+.2f})")
    if SYMBOL in state["open"] or bot.held_qty(client, SYMBOL) > 0:
        reasons.append(f"already holding or tracking {SYMBOL}")
    if side.direction == "short" and not account.shorting_enabled:
        reasons.append("shorting is disabled on the Alpaca account")
    if qty < 1:
        reasons.append(f"size rounds to 0 shares (${risk_dollars:.0f} risk / ${plan['risk_ps']:.2f} stop)")
    if blocked:
        reasons.append(f"blocked by APPROVED lessons.md rule(s) {', '.join(blocked)}")

    log.info(
        f"10am: {side.direction.upper()} setup ready: limit {plan['entry']:.2f}, stop {plan['stop']:.2f}, "
        f"target {plan['target']:.2f} ({plan['tp_basis']}), {qty} sh, risk ${qty * plan['risk_ps']:.2f}."
    )
    if reasons:
        log.info(f"10am: NO TRADE: {'; '.join(reasons)}.")
        return False

    mark = journal.MET
    back = "above" if side.direction == "long" else "below"
    gates = [
        [f"Did nothing before 10:00 ET; O10 = open of the 10:00 1-min bar ({lv['o10']:.2f})", mark],
        [f"Wick into the {side.sweep['level']} ({side.sweep['price']:.2f}) after 10:00", mark],
        [f"1-min close back {back} O10 after the sweep ({side.reclaim_time:%H:%M})", mark],
        [f"O10 between the {config.TENAM_FIB_LOW} and {config.TENAM_FIB_HIGH} retracement ({side.retrace:.3f})", mark],
        [f"Order placed before {config.TENAM_CANCEL_AT} ET ({now:%H:%M})", mark],
        [f"One 10am trade per day", mark],
        [f"Hard limit: max {config.MAX_SHARES_PER_TRADE} shares and ${config.MAX_POSITION_VALUE:,.0f} per position "
         f"({qty} sh, ${qty * plan['entry']:,.0f})", mark],
        [f"Hard limit: daily loss under ${config.MAX_DAILY_LOSS:,.0f} (today {today_pnl:+.2f})", mark],
        [f"Hard limit: max {config.MAX_TRADES_PER_DAY} trades per day", mark],
        ["Hard limit: stop at entry (bracket order)", mark],
        ["No APPROVED lessons.md rule blocks the entry", mark],
    ]
    items = journal.checklist(context, gates, journal.TENAM_CONDITIONS)
    grade, grade_reason = journal.grade(items)
    log.info(f"10am: grade {grade} ({grade_reason}).")
    if dry_run:
        log.info("10am: dry run: no order placed.")
        return False

    order = client.submit_order(LimitOrderRequest(
        symbol=SYMBOL, qty=qty, side=OrderSide.BUY if side.direction == "long" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC, limit_price=plan["entry"], order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=plan["target"]),
        stop_loss=StopLossRequest(stop_price=plan["stop"]),
    ))
    log.info(f"10am: submitted {side.direction.upper()} bracket id={order.id} status={order.status.value}")

    state = fresh_state()
    state["entries_by_day"][day] = state["entries_by_day"].get(day, 0) + 1
    state.setdefault("tenam_trades_by_day", {})[day] = state.get("tenam_trades_by_day", {}).get(day, 0) + 1
    state["open"][SYMBOL] = {
        "symbol": SYMBOL, "direction": side.direction, "model": "10am", "qty": qty,
        "setup": journal.TENAM_SETUP, "entry_order_id": str(order.id), "entry": None, "entry_time": None,
        "stop": plan["stop"], "target": plan["target"], "stop_order_id": None, "target_order_id": None,
        "context": context, "checklist": items, "grade": grade, "grade_reason": grade_reason, "adopted": False,
    }
    journal.save_state(state)
    return True


def manage(clients, day, until_flatten=True):
    """Wait for the fill (cancel at 11:00), then let stop/target work until the 15:55 flatten.
    With until_flatten=False (GitHub runs) it stops after 11:00 and leaves the 15:55 close
    to the separate --flatten job; the stop and target stay on Alpaca in the meantime.
    On a half day (13:00 close) it always stays and flattens at 12:55, since the afternoon
    flatten job would start after the close."""
    client = clients.trading
    cancel_at, flatten_at = et_time(day, config.TENAM_CANCEL_AT), flatten_time(clients, day)
    if flatten_at < et_time(day, config.TENAM_FLATTEN_AT):
        until_flatten = True
    while True:
        state = fresh_state()
        bot.reconcile(clients, state)
        state = fresh_state()
        trade = state["open"].get(SYMBOL)
        if not trade or trade.get("model") != "10am":
            log.info("10am: no open 10am trade left. Done for the day.")
            return
        now = bot.now_et()
        if trade["entry"] is None and now >= cancel_at:
            client.cancel_order_by_id(trade["entry_order_id"])
            time.sleep(3)
            order = client.get_order_by_id(trade["entry_order_id"])
            filled = float(order.filled_qty or 0)
            if filled > 0:
                trade["qty"] = filled
                trade["entry"] = round(float(order.filled_avg_price), 2)
                trade["entry_time"] = journal.to_et(order.filled_at or bot.now_et()).isoformat()
                trade["stop_order_id"], trade["target_order_id"] = bot.bracket_leg_ids(client, trade["entry_order_id"])
                journal.save_state(state)
                log.info(f"10am: {config.TENAM_CANCEL_AT}: entry partly filled ({filled:g} sh); rest cancelled.")
            else:
                del state["open"][SYMBOL]
                journal.save_state(state)
                log.info(f"10am: {config.TENAM_CANCEL_AT}: entry never filled; order cancelled.")
                return
        if trade["entry"] is not None and now >= flatten_at:
            bot.exit_trade(clients, state, SYMBOL, False, reason=f"End-of-day flatten ({flatten_at:%H:%M})")
            return
        if not until_flatten and trade["entry"] is not None and now >= cancel_at:
            log.info(f"10am: in the trade; stop and target are on Alpaca. The {config.TENAM_FLATTEN_AT} job closes it if still open.")
            return
        time.sleep(15)


def flatten_open_trade(clients, reason):
    """Close (or cancel) an open 10am trade. Returns True if there was one."""
    state = fresh_state()
    bot.reconcile(clients, state)
    state = fresh_state()
    trade = state["open"].get(SYMBOL)
    if not trade or trade.get("model") != "10am":
        return False
    if trade["entry"] is None:
        clients.trading.cancel_order_by_id(trade["entry_order_id"])
        del state["open"][SYMBOL]
        journal.save_state(state)
        log.info(f"10am: cancelled an unfilled entry order ({reason}).")
    else:
        bot.exit_trade(clients, state, SYMBOL, False, reason=reason)
    return True


FLATTEN_START_WINDOW = ("14:45", "15:58")


def run_flatten(scheduled):
    """The 15:55 job on GitHub. GitHub often starts scheduled jobs 30-50 minutes late, so the
    job starts from about 15:00 ET and waits for 15:55 itself. Starts outside 14:45-15:58 ET
    (the other daylight-saving season's UTC times) exit at once, and so does any day with no
    open 10am trade, so the wait only costs Actions minutes when there is a trade to close."""
    now = bot.now_et()
    day = now.date().isoformat()
    lo, hi = (et_time(day, t) for t in FLATTEN_START_WINDOW)
    if scheduled and not (lo <= now < hi):
        print(f"{now:%H:%M} ET is outside the {FLATTEN_START_WINDOW[0]}-{FLATTEN_START_WINDOW[1]} flatten start window; skipping.")
        return
    trade = fresh_state()["open"].get(SYMBOL, {})
    if trade.get("model") != "10am":
        print("No open 10am trade; nothing to flatten.")
        return
    bot.setup_logging()
    try:
        log.info("===== 10am flatten job =====")
        clients = bot.Clients()
        flatten_at = flatten_time(clients, day)
        if bot.now_et() < flatten_at:
            bot.reconcile(clients, fresh_state())  # it may already have hit its stop or target
            if fresh_state()["open"].get(SYMBOL, {}).get("model") != "10am":
                log.info("10am: the trade already closed at its stop or target; nothing to flatten.")
                return
            log.info(f"10am: trade still open; waiting until {flatten_at:%H:%M} ET to close it.")
            sleep_until(flatten_at)
        if not flatten_open_trade(clients, f"End-of-day flatten ({flatten_at:%H:%M})"):
            log.info("10am: the trade had already closed.")
    except Exception:
        log.exception("10am: flatten failed")
        sys.exit(1)


def run_live(args):
    clients = bot.Clients()
    day = bot.now_et().date().isoformat()
    log.info(f"===== 10am model started ({SYMBOL}{', dry run' if args.dry_run else ''}) =====")
    state = fresh_state()
    journal.ensure_lessons_file()
    journal.session_briefing(state)
    clock = clients.trading.get_clock()
    if bot.market_closed_today(clients.trading, clock):
        log.info("10am: market is closed today. Nothing to do.")
        return
    bot.reconcile(clients, state)
    until_flatten = not args.scheduled
    open_trade = fresh_state()["open"].get(SYMBOL, {})
    if open_trade.get("model") == "10am":
        opened = open_trade.get("entry_time")
        if opened and journal.to_et(opened).date().isoformat() < day and clock.is_open:
            log.info("10am: a trade from a previous day is still open (missed 15:55 close); closing it now.")
            flatten_open_trade(clients, "Flatten of a previous day's trade (missed the 15:55 close)")
        else:
            log.info("10am: a 10am trade is already open; managing it.")
            return manage(clients, day, until_flatten)
    if fresh_state().get("tenam_trades_by_day", {}).get(day, 0) >= config.TENAM_MAX_TRADES_PER_DAY:
        log.info("10am: already traded today. Nothing to do.")
        return

    cancel_at = et_time(day, config.TENAM_CANCEL_AT)
    if bot.now_et() >= cancel_at:
        log.info(f"10am: started after {config.TENAM_CANCEL_AT} ET. Nothing to do today.")
        return
    sleep_until(et_time(day, "10:01:03"))  # do nothing before 10:00; first bar completes at 10:01

    daily = bot.fetch_bars(SYMBOL, config.LOOKBACK_DAYS, clients)
    log.info(f"10am: prior day {daily.index[-1].date()} high {daily['High'].iloc[-1]:.2f}, low {daily['Low'].iloc[-1]:.2f}")
    model, lv, seen = None, None, set()
    while bot.now_et() < cancel_at:
        now = bot.now_et()
        bars = minute_bars(clients, day, DataFeed.IEX, end=now)
        bars = bars[bars.index + pd.Timedelta(minutes=1) <= now] if not bars.empty else bars
        if model is None:
            lv, why = opening_levels(bars, day, premarket_bars(clients, day))
            if not lv:
                log.info(f"10am: NO TRADE: {why}.")
                return
            log.info(f"10am: {describe_levels(lv)}")
            model = Model(lv["o10"], lv["rth_high"], lv["rth_low"], lv["pm_high"], lv["pm_low"])
            if model.dropped:
                log.info(f"10am: levels not in play: {'; '.join(model.dropped)}")
        for t, bar in bars[bars.index >= et_time(day, "10:00")].iterrows():
            if t in seen:
                continue
            seen.add(t)
            ready = model.on_bar(t, bar, lambda msg: log.info(f"10am: {msg}"))
            if len(ready) > 1:
                log.info("10am: NO TRADE: long and short setups completed on the same bar; unclear.")
                return
            if ready:
                completed = t + pd.Timedelta(minutes=1)
                if bot.now_et() - completed > pd.Timedelta(minutes=3):
                    log.info(f"10am: NO TRADE: the setup completed at {completed:%H:%M}, before the bot was "
                             f"watching (now {bot.now_et():%H:%M}); too late to act on it.")
                    return
                if place(clients, ready[0], lv, daily, args.dry_run):
                    return manage(clients, day, until_flatten)
                return
            if all(s.phase == "done" for s in model.sides):
                log.info("10am: NO TRADE: both sides invalidated.")
                return
        next_min = (bot.now_et().floor("min") + pd.Timedelta(minutes=1, seconds=3))
        sleep_until(min(next_min, cancel_at))
    log.info(f"10am: NO TRADE: no setup completed before {config.TENAM_CANCEL_AT} ET.")


# --------------------------------------------------------------------------- replay

def replay_day(day, bars, daily, verbose):
    """Simulate one past day. Returns a result dict."""
    say = (lambda m: print(f"  {m}")) if verbose else (lambda m: None)
    lv, why = opening_levels(bars, day)
    if not lv:
        say(f"NO TRADE: {why}")
        return {"day": day, "result": "no data", "why": why}
    prior = daily[daily.index.date < pd.Timestamp(day).date()]
    if prior.empty:
        return {"day": day, "result": "no data", "why": "no prior day"}
    ph, pl = float(prior["High"].iloc[-1]), float(prior["Low"].iloc[-1])
    say(describe_levels(lv) + f" | prior day {pl:.2f}-{ph:.2f}")
    model = Model(lv["o10"], lv["rth_high"], lv["rth_low"], lv["pm_high"], lv["pm_low"])
    if model.dropped:
        say(f"levels not in play: {'; '.join(model.dropped)}")
    cancel_at, flatten_at = et_time(day, config.TENAM_CANCEL_AT), et_time(day, config.TENAM_FLATTEN_AT)
    session = bars[bars.index >= et_time(day, "10:00")]
    side = None
    for t, bar in session[session.index < cancel_at].iterrows():
        ready = model.on_bar(t, bar, say)
        if len(ready) > 1:
            say("NO TRADE: long and short completed on the same bar")
            return {"day": day, "result": "no trade", "why": "both sides at once"}
        if ready:
            side, placed_t = ready[0], t + pd.Timedelta(minutes=1)
            break
        if all(s.phase == "done" for s in model.sides):
            break
    if not side:
        say("NO TRADE: no setup before 11:00")
        return {"day": day, "result": "no trade", "why": "no setup"}
    plan, why = plan_trade(side, ph, pl)
    if not plan:
        say(f"NO TRADE: {why}")
        return {"day": day, "result": "no trade", "why": why}
    long_ = side.direction == "long"
    say(f"{placed_t:%H:%M} ORDER {side.direction.upper()} limit {plan['entry']:.2f} stop {plan['stop']:.2f} "
        f"target {plan['target']:.2f} ({plan['tp_basis']}), planned {plan['planned_rr']:.2f}R, "
        f"{size(plan['risk_ps'], plan['entry'], config.TENAM_RISK_PER_TRADE)} sh")
    after = bars[bars.index >= placed_t]
    fill_t = None
    for t, bar in after[after.index < cancel_at].iterrows():
        if (bar["low"] <= plan["entry"]) if long_ else (bar["high"] >= plan["entry"]):
            fill_t = t
            break
    if fill_t is None:
        say("unfilled; cancelled at 11:00")
        return {"day": day, "result": "unfilled", "direction": side.direction, **plan}
    exit_price, reason = None, None
    for t, bar in after[(after.index >= fill_t) & (after.index < flatten_at)].iterrows():
        hit_stop = (bar["low"] <= plan["stop"]) if long_ else (bar["high"] >= plan["stop"])
        hit_tp = (bar["high"] >= plan["target"]) if long_ else (bar["low"] <= plan["target"])
        if hit_stop:  # if both in one bar, assume the stop came first (conservative)
            exit_price, reason, exit_t = plan["stop"], "stop", t
            break
        if hit_tp and t > fill_t:
            exit_price, reason, exit_t = plan["target"], "target", t
            break
    if exit_price is None:
        last = after[after.index < flatten_at]
        exit_price, reason, exit_t = float(last["close"].iloc[-1]), "flatten 15:55", last.index[-1]
    r = ((exit_price - plan["entry"]) if long_ else (plan["entry"] - exit_price)) / plan["risk_ps"]
    say(f"{fill_t:%H:%M} filled; {exit_t:%H:%M} exit {exit_price:.2f} ({reason}) = {r:+.2f}R")
    return {"day": day, "result": "trade", "direction": side.direction, "r": r, "exit": reason, **plan}


def run_replay(start, end):
    clients = bot.Clients()
    days = [d.date().isoformat() for d in pd.bdate_range(start, end)]
    daily = bot.alpaca_daily_bars(clients, SYMBOL, (pd.Timestamp(start) - pd.Timedelta(days=10)).to_pydatetime())
    trading_days = {d.isoformat() for d in daily.index.date}
    results = []
    for day in days:
        if day not in trading_days:
            continue
        bars = minute_bars(clients, day, DataFeed.SIP)
        verbose = len(days) <= 5
        if verbose:
            print(f"{day}:")
        results.append(replay_day(day, bars, daily, verbose))
    if len(days) <= 5:
        return
    trades = [r for r in results if r["result"] == "trade"]
    counts = pd.Series([r["result"] for r in results]).value_counts().to_dict()
    print(f"{SYMBOL} 10am model, {start} to {end}: {len(results)} trading days -> {counts}")
    if not trades:
        return
    df = pd.DataFrame(trades)
    wins, losses = df[df.r > 0], df[df.r <= 0]
    avg_win = wins.r.mean() if len(wins) else 0.0
    avg_loss = -losses.r.mean() if len(losses) else 0.0
    print(f"Trades {len(df)} | win rate {len(wins) / len(df):.0%} | avg win {avg_win:+.2f}R | avg loss {-avg_loss:+.2f}R | "
          f"realized R:R {'1:%.2f' % (avg_win / avg_loss) if avg_loss else 'n/a'} | expectancy {df.r.mean():+.2f}R | total {df.r.sum():+.2f}R")
    for d, g in df.groupby("direction"):
        print(f"  {d:5}: {len(g)} trades, win rate {(g.r > 0).mean():.0%}, avg {g.r.mean():+.2f}R, total {g.r.sum():+.2f}R")
    print("  exits:", df["exit"].value_counts().to_dict())
    print(f"  at ${config.TENAM_RISK_PER_TRADE:.0f}/trade (before size caps): about ${df.r.sum() * config.TENAM_RISK_PER_TRADE:+,.0f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="live data, no orders")
    parser.add_argument("--replay", nargs="+", metavar="DATE", help="replay one day, or a start and end date")
    parser.add_argument("--scheduled", action="store_true",
                        help="GitHub run: start only between 9:40 and 10:30 ET, once a day; stop after 11:00")
    parser.add_argument("--flatten", action="store_true", help="close an open 10am trade (the 15:55 job)")
    args = parser.parse_args()
    if args.replay:
        run_replay(args.replay[0], args.replay[-1])
        return
    if args.flatten:
        run_flatten(args.scheduled)
        return
    if args.scheduled:
        # GitHub's UTC schedule ignores daylight saving and can start late; only the first
        # start inside the window runs, and only once per day.
        now, state = bot.now_et(), fresh_state()
        if not (9 * 60 + 40 <= now.hour * 60 + now.minute < 10 * 60 + 30):
            print(f"{now:%H:%M} ET is outside the 9:40-10:30 start window; skipping.")
            return
        if state.get("tenam_last_start") == now.date().isoformat():
            print(f"Already started today ({now.date()}); skipping.")
            return
        state["tenam_last_start"] = now.date().isoformat()
        journal.save_state(state)
    bot.setup_logging()
    try:
        run_live(args)
    except Exception:
        log.exception("10am: run failed")
        sys.exit(1)
    finally:
        log.info("===== 10am model finished =====")


if __name__ == "__main__":
    main()
