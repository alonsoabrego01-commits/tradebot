"""Intraday models. Both act on completed 5-minute bars, take at most one trade a day each,
enter at market with a bracket (stop + target), and are flat by 15:55 ET.

QQQ opening range breakout (config.ORB_*):
  1. Opening range = high and low of 9:30-9:45 ET.
  2. The first 5-minute close outside the range, completed by 10:30, is the signal:
     close above the high -> long, close below the low -> short.
  3. Stop $0.05 beyond the midpoint of the range (or its far side, ORB_STOP). Target 2R.
  4. If the first breakout is not taken (filter, hard limit), there is no second try that day.

SPY trend-day VWAP pullback (config.VWAP_*):
  1. Strong open, judged at 10:00 ET: price moved at least 0.25 daily ATR from the 9:30 open,
     the 10:00 close is on that side of the session VWAP, and at least 80% of the 5-minute
     closes so far are too. Up = longs only, down = shorts only, neither = no trade.
  2. First pullback: a 5-minute bar trades back to the nearer of VWAP and the 21 EMA after the
     previous bar closed beyond it. A 5-minute close through VWAP means the trend broke: no trade.
  3. Entry when a 5-minute bar then closes back beyond the 9 EMA in the trend direction
     (it can be the same bar), completed by 11:00.
  4. Stop $0.05 behind the pullback's extreme (at least 0.05% of price away). Target 2R.

Size = INTRADAY_RISK_PER_TRADE / stop distance, inside INTRADAY_POSITION_VALUE and the hard
limits, and never more than the account's unused equity (no margin). Anything unclear
(missing bars, a signal that completed before the bot was watching, a limit in the way,
shorting disabled) means no trade, and the reason is logged.

Definitions used where the rules leave room:
  - VWAP is the session VWAP from 9:30. The 9 and 21 EMAs are of 5-minute closes from 9:30.
  - Live data is Alpaca's consolidated (SIP) feed for everything older than 16 minutes and
    the free IEX feed for the most recent minutes. Replays use SIP throughout.
  - Replays fill at the open of the minute after the signal bar and assume the stop came
    first when a 1-minute bar touches both stop and target.

Run:  python intraday.py                  live (both models, stays until the 15:55 flatten)
      python intraday.py --dry-run        live data, logs what it would do, places nothing
      python intraday.py --model orb      only one model (orb or vwap)
      python intraday.py --scheduled      how GitHub runs it (8:40-10:30 start window, stops after the last cutoff)
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
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest

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
    VWAP and the fast/slow EMAs of the closes as of each bar's close."""
    cols = ["open", "high", "low", "close", "vwap", "ema_fast", "ema_slow"]
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
    b5["ema_fast"] = b5["close"].ewm(span=config.VWAP_FAST_EMA, adjust=False).mean()
    b5["ema_slow"] = b5["close"].ewm(span=config.VWAP_SLOW_EMA, adjust=False).mean()
    return b5


def _still_time(b5, cutoff, waiting, gave_up):
    """After the last bar seen: keep waiting, or call it a day once the cutoff has passed."""
    if len(b5) and b5.index[-1] + 2 * FIVE > cutoff:
        return _none(gave_up)
    return _wait(waiting)


def orb_signal(b5, day, daily):
    """Opening range breakout. `daily` = completed daily bars before `day`."""
    range_end = et_time(day, "09:30") + pd.Timedelta(minutes=config.ORB_RANGE_MINUTES)
    cutoff = et_time(day, config.ORB_ENTRY_CUTOFF)
    need = config.ORB_RANGE_MINUTES // 5
    opening, later = b5[b5.index < range_end], b5[b5.index >= range_end]
    if len(opening) < need:
        if later.empty:
            return _wait("opening range still forming")
        return _none(f"only {len(opening)} of {need} opening-range bars, so the range is unclear")
    high, low = float(opening["high"].max()), float(opening["low"].min())
    width = high - low
    if width <= 0:
        return _none("opening range has no width")

    allowed = {"long", "short"}
    trend_note = ""
    if config.ORB_TREND_FILTER:
        ma = float(daily["Close"].tail(config.ORB_TREND_MA).mean())
        prior_close = float(daily["Close"].iloc[-1])
        allowed = {"long"} if prior_close > ma else {"short"}
        trend_note = f"prior close {prior_close:.2f} vs {config.ORB_TREND_MA}-day MA {ma:.2f}"

    for t, bar in later.iterrows():
        done = t + FIVE
        if done > cutoff:
            break
        close = float(bar["close"])
        direction = "long" if close > high else "short" if close < low else None
        if direction is None:
            continue
        if direction not in allowed:
            return _none(f"first breakout ({done:%H:%M}) was {direction}, against the daily trend ({trend_note})")
        long_ = direction == "long"
        anchor = (high + low) / 2 if config.ORB_STOP == "mid" else (low if long_ else high)
        stop = anchor - config.INTRADAY_STOP_BUFFER if long_ else anchor + config.INTRADAY_STOP_BUFFER
        stop_basis = "the range midpoint" if config.ORB_STOP == "mid" else f"the range {'low' if long_ else 'high'}"
        side = "above" if long_ else "below"
        gates = [
            f"Opening range {config.ORB_RANGE_MINUTES} min from 9:30: {low:.2f}-{high:.2f}",
            f"First 5-min close outside the range: {close:.2f} {side} it at {done:%H:%M}",
            f"Breakout completed by {config.ORB_ENTRY_CUTOFF} ET",
        ]
        if config.ORB_TREND_FILTER:
            gates.append(f"Breakout agrees with the daily trend ({trend_note})")
        return {"status": "signal", "signal": {
            "direction": direction, "time": done, "price": close, "stop": stop, "gates": gates,
            "summary": f"5-min close {close:.2f} {side} the opening range {low:.2f}-{high:.2f}",
            "ctx": {
                "range_high": round(high, 2), "range_low": round(low, 2),
                "range_pct": round(width / close * 100, 3), "breakout_close": round(close, 2),
                "stop_basis": f"${config.INTRADAY_STOP_BUFFER:.2f} beyond {stop_basis}",
            },
        }}
    return _still_time(b5, cutoff, f"no close outside {low:.2f}-{high:.2f} yet",
                       f"no 5-min close outside the opening range {low:.2f}-{high:.2f} by {config.ORB_ENTRY_CUTOFF}")


def vwap_signal(b5, day, daily):
    """Trend-day VWAP pullback. `daily` = completed daily bars before `day`."""
    open_end = et_time(day, "09:30") + pd.Timedelta(minutes=config.VWAP_OPEN_MINUTES)
    cutoff = et_time(day, config.VWAP_ENTRY_CUTOFF)
    need = config.VWAP_OPEN_MINUTES // 5
    first, later = b5[b5.index < open_end], b5[b5.index >= open_end]
    if len(first) < need:
        if later.empty:
            return _wait(f"waiting for {open_end:%H:%M} to judge the open")
        return _none(f"only {len(first)} of {need} bars in the first {config.VWAP_OPEN_MINUTES} minutes")

    atr = journal.atr(daily, config.ATR_PERIOD)
    day_open, last = float(first["open"].iloc[0]), first.iloc[-1]
    move = float(last["close"]) - day_open
    min_move = config.VWAP_MIN_OPEN_MOVE_ATR * atr
    min_side = math.ceil(config.VWAP_MIN_SIDE_SHARE * len(first))
    above = int((first["close"] > first["vwap"]).sum())
    below = int((first["close"] < first["vwap"]).sum())
    if move >= min_move and last["close"] > last["vwap"] and above >= min_side:
        direction, sign, on_side = "long", 1, above
    elif move <= -min_move and last["close"] < last["vwap"] and below >= min_side:
        direction, sign, on_side = "short", -1, below
    else:
        return _none(
            f"no strong open: {move:+.2f} from the open by {open_end:%H:%M} ({move / atr:+.2f} ATR, need "
            f"{config.VWAP_MIN_OPEN_MOVE_ATR:g}), {above} of {len(first)} closes above VWAP and {below} below "
            f"(need {min_side} on the trend side)")
    long_ = direction == "long"
    beyond, back = ("above", "below") if long_ else ("below", "above")

    prev_close, touch, extreme, pierced = float(last["close"]), None, None, False
    for t, bar in later.iterrows():
        done = t + FIVE
        if done > cutoff:
            break
        close, vwap = float(bar["close"]), float(bar["vwap"])
        if (close - vwap) * sign < 0:
            return _none(f"{done:%H:%M} 5-min close {close:.2f} back {back} VWAP {vwap:.2f}; the trend broke")
        zone = max(vwap, float(bar["ema_slow"])) if long_ else min(vwap, float(bar["ema_slow"]))
        grab = float(bar["low"] if long_ else bar["high"])
        if touch is None and (prev_close - zone) * sign > 0 and (grab - zone) * sign <= 0:
            touch = {"level": "VWAP" if zone == vwap else f"{config.VWAP_SLOW_EMA} EMA", "price": zone, "time": done}
            extreme = grab
        if touch is not None:
            extreme = min(extreme, grab) if long_ else max(extreme, grab)
            pierced = pierced or (grab - vwap) * sign < 0
            if (close - float(bar["ema_fast"])) * sign > 0 and (close - float(bar["open"])) * sign > 0:
                stop = extreme - sign * config.INTRADAY_STOP_BUFFER
                return {"status": "signal", "signal": {
                    "direction": direction, "time": done, "price": close, "stop": stop,
                    "summary": (f"{direction} trend open ({move / atr:+.2f} ATR), pullback to the {touch['level']} "
                                f"{touch['price']:.2f}, 5-min close {close:.2f} back {beyond} the {config.VWAP_FAST_EMA} EMA"),
                    "gates": [
                        f"Strong open by {open_end:%H:%M}: {move:+.2f} from the open ({move / atr:+.2f} daily ATR), "
                        f"{on_side} of {len(first)} 5-min closes {beyond} VWAP",
                        f"First pullback to the {touch['level']} ({touch['price']:.2f}) at {touch['time']:%H:%M}",
                        f"No 5-min close {back} VWAP before entry",
                        f"5-min close {close:.2f} back {beyond} the {config.VWAP_FAST_EMA} EMA at {done:%H:%M}",
                        f"Entry signal completed by {config.VWAP_ENTRY_CUTOFF} ET",
                    ],
                    "ctx": {
                        "open_move": round(move, 2), "open_move_atr": round(abs(move) / atr, 2),
                        "side_closes": f"{on_side}/{len(first)}", "vwap": round(vwap, 2),
                        "touch_level": touch["level"], "touch_price": round(touch["price"], 2),
                        "touch_time": f"{touch['time']:%H:%M}", "pullback_extreme": round(extreme, 2),
                        "pierced_vwap": bool(pierced),
                    },
                }}
        prev_close = close
    waiting = "in the pullback, waiting for the reclaim" if touch else f"{direction} trend open, waiting for the first pullback"
    return _still_time(b5, cutoff, waiting, f"{direction} trend open but no pullback entry by {config.VWAP_ENTRY_CUTOFF}")


MODELS = {
    "orb": {
        "symbol": config.ORB_SYMBOL, "setup": journal.ORB_SETUP, "find": orb_signal,
        "target_r": config.ORB_TARGET_R, "cutoff": config.ORB_ENTRY_CUTOFF, "min_stop_pct": 0.0,
        "conditions": journal.ORB_CONDITIONS,
    },
    "vwap": {
        "symbol": config.VWAP_SYMBOL, "setup": journal.VWAP_SETUP, "find": vwap_signal,
        "target_r": config.VWAP_TARGET_R, "cutoff": config.VWAP_ENTRY_CUTOFF,
        "min_stop_pct": config.VWAP_MIN_STOP_PCT, "conditions": journal.VWAP_CONDITIONS,
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


def live_minutes(clients, symbol, day, now):
    """Today's 1-minute bars: consolidated (SIP) data where it is old enough to be free
    (16+ minutes), the IEX feed for the latest minutes."""
    start = et_time(day, "09:30")
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
    width = signal["ctx"].get("range_high", 0) - signal["ctx"].get("range_low", 0)
    edge = signal["ctx"].get("range_high") if signal["direction"] == "long" else signal["ctx"].get("range_low")
    extra = {"extension": round(abs(plan["entry"] - edge) / width, 2)} if width > 0 else {}
    return {
        "session": journal.session_label(placed_at),
        "direction": signal["direction"],
        "signal_time": f"{signal['time']:%H:%M}", "signal_minute": signal["time"].hour * 60 + signal["time"].minute,
        "placed_at": f"{placed_at:%H:%M} ET",
        "news_count": len(headlines), "headlines": headlines[:3],
        **signal["ctx"], **extra,
        **{k: plan[k] for k in ("risk_ps", "planned_rr")},
        "risk_pct": round(plan["risk_ps"] / plan["entry"] * 100, 3),
        **journal.daily_trend(daily, plan["entry"]),
    }


def place(clients, key, signal, daily, dry_run):
    """All gates, then the market bracket order. Returns True if an order was placed."""
    model, client = MODELS[key], clients.trading
    symbol, tag = model["symbol"], f"{model['symbol']} {key}"
    now = bot.now_et()
    day = now.date().isoformat()
    state = fresh_state()
    price = bot.latest_price(clients, symbol, signal["price"])
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

    log.info(
        f"{tag}: {signal['direction'].upper()} signal ({signal['summary']}): market near {plan['entry']:.2f}, "
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

    order = client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY if signal["direction"] == "long" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC, order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=plan["target"]),
        stop_loss=StopLossRequest(stop_price=plan["stop"]),
    ))
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
    journal.save_state(state)

    order = bot.wait_for_fill(client, order.id)
    if order.status == OrderStatus.FILLED:
        bot.reconcile(clients, fresh_state())  # records the fill price and the stop/target leg ids
    else:
        log.info(f"{tag}: not filled yet (status={order.status.value}); the next check will pick it up.")
    return True


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


def check_model(clients, key, day, daily, dry_run):
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
    result = model["find"](five_min_bars(m1, day, now), day, daily)
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
    last_cutoff = max(et_time(day, MODELS[k]["cutoff"]) for k in keys)

    if bot.now_et() < last_cutoff:
        sleep_until(et_time(day, "09:35:05"))  # the first 5-minute bar completes at 9:35
    bot.reconcile(clients, fresh_state())
    if flatten_open_trades(clients, "Flatten of a previous day's trade (missed the end-of-day close)", only_before=day):
        log.info("Intraday: closed a trade left open from a previous day.")

    state = fresh_state()
    pending = []
    for key in keys:
        symbol = MODELS[key]["symbol"]
        if trades_today(state, day, key) >= config.INTRADAY_MAX_TRADES_PER_MODEL:
            log.info(f"{symbol} {key}: already traded today.")
        elif symbol in state["open"]:
            log.info(f"{symbol} {key}: {symbol} is already in a tracked trade.")
        elif bot.now_et() >= et_time(day, MODELS[key]["cutoff"]):
            log.info(f"{symbol} {key}: started after its {MODELS[key]['cutoff']} ET cutoff. Nothing to do today.")
        else:
            pending.append(key)

    daily = {key: bot.fetch_bars(MODELS[key]["symbol"], config.LOOKBACK_DAYS, clients) for key in pending}
    while pending:
        for key in list(pending):
            try:
                if check_model(clients, key, day, daily[key], args.dry_run):
                    pending.remove(key)
            except Exception:
                log.exception(f"{MODELS[key]['symbol']} {key}: check failed; trying again on the next bar")
        if not pending or bot.now_et() >= last_cutoff:
            break
        sleep_until(bot.now_et().floor("5min") + FIVE + pd.Timedelta(seconds=5))
        bot.reconcile(clients, fresh_state())  # journal a stop or target that filled meanwhile
    manage(clients, stay, flatten_at)


# --------------------------------------------------------------------------- replay

def replay_day(key, day, m1, daily, verbose):
    """Simulate one past day for one model. Returns a result dict."""
    model = MODELS[key]
    say = (lambda m: print(f"  {model['symbol']} {key}: {m}")) if verbose else (lambda m: None)
    prior = daily[daily.index.date < pd.Timestamp(day).date()]
    if len(prior) < max(config.ATR_PERIOD + 1, config.ORB_TREND_MA):
        return {"day": day, "result": "no data", "why": "not enough daily history"}
    result = model["find"](five_min_bars(m1, day), day, prior)
    if result["status"] != "signal":
        say(f"NO TRADE: {result['why']}")
        return {"day": day, "result": "no trade", "why": result["why"]}
    signal = result["signal"]
    flatten_at = et_time(day, config.INTRADAY_FLATTEN_AT)
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
    say(f"{signal['time']:%H:%M} {signal['direction'].upper()} ({signal['summary']}): entry {entry:.2f}, "
        f"stop {plan['stop']:.2f}, target {plan['target']:.2f}, {qty} sh")
    exit_price = None
    for t, bar in after.iterrows():
        if (bar["low"] <= plan["stop"]) if long_ else (bar["high"] >= plan["stop"]):
            exit_price, reason, exit_t = plan["stop"], "stop", t  # stop first if both in one bar
            break
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


def history_minutes(clients, symbol, start, end):
    """Regular-session 1-minute SIP bars for a date range, fetched a month at a time."""
    parts, cursor = [], pd.Timestamp(start, tz=ET)
    stop = pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)
    limit = bot.now_et() - pd.Timedelta(minutes=16)
    while cursor < stop:
        chunk_end = min(cursor + pd.Timedelta(days=31), stop, limit)
        if chunk_end <= cursor:
            break
        df = fetch_minutes(clients, symbol, cursor, chunk_end, DataFeed.SIP)
        if not df.empty:
            minutes = df.index.hour * 60 + df.index.minute
            parts.append(df[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)])
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
        minutes = history_minutes(clients, symbol, start, end)
        by_day = {d.isoformat(): g for d, g in minutes.groupby(minutes.index.date)} if not minutes.empty else {}
        results = []
        for day in days:
            if day not in by_day:
                continue
            if verbose:
                print(f"{day}:")
            results.append(replay_day(key, day, by_day[day], daily, verbose))
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
