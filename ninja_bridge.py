"""Mirror the bot's Alpaca intraday trades to a NinjaTrader account (e.g. Apex) without a middleman.

The bot keeps running on GitHub and trading QQQ/SPY on Alpaca. This program runs on the computer
where NinjaTrader 8 is open and logged in, and copies each Alpaca intraday trade to micro futures
through NinjaTrader's Automated Trading Interface (ATI): it writes order instruction files
(oif*.txt) into Documents\\NinjaTrader 8\\incoming and reads the order state files NinjaTrader
writes to Documents\\NinjaTrader 8\\outgoing.

Every POLL_SECONDS between START and the flatten time it does, per symbol in config.FUTURES:
  1. When the bot's Alpaca trade gets its first fill: size the futures trade (same rules as the
     TradersPost mirror: webhook.contracts) and buy/sell at market.
  2. When that market order fills: one stop and one take-profit per lot, OCO-linked, at the same
     percent distances from the futures fill as the Alpaca stop and steps are from the Alpaca fill.
  3. When the bot moves its Alpaca stop (scale-out ratchet): move the futures stops the same way,
     so the futures side gets the real trailing stop.
  4. When the Alpaca position is gone (stop, target, trailing stop, closed by hand): close the
     futures position (CLOSEPOSITION also cancels its working orders).
  5. At the flatten time (15:55 ET, or 5 minutes before an early close): close everything it opened.

Run:  python ninja_bridge.py            (today's session, account from config.NINJA_ACCOUNT)
      python ninja_bridge.py --account Sim101 --once   (one pass, for testing)
Only one futures trade per symbol per day, like the bot. State: ninja_state.json (not committed).
Turn on ATI first: NinjaTrader > Tools > Options (Settings) > Automated trading interface > AT Interface.
"""
import argparse
import glob
import itertools
import json
import os
import sys
import time

import pandas as pd
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

import bot
import config
import intraday
import webhook

log = bot.log
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "ninja_state.json")
POLL_SECONDS = 3
START = "09:25"
_ids = itertools.count(int(time.time() * 1000) % 10**9)


# --------------------------------------------------------------------------- NinjaTrader files

def ninja_dir():
    for base in (os.path.join(os.path.expanduser("~"), "OneDrive", "Documents"),
                 os.path.join(os.path.expanduser("~"), "Documents")):
        path = os.path.join(base, "NinjaTrader 8")
        if os.path.isdir(os.path.join(path, "incoming")):
            return path
    raise RuntimeError("NinjaTrader 8 folder with an 'incoming' folder not found; is NinjaTrader installed?")


def ninja_instrument(root, day):
    """Front quarterly contract in NinjaTrader's naming ('MNQ 12-26'), rolling 8 days before expiry
    (CME's roll date for equity index futures is the Thursday 8 days before the third Friday)."""
    d = pd.Timestamp(day)
    for year in (d.year, d.year + 1):
        for month in (3, 6, 9, 12):
            first = pd.Timestamp(year=year, month=month, day=1)
            third_friday = first + pd.Timedelta(days=(4 - first.weekday()) % 7 + 14)
            if d < third_friday - pd.Timedelta(days=8):
                return f"{root} {month:02d}-{year % 100:02d}"
    raise ValueError(day)


def send(lines):
    """Write one OIF file (several instruction lines) into NinjaTrader's incoming folder."""
    path = os.path.join(ninja_dir(), "incoming", f"oif{next(_ids)}.txt")
    with open(path, "w", encoding="ascii") as f:  # written in place: NinjaTrader watches for new files
        f.write("\n".join(lines) + "\n")
    for line in lines:
        log.info(f"Ninja: {line}")


def order_state(order_id):
    """(state, filled, avg price) from NinjaTrader's outgoing order file, or None if not there yet.
    NinjaTrader names it '<account>_<order id>.txt' (e.g. 'Sim101_tb123.txt'), content 'FILLED;2;26228.875'."""
    matches = glob.glob(os.path.join(ninja_dir(), "outgoing", f"*_{order_id}.txt"))
    if not matches:
        return None
    try:
        with open(matches[0], encoding="utf-8", errors="replace") as f:
            parts = f.read().strip().split(";")
        return parts[0].strip().upper().replace(" ", ""), float(parts[1] or 0), float(parts[2] or 0)
    except (OSError, IndexError, ValueError):
        return None


def keep_awake():
    """Stop Windows from sleeping while the mirror runs (it is undone when the process exits)."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # CONTINUOUS | SYSTEM_REQUIRED
    except Exception:
        pass


def ninja_running():
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NinjaTrader.exe"], capture_output=True, text=True).stdout
        return "NinjaTrader.exe" in out
    except Exception:
        return True


def tick_round(price, tick):
    return round(round(price / tick) * tick, 2)


def place(account, instrument, action, qty, kind, tif="DAY", limit=0.0, stop=0.0, oco=""):
    order_id = f"tb{next(_ids)}"
    lim = f"{limit:.2f}" if limit else ""
    stp = f"{stop:.2f}" if stop else ""
    return order_id, f"PLACE;{account};{instrument};{action};{qty};{kind};{lim};{stp};{tif};{oco};{order_id};;"


# --------------------------------------------------------------------------- Alpaca side

def alpaca_trade(clients, symbol, day):
    """The bot's intraday trade on Alpaca today: direction, filled qty, average fill, initial stop,
    take-profit prices per lot (step 1, 2, ...), the current stop, and whether shares are still held."""
    since = intraday.et_time(day, "09:00").tz_convert("UTC").to_pydatetime()
    orders = clients.trading.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.ALL, symbols=[symbol], after=since, nested=True, limit=200))
    entries = [o for o in orders if o.legs and float(o.filled_qty or 0) > 0
               and (o.client_order_id or "").startswith(intraday.BOT_ORDER_TAG)]  # the bot's, not manual trades
    if not entries:
        return None
    side = entries[0].side
    sign = 1 if side == OrderSide.BUY else -1
    qty = sum(float(o.filled_qty) for o in entries)
    avg = sum(float(o.filled_qty) * float(o.filled_avg_price) for o in entries) / qty
    stops, takes = [], []
    for o in entries:
        for leg in o.legs:
            if leg.stop_price is not None:
                stops.append(float(leg.stop_price))
            elif leg.limit_price is not None:
                takes.append(float(leg.limit_price))
    open_stops = [float(o.stop_price) for o in clients.trading.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, symbols=[symbol], limit=100))
        if o.stop_price is not None and o.side != side]
    try:
        position = float(clients.trading.get_open_position(symbol).qty)
    except Exception:
        position = 0.0
    held = position * sign > 0
    initial_stop = min(stops) if sign == 1 else max(stops)
    current = (max(open_stops) if sign == 1 else min(open_stops)) if open_stops else initial_stop
    return {"direction": "long" if sign == 1 else "short", "sign": sign, "qty": qty, "entry": avg,
            "stop": initial_stop, "current_stop": current, "takes": sorted(takes, key=lambda p: p * sign),
            "held": held, "single": len(entries) == 1}


# --------------------------------------------------------------------------- the mirror

def load_state(day):
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("day") == day:
            return state
    except (OSError, ValueError):
        pass
    return {"day": day, "symbols": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def step(clients, account, day, state):
    """One pass over every mirrored symbol. Returns True while something is still open."""
    busy = False
    for symbol, spec in config.FUTURES.items():
        st = state["symbols"].setdefault(symbol, {"status": "waiting"})
        if st["status"] in ("closed", "skipped", "failed"):
            continue
        instrument = st.get("instrument") or ninja_instrument(spec["ticker"].rstrip("1!"), day)
        trade = alpaca_trade(clients, symbol, day)

        if st["status"] == "waiting":
            if not trade or not trade["held"]:
                continue
            risk_ps = abs(trade["entry"] - trade["stop"])
            n = webhook.contracts(symbol, risk_ps)
            if n < 1:
                log.info(f"Ninja: {symbol} trade not mirrored: one contract is over its risk limit.")
                st["status"] = "skipped"
                continue
            action = "BUY" if trade["sign"] == 1 else "SELL"
            order_id, line = place(account, instrument, action, n, "MARKET")
            send([line])
            st.update(status="entering", instrument=instrument, entry_id=order_id, contracts=n,
                      lots=webhook.lots_for(n, trade["single"]), sign=trade["sign"], alpaca_entry=trade["entry"],
                      alpaca_stop=trade["stop"], takes=trade["takes"], sent_at=bot.now_et().isoformat())
            log.info(f"Ninja: {symbol} {trade['direction']} -> {instrument} {n} contract(s) at market "
                     f"on {account} (risk about ${n * risk_ps * webhook.per_etf_dollar(spec):.0f}).")
            busy = True
            continue

        busy = True
        if st["status"] == "entering":
            got = order_state(st["entry_id"])
            if got and got[0] == "REJECTED":
                log.error(f"Ninja: {symbol} entry rejected by NinjaTrader; nothing is open. Check NinjaTrader's log.")
                st["status"] = "failed"
                continue
            if not got or got[0] != "FILLED":
                if pd.Timestamp(st["sent_at"]) < bot.now_et() - pd.Timedelta(minutes=2):
                    log.error(f"Ninja: {symbol} entry not filled after 2 minutes (state {got}); "
                              f"is ATI on and {account} connected? Check NinjaTrader.")
                    st["status"] = "failed"
                continue
            fill, sign, tick = got[2], st["sign"], spec["tick"]
            ratio = fill / st["alpaca_entry"]
            stop = tick_round(st["alpaca_stop"] * ratio, tick)
            exit_action = "SELL" if sign == 1 else "BUY"
            lines, lots = [], []
            for k, qty in enumerate(st["lots"], start=1):
                take_etf = st["takes"][min(k, len(st["takes"])) - 1]
                take = tick_round(take_etf * ratio, tick)
                oco = f"oco{next(_ids)}"
                stop_id, stop_line = place(account, st["instrument"], exit_action, qty, "STOPMARKET", stop=stop, oco=oco)
                take_id, take_line = place(account, st["instrument"], exit_action, qty, "LIMIT", limit=take, oco=oco)
                lines += [stop_line, take_line]
                lots.append({"k": k, "qty": qty, "stop_id": stop_id, "take_id": take_id, "take": take})
            send(lines)
            st.update(status="open", fill=fill, ratio=ratio, stop=stop, lots_live=lots)
            takes = ", ".join(f"{lot['take']:.2f}" for lot in lots)
            log.info(f"Ninja: {symbol} filled {st['contracts']} at {fill:.2f}; stop {stop:.2f}, take-profit(s) {takes}.")
            continue

        if st["status"] == "open":
            if not trade or not trade["held"]:  # the bot's trade is over: close what is left
                send([f"CLOSEPOSITION;{account};{st['instrument']};;;;;;;;;;"])
                st["status"] = "closed"
                log.info(f"Ninja: {symbol} Alpaca trade is over; closed the {st['instrument']} position.")
                continue
            sign, tick = st["sign"], spec["tick"]
            new_stop = tick_round(trade["current_stop"] * st["ratio"], tick)
            if (new_stop - st["stop"]) * sign >= tick:  # the bot tightened its stop: follow it
                lines = []
                for lot in st["lots_live"]:
                    got = order_state(lot["stop_id"])
                    if got and got[0] in ("FILLED", "CANCELLED", "REJECTED"):
                        continue
                    lines.append(f"CHANGE;;;;0;;0;{new_stop:.2f};;;{lot['stop_id']};;")
                if lines:
                    send(lines)
                log.info(f"Ninja: {symbol} stop moved {st['stop']:.2f} -> {new_stop:.2f} with the bot's.")
                st["stop"] = new_stop
    return busy


def flatten_all(account, state, reason):
    for symbol, st in state["symbols"].items():
        if st.get("status") in ("entering", "open"):
            send([f"CLOSEPOSITION;{account};{st['instrument']};;;;;;;;;;"])
            st["status"] = "closed"
            log.info(f"Ninja: {symbol} closed ({reason}).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", help="NinjaTrader account (default: NINJA_ACCOUNT in .env, else config)")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args()
    bot.LOG_FILE = os.path.join(HERE, "ninja.log")  # its own log: bot.log is committed by the GitHub runs
    bot.setup_logging()
    clients = bot.Clients()  # also loads .env
    args.account = args.account or os.getenv("NINJA_ACCOUNT") or config.NINJA_ACCOUNT
    keep_awake()
    if not ninja_running():
        log.error("Ninja: NinjaTrader is not running; open it, log in and turn on AT Interface. Waiting for it.")
    now = bot.now_et()
    day = now.date().isoformat()
    log.info(f"===== NinjaTrader mirror started (account {args.account}, {ninja_dir()}) =====")
    state = load_state(day)
    if args.once:
        step(clients, args.account, day, state)
        save_state(state)
        return
    flatten_at = intraday.flatten_time(clients, day)
    intraday.sleep_until(intraday.et_time(day, START))
    while bot.now_et() < flatten_at:
        try:
            step(clients, args.account, day, state)
        except Exception as e:
            log.error(f"Ninja: pass failed ({e}); retrying.")
        save_state(state)
        time.sleep(POLL_SECONDS)
    flatten_all(args.account, state, f"end-of-day flatten {flatten_at:%H:%M}")
    save_state(state)
    log.info("===== NinjaTrader mirror finished =====")


if __name__ == "__main__":
    sys.exit(main())
