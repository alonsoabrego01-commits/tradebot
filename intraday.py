"""Intraday models. Both act on completed 5-minute bars, take at most one trade a day each,
enter with a bracket (stop + target), and are flat by 15:55 ET.

QQQ inverse fair value gap with the order flow (config.IFVG_*):
  1. Structure is read on 5-minute bars from 8:00 ET. A swing high/low is a bar whose high/low
     beats the two bars on each side. Order flow is bullish after a 5-minute close above the
     latest swing high and bearish after a close below the latest swing low.
  2. A fair value gap is a 3-bar gap: a bar's low above the high two bars back (bullish) or its
     high below the low two bars back (bearish).
  3. Setup: with bullish order flow, a 5-minute bar closes above the top of a bearish gap formed
     in the last hour (the gap is now "inverse"). Mirror image for shorts.
  4. Entry: a limit order at the retest of that gap's edge, for setups completing 10:00-12:00.
     It is cancelled after 45 minutes, or sooner if order flow turns the other way. Only one
     order rests at a time; the first fill is the day's trade.
  5. Stop $0.05 beyond the extreme of the leg since the gap formed (at least 0.05% of price
     away). Target 2R.

SPY liquidity sweep and reversal (config.SWEEP_*):
  1. Levels: the premarket (4:00-9:15 ET) high and low and the prior day's high and low.
     A high only counts if it is above the 9:30 open, a low only if it is below it.
  2. Sweep: a 5-minute bar trades beyond a level. Reclaim: that bar or one of the next two
     closes back inside it. Below a low then back above = long; above a high then back below = short.
  3. Entry on the reclaim bar's close, completed between 9:35 and 11:30. Each level gives
     one setup.
  4. Stop $0.05 beyond the sweep's extreme (at least 0.05% of price away). Target 3R.

Size = INTRADAY_RISK_PER_TRADE / stop distance, inside INTRADAY_POSITION_VALUE and the hard
limits, and never more than the account's unused equity (no margin). Anything unclear
(missing bars, a signal that completed before the bot was watching, a limit in the way,
shorting disabled) means no trade, and the reason is logged.

Definitions used where the rules leave room:
  - The prior day's high and low come from its regular-session minute bars (unadjusted prices).
  - The premarket range stops at 9:15 because Alpaca's consolidated feed is only free once
    it is 15 minutes old, and the first decision is at 9:35.
  - Live data is Alpaca's consolidated (SIP) feed for everything older than 16 minutes and
    the free IEX feed for the most recent minutes. Replays use SIP throughout.
  - Replays fill market entries at the open of the minute after the signal bar and limit
    entries at the limit price, and assume the stop came first when a 1-minute bar touches
    both stop and target.

Run:  python intraday.py                  live (both models, stays until the 15:55 flatten)
      python intraday.py --dry-run        live data, logs what it would do, places nothing
      python intraday.py --model ifvg     only one model (ifvg or sweep)
      python intraday.py --scheduled      how GitHub runs it (8:40-10:30 start window, stops about 12:45)
      python intraday.py --flatten        close open intraday trades (GitHub's 15:55 job; starts ~15:00 and waits)
      python intraday.py --replay 2026-11-06             replay one past day
      python intraday.py --replay 2025-11-01 2026-10-31  replay a date range and summarise
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

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest, LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest,
)

import bot
import config
import journal

log = bot.log
ET = "America/New_York"
FIVE = pd.Timedelta(minutes=5)
START_WINDOW = ("08:40", "10:30")          # --scheduled only starts inside this ET window
FLATTEN_START_WINDOW = ("14:45", "15:58")  # --flatten --scheduled only starts inside this one


def et_time(day, hhmm):
    return pd.Timestamp(f"{day} {hhmm}", tz=ET)


def _wait(why):
    return {"status": "wait", "why": why}


def _none(why):
    return {"status": "none", "why": why}


# --------------------------------------------------------------------------- signals

def five_min_bars(m1, day, now=None):
    """Completed 5-minute bars of the regular session from 1-minute bars, with the session
    VWAP as of each bar's close."""
    cols = ["open", "high", "low", "close", "vwap"]
    if m1 is None or m1.empty:
        return pd.DataFrame(columns=cols)
    rth = m1[(m1.index >= et_time(day, "09:30")) & (m1.index < et_time(day, "16:00"))]
    if rth.empty:
        return pd.DataFrame(columns=cols)
    bar_price = rth["vwap"] if "vwap" in rth else (rth["high"] + rth["low"] + rth["close"]) / 3
    rth = rth.assign(svwap=(bar_price * rth["volume"]).cumsum() / rth["volume"].cumsum())
    g = rth.resample("5min")
    b5 = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "vwap": g["svwap"].last(),
    }).dropna(subset=["close"])
    if now is not None:
        b5 = b5[b5.index + FIVE <= now]
    return b5


def _still_time(b5, cutoff, waiting, gave_up):
    """After the last bar seen: keep waiting, or call it a day once the cutoff has passed."""
    if len(b5) and b5.index[-1] + 2 * FIVE > cutoff:
        return _none(gave_up)
    return _wait(waiting)


def structure_bars(m1, day, now=None):
    """Completed 5-minute bars from IFVG_STRUCTURE_FROM (premarket included) to the close."""
    cols = ["open", "high", "low", "close"]
    if m1 is None or m1.empty:
        return pd.DataFrame(columns=cols)
    x = m1[(m1.index >= et_time(day, config.IFVG_STRUCTURE_FROM)) & (m1.index < et_time(day, "16:00"))]
    if x.empty:
        return pd.DataFrame(columns=cols)
    g = x.resample("5min")
    b5 = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                       "close": g["close"].last()}).dropna()
    return b5[b5.index + FIVE <= now] if now is not None else b5


def ifvg_orders(b5, day):
    """Every limit order the inverse fair value gap rules would create that day, in order.
    Each: direction, time (bar completion), expire, limit, stop, plus journal details."""
    o, h, l, c = (b5[k].tolist() for k in ("open", "high", "low", "close"))
    times = list(b5.index)
    first, last = et_time(day, config.IFVG_FIRST_ORDER), et_time(day, config.IFVG_LAST_ORDER)
    life = pd.Timedelta(minutes=config.IFVG_ORDER_MINUTES)
    swing_high = swing_low = None  # [price, already broken]
    flow, gaps, orders = 0, [], []
    for j in range(len(c)):
        done = times[j] + FIVE
        if j >= 4:  # the bar two back is now confirmed as a swing (or not)
            k = j - 2
            if h[k] > max(h[k - 2], h[k - 1]) and h[k] >= max(h[k + 1], h[k + 2]):
                swing_high = [h[k], False]
            if l[k] < min(l[k - 2], l[k - 1]) and l[k] <= min(l[k + 1], l[k + 2]):
                swing_low = [l[k], False]
        broke = 0
        if swing_high and not swing_high[1] and c[j] > swing_high[0]:
            swing_high[1], broke = True, 1
        elif swing_low and not swing_low[1] and c[j] < swing_low[0]:
            swing_low[1], broke = True, -1
        if broke:
            flow = broke
            for order in orders:  # order flow turned against a resting order
                if order["sign"] == -broke:
                    order["expire"] = min(order["expire"], done)
        if j >= 2:
            if l[j] > h[j - 2]:
                gaps.append({"sign": 1, "low": h[j - 2], "high": l[j], "born": j, "live": True})
            elif h[j] < l[j - 2]:
                gaps.append({"sign": -1, "low": h[j], "high": l[j - 2], "born": j, "live": True})
        for gap in gaps:
            if not gap["live"] or gap["born"] >= j:
                continue
            if not (c[j] > gap["high"] if gap["sign"] == -1 else c[j] < gap["low"]):
                continue
            gap["live"] = False  # closed through: it is now an inverse gap for the other side
            sign = -gap["sign"]
            age = j - gap["born"]
            if flow != sign or age > config.IFVG_MAX_GAP_BARS or not (first <= done <= last):
                continue
            long_ = sign == 1
            leg = slice(max(gap["born"] - 2, 0), j + 1)
            extreme = min(l[leg]) if long_ else max(h[leg])
            limit = gap["high"] if long_ else gap["low"]
            through, old, flow_name = ("above", "bearish", "bullish") if long_ else ("below", "bullish", "bearish")
            gap_time = times[gap["born"]] + FIVE
            orders.append({
                "direction": "long" if long_ else "short", "sign": sign, "time": done, "expire": done + life,
                "limit": limit, "price": limit, "stop": extreme - sign * config.INTRADAY_STOP_BUFFER,
                "summary": (f"{flow_name} order flow, 5-min close {c[j]:.2f} {through} the {old} gap "
                            f"{gap['low']:.2f}-{gap['high']:.2f}"),
                "gates": [
                    f"Order flow {flow_name}: the last break of structure was {'up' if long_ else 'down'}",
                    f"{old.capitalize()} fair value gap {gap['low']:.2f}-{gap['high']:.2f} formed at {gap_time:%H:%M}, "
                    f"within {config.IFVG_MAX_GAP_BARS} bars",
                    f"Inverted: 5-min close {c[j]:.2f} {through} it at {done:%H:%M}",
                    f"Setup completed between {config.IFVG_FIRST_ORDER} and {config.IFVG_LAST_ORDER} ET",
                    f"Limit entry at the retest of the gap ({limit:.2f}), good for {config.IFVG_ORDER_MINUTES} minutes",
                ],
                "ctx": {
                    "flow": flow_name, "gap_low": round(gap["low"], 2), "gap_high": round(gap["high"], 2),
                    "gap_time": f"{gap_time:%H:%M}", "gap_age_bars": age, "inversion_close": round(c[j], 2),
                    "limit": round(limit, 2), "leg_extreme": round(extreme, 2),
                },
            })
    return orders


def limit_fill(order, m1):
    """When and where a resting limit order would fill on 1-minute bars: (time, price) or None."""
    sign = order["sign"]
    bars = m1[(m1.index >= order["time"]) & (m1.index < order["expire"])]
    for t, bar in bars.iterrows():
        if (bar["low"] <= order["limit"]) if sign == 1 else (bar["high"] >= order["limit"]):
            gapped = (bar["open"] < order["limit"]) if sign == 1 else (bar["open"] > order["limit"])
            return t, float(bar["open"]) if gapped else float(order["limit"])
    return None


SWEEP_LEVELS = (  # (name, key in the levels dict, direction of the trade after a sweep)
    ("premarket low", "pm_low", "long"), ("prior-day low", "pd_low", "long"),
    ("premarket high", "pm_high", "short"), ("prior-day high", "pd_high", "short"),
)


def sweep_signal(b5, day, daily, levels):
    """Liquidity sweep and reversal. `levels` = premarket and prior-day high/low (None until known)."""
    if levels is None:
        return _wait("waiting for the premarket and prior-day levels")
    if b5.empty:
        return _wait("no bars yet")
    first_entry, cutoff = et_time(day, config.SWEEP_FIRST_ENTRY), et_time(day, config.SWEEP_ENTRY_CUTOFF)
    day_open = float(b5["open"].iloc[0])
    sides = []
    for name, key, direction in SWEEP_LEVELS:
        level = levels.get(key)
        if level is not None and ((level < day_open) if direction == "long" else (level > day_open)):
            sides.append({"direction": direction, "name": name, "level": level, "left": None})
    if not sides:
        return _none("no premarket or prior-day level on either side of the open")
    shown = {k: (round(v, 2) if v is not None else None) for k, v in levels.items()}

    for t, bar in b5.iterrows():
        done = t + FIVE
        if done > cutoff:
            break
        close = float(bar["close"])
        for s in sides:
            long_ = s["direction"] == "long"
            grab = float(bar["low"] if long_ else bar["high"])
            if s["left"] is None:
                if not ((grab < s["level"]) if long_ else (grab > s["level"])):
                    continue
                s.update(left=config.SWEEP_RECLAIM_BARS, extreme=grab, sweep_time=done)
            if s["left"] <= 0:
                continue  # this level's setup was used up or timed out
            s["extreme"] = min(s["extreme"], grab) if long_ else max(s["extreme"], grab)
            s["left"] -= 1
            if not ((close > s["level"]) if long_ else (close < s["level"])):
                continue
            bars_taken, s["left"] = config.SWEEP_RECLAIM_BARS - s["left"], 0
            if done < first_entry:
                continue  # completed before the bot may enter; the other levels stay live
            beyond, back = ("below", "above") if long_ else ("above", "below")
            stop = s["extreme"] - config.INTRADAY_STOP_BUFFER if long_ else s["extreme"] + config.INTRADAY_STOP_BUFFER
            return {"status": "signal", "signal": {
                "direction": s["direction"], "time": done, "price": close, "stop": stop,
                "summary": f"swept the {s['name']} {s['level']:.2f} to {s['extreme']:.2f}, 5-min close {close:.2f} back {back} it",
                "gates": [
                    f"Level in play at the open: {s['name']} {s['level']:.2f}",
                    f"Sweep: a 5-min bar traded {beyond} it at {s['sweep_time']:%H:%M} (extreme {s['extreme']:.2f})",
                    f"Reclaim: 5-min close {close:.2f} back {back} it within {config.SWEEP_RECLAIM_BARS} bars ({done:%H:%M})",
                    f"Entry signal between {config.SWEEP_FIRST_ENTRY} and {config.SWEEP_ENTRY_CUTOFF} ET",
                ],
                "ctx": {
                    "swept_level": s["name"], "swept_price": round(s["level"], 2),
                    "sweep_time": f"{s['sweep_time']:%H:%M}", "sweep_extreme": round(s["extreme"], 2),
                    "reclaim_bars": bars_taken, "reclaim_close": round(close, 2), "levels": shown,
                },
            }}
    return _still_time(b5, cutoff, "no sweep and reclaim yet",
                       f"no level swept and reclaimed between {config.SWEEP_FIRST_ENTRY} and {config.SWEEP_ENTRY_CUTOFF}")


MODELS = {
    "ifvg": {
        "symbol": config.IFVG_SYMBOL, "setup": journal.IFVG_SETUP, "orders": ifvg_orders, "limit": True,
        "target_r": config.IFVG_TARGET_R, "cutoff": config.IFVG_LAST_ORDER,
        "min_stop_pct": config.IFVG_MIN_STOP_PCT, "conditions": journal.IFVG_CONDITIONS, "premarket": True,
    },
    "sweep": {
        "symbol": config.SWEEP_SYMBOL, "setup": journal.SWEEP_SETUP, "find": sweep_signal,
        "target_r": config.SWEEP_TARGET_R, "cutoff": config.SWEEP_ENTRY_CUTOFF,
        "min_stop_pct": config.SWEEP_MIN_STOP_PCT, "conditions": journal.SWEEP_CONDITIONS, "levels": True, "premarket": True,
    },
}
assert len({m["symbol"] for m in MODELS.values()}) == len(MODELS), "each intraday model needs its own symbol"


def plan_trade(signal, entry, target_r, min_stop_pct=0.0):
    """Stop and target for a market entry near `entry`. Returns (plan, None) or (None, reason)."""
    sign = 1 if signal["direction"] == "long" else -1
    risk = (entry - signal["stop"]) * sign
    if risk <= 0:
        return None, f"price {entry:.2f} is already beyond the stop {signal['stop']:.2f}"
    risk = max(risk, entry * min_stop_pct / 100)
    stop = round(entry - sign * risk, 2)
    target = round(entry + sign * target_r * risk, 2)
    risk_ps = round(abs(entry - stop), 2)
    if risk_ps < 0.02:
        return None, f"stop {stop:.2f} is too close to entry {entry:.2f}"
    return {"entry": round(entry, 2), "stop": stop, "target": target, "risk_ps": risk_ps,
            "planned_rr": round(abs(target - entry) / risk_ps, 2)}, None


def size(risk_ps, entry, risk_dollars, value_cap):
    by_risk = math.floor(risk_dollars / risk_ps)
    by_value = math.floor(min(value_cap, config.MAX_POSITION_VALUE) / entry)
    return max(0, min(by_risk, config.MAX_SHARES_PER_TRADE, by_value))


# --------------------------------------------------------------------------- data

def fetch_minutes(clients, symbol, start, end, feed):
    df = clients.data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, feed=feed, adjustment=Adjustment.RAW,
        start=start.tz_convert("UTC").to_pydatetime(), end=end.tz_convert("UTC").to_pydatetime(),
    )).df
    if df.empty:
        return df
    df = df.xs(symbol, level="symbol")
    df.index = df.index.tz_convert(ET)
    return df


def live_minutes(clients, symbol, day, now, start="09:30"):
    """Today's 1-minute bars from `start`: consolidated (SIP) data where it is old enough to be
    free (16+ minutes), the IEX feed for the latest minutes (IEX has no premarket)."""
    start = et_time(day, start)
    split = (now - pd.Timedelta(minutes=16)).floor("min")
    parts = []
    if split > start:
        try:
            parts.append(fetch_minutes(clients, symbol, start, split, DataFeed.SIP))
        except Exception as e:
            log.warning(f"{symbol}: consolidated bars unavailable ({e}); using IEX only.")
            split = start
    parts.append(fetch_minutes(clients, symbol, max(start, split), now, DataFeed.IEX))
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame()
    bars = pd.concat(parts)
    return bars[~bars.index.duplicated(keep="first")].sort_index()


def levels_from(premarket, prior_session):
    """Premarket and prior-day high/low from 1-minute bars (either frame may be empty)."""
    def hi_lo(df):
        return (float(df["high"].max()), float(df["low"].min())) if df is not None and len(df) else (None, None)
    pm_high, pm_low = hi_lo(premarket)
    pd_high, pd_low = hi_lo(prior_session)
    return {"pm_high": pm_high, "pm_low": pm_low, "pd_high": pd_high, "pd_low": pd_low}


def live_levels(clients, symbol, day, daily):
    """Today's premarket range (to SWEEP_PREMARKET_END) and the prior session's range, from the
    consolidated feed. Call it 16+ minutes after SWEEP_PREMARKET_END, when that data is free."""
    def fetch(what, start, end):
        try:
            return fetch_minutes(clients, symbol, start, end, DataFeed.SIP)
        except Exception as e:
            log.warning(f"{symbol}: {what} bars unavailable ({e}); those levels are not used today.")
            return None
    prior_day = daily.index[-1].date().isoformat()
    return levels_from(
        fetch("premarket", et_time(day, "04:00"), et_time(day, config.SWEEP_PREMARKET_END)),
        fetch("prior-day", et_time(prior_day, "09:30"), et_time(prior_day, "16:00")),
    )


def describe_levels(lv):
    part = lambda name, lo, hi: f"{name} {lo:.2f}-{hi:.2f}" if lo is not None else f"{name}: no bars"
    return f"{part('premarket', lv['pm_low'], lv['pm_high'])} | {part('prior day', lv['pd_low'], lv['pd_high'])}"


def flatten_time(clients, day):
    """15:55 ET, or 5 minutes before the close on a half day (13:00 closes, e.g. Nov 27, Dec 24)."""
    normal = et_time(day, config.INTRADAY_FLATTEN_AT)
    try:
        cal = clients.trading.get_calendar(GetCalendarRequest(start=day, end=day))
        close = pd.Timestamp(cal[0].close).tz_localize(ET)  # Alpaca gives naive ET times
        return min(normal, close - pd.Timedelta(minutes=5))
    except Exception:
        log.warning("Intraday: could not read today's close from Alpaca; using the normal flatten time.")
        return normal


def sleep_until(ts):
    while (wait := (ts - bot.now_et()).total_seconds()) > 0:
        time.sleep(min(wait, 30))


def fresh_state():
    return journal.load_state()


def open_intraday(state):
    return {s: t for s, t in state["open"].items() if t.get("model") in MODELS}


def trades_today(state, day, key):
    return state.get("intraday_trades_by_day", {}).get(day, {}).get(key, 0)


# --------------------------------------------------------------------------- live trading

def build_context(signal, plan, daily, headlines, placed_at):
    return {
        "session": journal.session_label(placed_at),
        "direction": signal["direction"],
        "signal_time": f"{signal['time']:%H:%M}", "signal_minute": signal["time"].hour * 60 + signal["time"].minute,
        "placed_at": f"{placed_at:%H:%M} ET",
        "news_count": len(headlines), "headlines": headlines[:3],
        **signal["ctx"],
        **{k: plan[k] for k in ("risk_ps", "planned_rr")},
        "risk_pct": round(plan["risk_ps"] / plan["entry"] * 100, 3),
        **journal.daily_trend(daily, plan["entry"]),
    }


def place(clients, key, signal, daily, dry_run):
    """All gates, then the bracket order: at market, or a resting limit when the signal carries
    a limit price. Returns True if an order was placed."""
    model, client = MODELS[key], clients.trading
    symbol, tag = model["symbol"], f"{model['symbol']} {key}"
    now = bot.now_et()
    day = now.date().isoformat()
    state = fresh_state()
    limit = signal.get("limit")
    price = round(limit, 2) if limit is not None else bot.latest_price(clients, symbol, signal["price"])
    plan, why = plan_trade(signal, price, model["target_r"], model["min_stop_pct"])
    if not plan:
        log.info(f"{tag}: NO TRADE: {why}.")
        return False

    account = client.get_account()
    today_pnl = bot.day_pnl(account)
    risk_dollars = min(config.INTRADAY_RISK_PER_TRADE, config.MAX_DAILY_LOSS + today_pnl)
    in_use = sum(abs(float(p.market_value)) for p in client.get_all_positions())
    value_cap = min(config.INTRADAY_POSITION_VALUE, float(account.equity) - in_use)
    qty = size(plan["risk_ps"], plan["entry"], risk_dollars, value_cap)
    context = build_context(signal, plan, daily, bot.recent_headlines(clients, symbol), now)
    blocked = journal.blocking_rules(context, journal.approved_rules(), model["conditions"])

    reasons = []
    if trades_today(state, day, key) >= config.INTRADAY_MAX_TRADES_PER_MODEL:
        reasons.append("this model already traded today")
    if state["entries_by_day"].get(day, 0) >= config.MAX_TRADES_PER_DAY:
        reasons.append(f"max {config.MAX_TRADES_PER_DAY} trades per day reached")
    if today_pnl <= -config.MAX_DAILY_LOSS:
        reasons.append(f"daily loss limit hit ({today_pnl:+.2f})")
    if symbol in state["open"] or bot.held_qty(client, symbol) > 0:
        reasons.append(f"already holding or tracking {symbol}")
    if signal["direction"] == "short" and not account.shorting_enabled:
        reasons.append("shorting is disabled on the Alpaca account")
    if qty < 1:
        reasons.append(f"size rounds to 0 shares (${risk_dollars:.0f} risk / ${plan['risk_ps']:.2f} stop, "
                       f"${max(value_cap, 0):,.0f} of position value available)")
    if blocked:
        reasons.append(f"blocked by APPROVED lessons.md rule(s) {', '.join(blocked)}")

    how = f"limit {plan['entry']:.2f}" if limit is not None else f"market near {plan['entry']:.2f}"
    log.info(
        f"{tag}: {signal['direction'].upper()} signal ({signal['summary']}): {how}, "
        f"stop {plan['stop']:.2f}, target {plan['target']:.2f} ({plan['planned_rr']:g}R), {qty} sh, "
        f"risk ${qty * plan['risk_ps']:.2f}."
    )
    if reasons:
        log.info(f"{tag}: NO TRADE: {'; '.join(reasons)}.")
        return False

    mark = journal.MET
    gates = [[text, mark] for text in signal["gates"]] + [
        ["One trade per day for this model", mark],
        [f"Hard limit: max {config.MAX_SHARES_PER_TRADE} shares and ${config.MAX_POSITION_VALUE:,.0f} per position "
         f"({qty} sh, ${qty * plan['entry']:,.0f}); no margin", mark],
        [f"Hard limit: daily loss under ${config.MAX_DAILY_LOSS:,.0f} (today {today_pnl:+.2f})", mark],
        [f"Hard limit: max {config.MAX_TRADES_PER_DAY} trades per day", mark],
        ["Hard limit: stop at entry (bracket order)", mark],
        ["No APPROVED lessons.md rule blocks the entry", mark],
    ]
    items = journal.checklist(context, gates, model["conditions"])
    grade, grade_reason = journal.grade(items)
    log.info(f"{tag}: grade {grade} ({grade_reason}).")
    if dry_run:
        log.info(f"{tag}: dry run: no order placed.")
        return False

    request = dict(
        symbol=symbol, qty=qty, side=OrderSide.BUY if signal["direction"] == "long" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=plan["target"]),
        stop_loss=StopLossRequest(stop_price=plan["stop"]),
    )
    order = client.submit_order(
        LimitOrderRequest(limit_price=plan["entry"], **request) if limit is not None else MarketOrderRequest(**request))
    log.info(f"{tag}: submitted {signal['direction'].upper()} bracket id={order.id} status={order.status.value}")

    state = fresh_state()
    state["entries_by_day"][day] = state["entries_by_day"].get(day, 0) + 1
    state.setdefault("intraday_trades_by_day", {}).setdefault(day, {})[key] = trades_today(state, day, key) + 1
    state["open"][symbol] = {
        "symbol": symbol, "direction": signal["direction"], "model": key, "qty": qty,
        "setup": model["setup"], "entry_order_id": str(order.id), "entry": None, "entry_time": None,
        "stop": plan["stop"], "target": plan["target"], "stop_order_id": None, "target_order_id": None,
        "context": context, "checklist": items, "grade": grade, "grade_reason": grade_reason, "adopted": False,
    }
    if limit is not None:
        state["open"][symbol].update(signal_start=signal["time"].isoformat(), expire=signal["expire"].isoformat())
    journal.save_state(state)

    if limit is not None:
        log.info(f"{tag}: limit order resting until {signal['expire']:%H:%M} ET.")
        return True
    order = bot.wait_for_fill(client, order.id)
    if order.status == OrderStatus.FILLED:
        bot.reconcile(clients, fresh_state())  # records the fill price and the stop/target leg ids
    else:
        log.info(f"{tag}: not filled yet (status={order.status.value}); the next check will pick it up.")
    return True


def cancel_resting(clients, key, why):
    """Cancel an unfilled limit entry. Returns "filled" if part of it had filled (that part is
    kept as the day's trade), "cancelled" if nothing had, or None if there was nothing resting."""
    symbol, tag = MODELS[key]["symbol"], f"{MODELS[key]['symbol']} {key}"
    client = clients.trading
    state = fresh_state()
    trade = state["open"].get(symbol)
    if not trade or trade.get("model") != key or trade["entry"] is not None:
        return None
    try:
        client.cancel_order_by_id(trade["entry_order_id"])
    except Exception:
        pass  # already filled or cancelled; the status below says which
    time.sleep(3)
    order = client.get_order_by_id(trade["entry_order_id"])
    filled = float(order.filled_qty or 0)
    if filled > 0:
        trade["qty"] = filled
        trade["entry"] = round(float(order.filled_avg_price), 2)
        trade["entry_time"] = journal.to_et(order.filled_at or bot.now_et()).isoformat()
        trade["stop_order_id"], trade["target_order_id"] = bot.bracket_leg_ids(client, trade["entry_order_id"])
        journal.save_state(state)
        log.info(f"{tag}: entry filled {filled:g} sh at {trade['entry']:.2f} before the cancel ({why}); keeping the trade.")
        return "filled"
    del state["open"][symbol]
    day = bot.now_et().date().isoformat()  # an order that never filled doesn't use up the day's trade
    state["entries_by_day"][day] = max(0, state["entries_by_day"].get(day, 0) - 1)
    by_model = state.setdefault("intraday_trades_by_day", {}).setdefault(day, {})
    by_model[key] = max(0, by_model.get(key, 0) - 1)
    journal.save_state(state)
    log.info(f"{tag}: limit order cancelled unfilled ({why}).")
    return "cancelled"


def flatten_open_trades(clients, reason, only_before=None):
    """Close (or cancel) open intraday trades. `only_before` limits it to trades opened before
    that day (leftovers). Returns how many it handled."""
    state = fresh_state()
    bot.reconcile(clients, state)
    handled = 0
    for symbol, trade in open_intraday(fresh_state()).items():
        if only_before and trade.get("entry_time") and journal.to_et(trade["entry_time"]).date().isoformat() >= only_before:
            continue
        state = fresh_state()
        if trade["entry"] is None:
            clients.trading.cancel_order_by_id(trade["entry_order_id"])
            del state["open"][symbol]
            journal.save_state(state)
            log.info(f"{symbol}: cancelled an unfilled entry order ({reason}).")
        else:
            bot.exit_trade(clients, state, symbol, False, reason=reason)
        handled += 1
    return handled


def manage(clients, stay, flatten_at):
    """Let stops and targets work. Local runs (and half days) stay until the flatten time and
    close what is left; GitHub runs leave that to the separate --flatten job."""
    while True:
        bot.reconcile(clients, fresh_state())
        trades = open_intraday(fresh_state())
        if not trades:
            log.info("Intraday: no open intraday trade. Done for the day.")
            return
        if bot.now_et() >= flatten_at:
            flatten_open_trades(clients, f"End-of-day flatten ({flatten_at:%H:%M})")
            return
        if not stay:
            log.info(f"Intraday: still in {', '.join(trades)}; stops and targets are on Alpaca. "
                     f"The {config.INTRADAY_FLATTEN_AT} job closes what is left.")
            return
        time.sleep(30)


def run_flatten(scheduled):
    """The 15:55 job on GitHub. GitHub often starts scheduled jobs 30-50 minutes late, so the
    job starts from about 15:00 ET and waits for 15:55 itself. Starts outside 14:45-15:58 ET
    (the other daylight-saving season's UTC times) exit at once, and so does any day with no
    open intraday trade, so the wait only costs Actions minutes when there is a trade to close."""
    now = bot.now_et()
    day = now.date().isoformat()
    lo, hi = (et_time(day, t) for t in FLATTEN_START_WINDOW)
    if scheduled and not (lo <= now < hi):
        print(f"{now:%H:%M} ET is outside the {FLATTEN_START_WINDOW[0]}-{FLATTEN_START_WINDOW[1]} flatten start window; skipping.")
        return
    if not open_intraday(fresh_state()):
        print("No open intraday trade; nothing to flatten.")
        return
    bot.setup_logging()
    try:
        log.info("===== Intraday flatten job =====")
        clients = bot.Clients()
        flatten_at = flatten_time(clients, day)
        if bot.now_et() < flatten_at:
            bot.reconcile(clients, fresh_state())  # they may already have hit a stop or target
            if not open_intraday(fresh_state()):
                log.info("Intraday: every trade already closed at its stop or target; nothing to flatten.")
                return
            log.info(f"Intraday: trade(s) still open; waiting until {flatten_at:%H:%M} ET to close.")
            sleep_until(flatten_at)
        if not flatten_open_trades(clients, f"End-of-day flatten ({flatten_at:%H:%M})"):
            log.info("Intraday: the trade(s) had already closed.")
    except Exception:
        log.exception("Intraday: flatten failed")
        sys.exit(1)


def check_model(clients, key, day, daily, dry_run, cache):
    """One look at a model after a 5-minute bar completes. Returns True when it is done for the day."""
    model = MODELS[key]
    symbol, tag = model["symbol"], f"{model['symbol']} {key}"
    now = bot.now_et()
    boundary = now.floor("5min")
    m1 = live_minutes(clients, symbol, day, now)
    for _ in range(3):  # the latest IEX minute can land a few seconds late
        if not m1.empty and m1.index[-1] >= boundary - pd.Timedelta(minutes=1):
            break
        time.sleep(3)
        m1 = live_minutes(clients, symbol, day, bot.now_et())
    levels = None
    if model.get("levels") and now >= et_time(day, config.SWEEP_FIRST_ENTRY):
        if key not in cache:
            cache[key] = live_levels(clients, symbol, day, daily)
            log.info(f"{tag}: {describe_levels(cache[key])}")
        levels = cache[key]
    result = model["find"](five_min_bars(m1, day, now), day, daily, levels)
    if result["status"] == "wait":
        log.info(f"{tag}: {now:%H:%M} {result['why']}.")
        return False
    if result["status"] == "none":
        log.info(f"{tag}: NO TRADE: {result['why']}.")
        return True
    signal = result["signal"]
    if bot.now_et() - signal["time"] > pd.Timedelta(seconds=90):
        log.info(f"{tag}: NO TRADE: the signal ({signal['summary']}) completed at {signal['time']:%H:%M}, "
                 f"before the bot was watching (now {bot.now_et():%H:%M}); too late to act on it.")
        return True
    place(clients, key, signal, daily, dry_run)
    return True


def check_limit_model(clients, key, day, daily, dry_run, cache):
    """One look at a limit-entry model after a 5-minute bar completes: watch a resting order,
    or place one for a setup that just completed. Returns True when it is done for the day."""
    model = MODELS[key]
    symbol, tag = model["symbol"], f"{model['symbol']} {key}"
    now = bot.now_et()
    cutoff = et_time(day, model["cutoff"])
    state = fresh_state()
    trade = state["open"].get(symbol)
    resting = trade is not None and trade.get("model") == key and trade["entry"] is None
    if trade is not None and not resting:
        return True  # filled: the stop and target are working
    if not resting and trades_today(state, day, key) >= config.INTRADAY_MAX_TRADES_PER_MODEL:
        return True
    if not resting and now > cutoff + pd.Timedelta(seconds=90):
        log.info(f"{tag}: NO TRADE: no setup filled by {model['cutoff']} ET.")
        return True

    m1 = live_minutes(clients, symbol, day, now, start=config.IFVG_STRUCTURE_FROM)
    orders = model["orders"](structure_bars(m1, day, now), day)
    if resting:
        placed_until = pd.Timestamp(trade["expire"])
        expire = min([placed_until] + [o["expire"] for o in orders if o["time"].isoformat() == trade.get("signal_start")])
        if now < expire:
            return False
        why = "order flow turned the other way" if expire < placed_until else f"not filled in {config.IFVG_ORDER_MINUTES} minutes"
        if cancel_resting(clients, key, why) == "filled":
            return True
        return bot.now_et() > cutoff
    fresh = [o for o in orders if pd.Timedelta(0) <= now - o["time"] <= pd.Timedelta(seconds=90)]
    if fresh:
        place(clients, key, fresh[-1], daily, dry_run)
    return False


def decision_end(day, key):
    """When a model has nothing left to decide: its cutoff, plus the life of a resting order."""
    end = et_time(day, MODELS[key]["cutoff"])
    return end + pd.Timedelta(minutes=config.IFVG_ORDER_MINUTES) if MODELS[key].get("limit") else end


def run_live(args):
    clients = bot.Clients()
    day = bot.now_et().date().isoformat()
    keys = [args.model] if args.model else list(MODELS)
    names = ", ".join(f"{MODELS[k]['symbol']} {k}" for k in keys)
    log.info(f"===== Intraday models started ({names}{', dry run' if args.dry_run else ''}) =====")
    state = fresh_state()
    journal.ensure_lessons_file()
    journal.session_briefing(state)
    clock = clients.trading.get_clock()
    if bot.market_closed_today(clients.trading, clock):
        log.info("Intraday: market is closed today. Nothing to do.")
        return
    flatten_at = flatten_time(clients, day)
    stay = not args.scheduled or flatten_at < et_time(day, config.INTRADAY_FLATTEN_AT)  # half days: stay and flatten
    last_decision = max(decision_end(day, k) for k in keys)

    if bot.now_et() < last_decision:
        sleep_until(et_time(day, "09:35:05"))  # the first 5-minute bar completes at 9:35
    bot.reconcile(clients, fresh_state())
    if flatten_open_trades(clients, "Flatten of a previous day's trade (missed the end-of-day close)", only_before=day):
        log.info("Intraday: closed a trade left open from a previous day.")

    state = fresh_state()
    pending = []
    for key in keys:
        symbol = MODELS[key]["symbol"]
        trade = state["open"].get(symbol)
        if trade and trade.get("model") == key and trade["entry"] is None:
            log.info(f"{symbol} {key}: an entry order is already resting; watching it.")
            pending.append(key)
        elif trades_today(state, day, key) >= config.INTRADAY_MAX_TRADES_PER_MODEL:
            log.info(f"{symbol} {key}: already traded today.")
        elif trade:
            log.info(f"{symbol} {key}: {symbol} is already in a tracked trade.")
        elif bot.now_et() >= et_time(day, MODELS[key]["cutoff"]):
            log.info(f"{symbol} {key}: started after its {MODELS[key]['cutoff']} ET cutoff. Nothing to do today.")
        else:
            pending.append(key)

    daily = {key: bot.fetch_bars(MODELS[key]["symbol"], config.LOOKBACK_DAYS, clients) for key in pending}
    cache = {}
    while pending:
        for key in list(pending):
            check = check_limit_model if MODELS[key].get("limit") else check_model
            try:
                if check(clients, key, day, daily[key], args.dry_run, cache):
                    pending.remove(key)
            except Exception:
                log.exception(f"{MODELS[key]['symbol']} {key}: check failed; trying again on the next bar")
        if not pending or bot.now_et() >= min(last_decision + pd.Timedelta(minutes=2), flatten_at):
            break
        sleep_until(bot.now_et().floor("5min") + FIVE + pd.Timedelta(seconds=5))
        bot.reconcile(clients, fresh_state())  # journal an entry, stop or target that filled meanwhile
    for key in pending:  # never leave an entry order resting with nobody watching it
        if MODELS[key].get("limit"):
            cancel_resting(clients, key, "end of the entry window")
    manage(clients, stay, flatten_at)


# --------------------------------------------------------------------------- replay

def replay_day(key, day, m1, daily, verbose, prev_m1=None):
    """Simulate one past day for one model. Returns a result dict."""
    model = MODELS[key]
    say = (lambda m: print(f"  {model['symbol']} {key}: {m}")) if verbose else (lambda m: None)
    prior = daily[daily.index.date < pd.Timestamp(day).date()]
    if len(prior) < config.ATR_PERIOD + 1:
        return {"day": day, "result": "no data", "why": "not enough daily history"}
    flatten_at = et_time(day, config.INTRADAY_FLATTEN_AT)
    is_limit = bool(model.get("limit"))
    if is_limit:
        signal, busy_until = None, None
        for order in model["orders"](structure_bars(m1, day), day):
            if busy_until is not None and order["time"] < busy_until:
                continue  # one resting order at a time
            got = limit_fill(order, m1)
            if got:
                signal, (start, entry) = order, got
                break
            busy_until = order["expire"]
            say(f"{order['time']:%H:%M} {order['direction'].upper()} limit {order['limit']:.2f} ({order['summary']}): "
                f"cancelled unfilled at {order['expire']:%H:%M}")
        if signal is None:
            say("NO TRADE: no setup filled")
            return {"day": day, "result": "no trade", "why": "no setup filled"}
        after = m1[(m1.index >= start) & (m1.index < flatten_at)]
    else:
        levels = None
        if model.get("levels"):
            prev_session = prev_m1[prev_m1.index.hour * 60 + prev_m1.index.minute >= 9 * 60 + 30] if prev_m1 is not None else None
            levels = levels_from(m1[m1.index < et_time(day, config.SWEEP_PREMARKET_END)], prev_session)
        result = model["find"](five_min_bars(m1, day), day, prior, levels)
        if result["status"] != "signal":
            say(f"NO TRADE: {result['why']}")
            return {"day": day, "result": "no trade", "why": result["why"]}
        signal = result["signal"]
        after = m1[(m1.index >= signal["time"]) & (m1.index < flatten_at)]
        if after.empty:
            return {"day": day, "result": "no data", "why": "no bars after the signal"}
        entry = float(after["open"].iloc[0])
    plan, why = plan_trade(signal, entry, model["target_r"], model["min_stop_pct"])
    if not plan:
        say(f"NO TRADE: {why}")
        return {"day": day, "result": "no trade", "why": why}
    long_ = signal["direction"] == "long"
    qty = size(plan["risk_ps"], entry, config.INTRADAY_RISK_PER_TRADE, config.INTRADAY_POSITION_VALUE)
    say(f"{signal['time']:%H:%M} {signal['direction'].upper()} ({signal['summary']}): "
        f"{'filled ' + format(after.index[0], '%H:%M') + ' at' if is_limit else 'entry'} {entry:.2f}, "
        f"stop {plan['stop']:.2f}, target {plan['target']:.2f}, {qty} sh")
    exit_price = None
    for i, (t, bar) in enumerate(after.iterrows()):
        if (bar["low"] <= plan["stop"]) if long_ else (bar["high"] >= plan["stop"]):
            exit_price, reason, exit_t = plan["stop"], "stop", t  # stop first if both in one bar
            break
        if is_limit and i == 0:
            continue  # the bar a limit order fills in can stop out but is not credited with the target
        if (bar["high"] >= plan["target"]) if long_ else (bar["low"] <= plan["target"]):
            exit_price, reason, exit_t = plan["target"], "target", t
            break
    if exit_price is None:
        exit_price, reason, exit_t = float(after["close"].iloc[-1]), "flatten", after.index[-1]
    points = (exit_price - entry) if long_ else (entry - exit_price)
    r = points / plan["risk_ps"]
    say(f"{exit_t:%H:%M} exit {exit_price:.2f} ({reason}) = {r:+.2f}R, ${points * qty:+.2f}")
    return {"day": day, "result": "trade", "direction": signal["direction"], "r": r, "exit": reason,
            "pnl": points * qty, "qty": qty, **plan}


def history_minutes(clients, symbol, start, end, premarket=False):
    """1-minute SIP bars for a date range, fetched in chunks: the regular session, plus the
    premarket from 4:00 (and a few earlier days, for the prior session) when `premarket` is set."""
    first_minute = 4 * 60 if premarket else 9 * 60 + 30
    parts, cursor = [], pd.Timestamp(start, tz=ET) - pd.Timedelta(days=6 if premarket else 0)
    stop = pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)
    limit = bot.now_et() - pd.Timedelta(minutes=16)
    while cursor < stop:
        chunk_end = min(cursor + pd.Timedelta(days=21), stop, limit)
        if chunk_end <= cursor:
            break
        df = fetch_minutes(clients, symbol, cursor, chunk_end, DataFeed.SIP)
        if not df.empty:
            minutes = df.index.hour * 60 + df.index.minute
            parts.append(df[(minutes >= first_minute) & (minutes < 16 * 60)])
        cursor = chunk_end
    return pd.concat(parts) if parts else pd.DataFrame()


def summarise(key, results, start, end):
    model = MODELS[key]
    counts = pd.Series([r["result"] for r in results]).value_counts().to_dict()
    print(f"\n{model['symbol']} {model['setup']}, {start} to {end}: {len(results)} trading days -> {counts}")
    trades = [r for r in results if r["result"] == "trade"]
    if not trades:
        return
    df = pd.DataFrame(trades)
    wins, losses = df[df.r > 0], df[df.r <= 0]
    avg_win = wins.r.mean() if len(wins) else 0.0
    avg_loss = -losses.r.mean() if len(losses) else 0.0
    equity = df.pnl.cumsum()
    drawdown = (equity.cummax().clip(lower=0) - equity).max()
    streak = longest = 0
    for r in df.r:
        streak = streak + 1 if r <= 0 else 0
        longest = max(longest, streak)
    print(f"Trades {len(df)} | win rate {len(wins) / len(df):.0%} | avg win {avg_win:+.2f}R | avg loss {-avg_loss:+.2f}R | "
          f"realized R:R {'1:%.2f' % (avg_win / avg_loss) if avg_loss else 'n/a'} | expectancy {df.r.mean():+.3f}R | total {df.r.sum():+.2f}R")
    for d, g in df.groupby("direction"):
        print(f"  {d:5}: {len(g)} trades, win rate {(g.r > 0).mean():.0%}, avg {g.r.mean():+.2f}R, total {g.r.sum():+.2f}R")
    print("  exits:", df["exit"].value_counts().to_dict())
    print(f"  at the configured size (avg {df.qty.mean():.0f} sh, avg risk ${(df.qty * df.risk_ps).mean():.0f}): "
          f"P&L ${df.pnl.sum():+,.0f}, worst day ${df.pnl.min():+,.0f}, max drawdown ${drawdown:,.0f}, "
          f"longest losing streak {longest}")
    print("  (no commissions or slippage; stop assumed first when a bar touches both)")


def run_replay(start, end, keys):
    clients = bot.Clients()
    days = [d.date().isoformat() for d in pd.bdate_range(start, end)]
    verbose = len(days) <= 5
    for key in keys:
        symbol = MODELS[key]["symbol"]
        daily = bot.alpaca_daily_bars(clients, symbol, (pd.Timestamp(start) - pd.Timedelta(days=60)).to_pydatetime())
        minutes = history_minutes(clients, symbol, start, end, premarket=bool(MODELS[key].get("premarket")))
        by_day = {d.isoformat(): g for d, g in minutes.groupby(minutes.index.date)} if not minutes.empty else {}
        sessions = sorted(by_day)
        results = []
        for i, day in enumerate(sessions):
            if day not in days:
                continue
            if verbose:
                print(f"{day}:")
            prev_m1 = by_day[sessions[i - 1]] if i else None
            results.append(replay_day(key, day, by_day[day], daily, verbose, prev_m1))
        if not verbose:
            summarise(key, results, start, end)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="live data, no orders")
    parser.add_argument("--model", choices=list(MODELS), help="only this model (default: both)")
    parser.add_argument("--replay", nargs="+", metavar="DATE", help="replay one day, or a start and end date")
    parser.add_argument("--scheduled", action="store_true",
                        help=f"GitHub run: start only between {START_WINDOW[0]} and {START_WINDOW[1]} ET, once a day; "
                             "stop after the last entry cutoff")
    parser.add_argument("--flatten", action="store_true", help="close open intraday trades (the 15:55 job)")
    args = parser.parse_args()
    if args.replay:
        run_replay(args.replay[0], args.replay[-1], [args.model] if args.model else list(MODELS))
        return
    if args.flatten:
        run_flatten(args.scheduled)
        return
    if args.scheduled:
        # GitHub's UTC schedule ignores daylight saving and can start an hour late; only the
        # first start inside the window runs, and only once per day.
        now, state = bot.now_et(), fresh_state()
        day = now.date().isoformat()
        lo, hi = (et_time(day, t) for t in START_WINDOW)
        if not (lo <= now < hi):
            print(f"{now:%H:%M} ET is outside the {START_WINDOW[0]}-{START_WINDOW[1]} start window; skipping.")
            return
        if state.get("intraday_last_start") == day:
            print(f"Already started today ({day}); skipping.")
            return
        state["intraday_last_start"] = day
        journal.save_state(state)
    bot.setup_logging()
    try:
        run_live(args)
    except Exception:
        log.exception("Intraday: run failed")
        sys.exit(1)
    finally:
        log.info("===== Intraday models finished =====")


if __name__ == "__main__":
    main()
