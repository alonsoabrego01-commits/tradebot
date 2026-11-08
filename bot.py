"""Moving average crossover bot for an Alpaca paper trading account.

Run:  python bot.py            (checks every symbol in config.SYMBOLS and trades on signals)
      python bot.py --dry-run  (checks for signals but places no orders)
      python bot.py --symbol QQQ  (only checks one symbol)
      python bot.py --force buy --symbol AAPL  (places an order without checking, for testing)
      python bot.py --scheduled  (how GitHub Actions runs it: once a day, 9:30-12:00 ET only)

Every entry is a bracket order with a stop-loss and a take-profit target. Closed trades
are written to trade_journal.md, and every config.REVIEW_EVERY trades a review goes to
lessons.md. Every run is logged with timestamps to bot.log next to this file.
"""

import os
import sys

# Under pythonw (how Task Scheduler runs the bot, with no window) there is no console;
# send anything printed or any crash before logging starts to bot_errors.log.
if sys.stderr is None:
    sys.stdout = sys.stderr = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_errors.log"), "a", encoding="utf-8"
    )

import argparse
import logging
import time
import warnings
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrderByIdRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from dotenv import load_dotenv

import config
import journal

# yfinance triggers pandas deprecation warnings that aren't ours to fix.
warnings.filterwarnings("ignore", module="yfinance")

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
log = logging.getLogger("tradebot")

DONE_STATUSES = {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}


def now_et():
    return pd.Timestamp.now(tz="America/New_York")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    # Log in New York time wherever the bot runs (GitHub's servers use UTC).
    fmt.converter = lambda ts: pd.Timestamp(ts, unit="s", tz="UTC").tz_convert("America/New_York").timetuple()
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    # Also echo to the terminal when run by hand, and to the GitHub Actions run log.
    if sys.stdout and (sys.stdout.isatty() or os.getenv("GITHUB_ACTIONS")):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)


# --------------------------------------------------------------------------- data

def alpaca_daily_bars(clients, symbol, start):
    # Free Alpaca accounts can read consolidated (SIP) data older than 15 minutes.
    bars = clients.data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start,
        end=datetime.now(timezone.utc) - timedelta(minutes=16),
        adjustment=Adjustment.ALL, feed=DataFeed.SIP,
    )).df
    history = bars.xs(symbol, level="symbol").rename(columns=str.capitalize)
    history.index = history.index.tz_convert("America/New_York")
    return history


def fetch_bars(symbol, lookback_days, clients=None):
    """Completed daily bars (Open/High/Low/Close) for the last `lookback_days` trading days.

    Uses Alpaca market data when clients are given (Yahoo often blocks cloud servers),
    falling back to Yahoo Finance.
    """
    # Pull extra calendar days so weekends/holidays still leave `lookback_days` trading days.
    start = datetime.now(timezone.utc) - timedelta(days=int(lookback_days * 1.6) + 10)
    history = None
    if clients:
        try:
            history = alpaca_daily_bars(clients, symbol, start)
        except Exception as e:
            log.warning(f"{symbol}: Alpaca bars unavailable ({e}); falling back to Yahoo Finance.")
    if history is None or history.empty:
        history = yf.Ticker(symbol).history(start=start.strftime("%Y-%m-%d"))
    if history.empty:
        raise RuntimeError(f"No price data returned for {symbol}")

    # During market hours the last row is today's unfinished bar; only compare completed days.
    now = pd.Timestamp.now(tz=history.index.tz)
    if history.index[-1].date() == now.date() and now.hour < 16:
        log.info(f"{symbol}: ignoring today's incomplete bar; using completed days only.")
        history = history.iloc[:-1]

    return history[["Open", "High", "Low", "Close"]].tail(lookback_days)


def find_crossover(closes, short_window, long_window, verbose=True):
    """Return "buy", "sell", or None based on the last two trading days."""
    if len(closes) < long_window + 1:
        raise RuntimeError(
            f"Need at least {long_window + 1} days of data, got {len(closes)}"
        )

    short_ma = closes.rolling(short_window).mean()
    long_ma = closes.rolling(long_window).mean()

    prev_diff = short_ma.iloc[-2] - long_ma.iloc[-2]
    curr_diff = short_ma.iloc[-1] - long_ma.iloc[-1]

    if verbose:
        log.info(f"Latest close: {closes.index[-1].date()} at {closes.iloc[-1]:.2f}")
        log.info(f"{short_window}-day MA: {short_ma.iloc[-2]:.2f} -> {short_ma.iloc[-1]:.2f}")
        log.info(f"{long_window}-day MA: {long_ma.iloc[-2]:.2f} -> {long_ma.iloc[-1]:.2f}")

    if prev_diff <= 0 < curr_diff:
        return "buy"   # short MA crossed above long MA
    if prev_diff >= 0 > curr_diff:
        return "sell"  # short MA crossed below long MA
    return None


class Clients:
    def __init__(self):
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key or "your_" in api_key:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in the .env file first.")
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        self.news = NewsClient(api_key, secret_key)


def latest_price(clients, symbol, fallback):
    try:
        trade = clients.data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        )
        return float(trade[symbol].price)
    except Exception as e:
        log.warning(f"{symbol}: latest price unavailable ({e}); using last close {fallback:.2f}")
        return fallback


def recent_headlines(clients, symbol):
    try:
        news = clients.news.get_news(
            NewsRequest(symbols=symbol, start=datetime.now(timezone.utc) - timedelta(days=1), limit=50)
        )
        return [item.headline for item in news.data.get("news", [])]
    except Exception as e:
        log.warning(f"{symbol}: news unavailable ({e})")
        return []


def held_qty(client, symbol):
    try:
        return abs(float(client.get_open_position(symbol).qty))  # shorts report negative qty
    except Exception:
        return 0.0


def day_pnl(account):
    """Today's P&L. last_equity is 0 on a brand-new account, which isn't a real gain."""
    last = float(account.last_equity)
    return float(account.equity) - last if last > 0 else 0.0


def market_closed_today(client, clock):
    """True on weekends and market holidays (not just outside trading hours)."""
    if clock.is_open:
        return False
    today = clock.timestamp.date()
    return not client.get_calendar(GetCalendarRequest(start=today, end=today))


def wait_for_fill(client, order_id, seconds=15):
    order = client.get_order_by_id(order_id)
    for _ in range(seconds):
        if order.status == OrderStatus.FILLED or order.status in DONE_STATUSES:
            break
        time.sleep(1)
        order = client.get_order_by_id(order_id)
    return order


# --------------------------------------------------------------------------- trade lifecycle

def bracket_leg_ids(client, parent_id):
    parent = client.get_order_by_id(parent_id, GetOrderByIdRequest(nested=True))
    stop_id = target_id = None
    for leg in parent.legs or []:
        if leg.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            stop_id = str(leg.id)
        else:
            target_id = str(leg.id)
    return stop_id, target_id


def reconcile(clients, state):
    """Pick up fills that happened since the last run: entries, stops, targets, exits."""
    client = clients.trading
    for symbol in list(state["open"]):
        trade = state["open"][symbol]

        if trade["entry"] is None:  # entry order was waiting for the market to open
            parent = client.get_order_by_id(trade["entry_order_id"])
            if parent.status in DONE_STATUSES:
                log.info(f"{symbol}: entry order {parent.status.value}; dropping it.")
                del state["open"][symbol]
                journal.save_state(state)
                continue
            if parent.status != OrderStatus.FILLED:
                continue
            trade["entry"] = round(float(parent.filled_avg_price), 2)
            trade["entry_time"] = journal.to_et(parent.filled_at).isoformat()
            trade["stop_order_id"], trade["target_order_id"] = bracket_leg_ids(client, trade["entry_order_id"])
            journal.save_state(state)
            log.info(f"{symbol}: entry filled at {trade['entry']:.2f} (stop {trade['stop']:.2f}, target {trade['target']:.2f})")

        exits = [
            (trade.get("stop_order_id"), "Stop hit"),
            (trade.get("target_order_id"), "Target hit"),
            (trade.get("exit_order_id"), "Crossover sell signal"),
        ]
        closed = False
        for order_id, reason in exits:
            if not order_id:
                continue
            order = client.get_order_by_id(order_id)
            if order.status == OrderStatus.FILLED:
                journal.record_close(state, symbol, order.filled_avg_price, order.filled_at, reason)
                closed = True
                break
        if closed:
            continue

        if held_qty(client, symbol) <= 0:
            # Position is gone but none of our exit orders filled: closed by hand.
            sells = client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol], side=OrderSide.SELL if trade["direction"] == "long" else OrderSide.BUY,
                after=pd.Timestamp(trade["entry_time"]).to_pydatetime(),
            ))
            filled = [o for o in sells if o.status == OrderStatus.FILLED]
            if filled:
                last = max(filled, key=lambda o: o.filled_at)
                journal.record_close(state, symbol, last.filled_avg_price, last.filled_at, "Closed outside the bot")
            else:
                log.warning(f"{symbol}: position gone but no sell fill found; check Alpaca. Leaving trade open.")


def entry_gates(signal_ok, clock, qty, today_pnl, trades_today):
    """Signal, market and hard-limit checks for the checklist. Hard limits are enforced before this."""
    mark = lambda ok: journal.MET if ok else journal.NOT_MET
    return [
        [f"{config.SHORT_WINDOW}-day MA crossed above {config.LONG_WINDOW}-day MA on the last completed day"
         if signal_ok else "Crossover signal (none: forced test entry)", mark(signal_ok)],
        ["Market open when the order was sent", mark(clock.is_open)],
        [f"Hard limit: max {config.MAX_SHARES_PER_TRADE:g} shares and ${config.MAX_POSITION_VALUE:,.0f} per position", mark(qty > 0)],
        [f"Hard limit: daily loss under ${config.MAX_DAILY_LOSS:,.0f} (today {today_pnl:+.2f})",
         mark(today_pnl > -config.MAX_DAILY_LOSS)],
        [f"Hard limit: max {config.MAX_TRADES_PER_DAY} trades per day (this is #{trades_today + 1})",
         mark(trades_today < config.MAX_TRADES_PER_DAY)],
        ["Hard limit: stop attached at entry (bracket order)", journal.MET],
        ["No APPROVED lessons.md rule blocks the entry", journal.MET],
    ]


def enter_trade(clients, state, symbol, bars, context, clock, dry_run, forced=False):
    client = clients.trading
    if symbol in state["open"]:
        log.info(f"{symbol}: already in a tracked trade. Skipping entry.")
        return

    blocked = journal.blocking_rules(context, journal.approved_rules())
    qty = min(config.TRADE_QTY, config.MAX_SHARES_PER_TRADE - held_qty(client, symbol),
              int(config.MAX_POSITION_VALUE // max(context["price"], 0.01)))
    today = day_pnl(client.get_account())
    day_key = journal.to_et(clock.timestamp).date().isoformat()
    trades_today = state["entries_by_day"].get(day_key, 0)

    # Hard limits and approved rules are gates, not just journal notes.
    if qty <= 0:
        log.info(f"{symbol}: max position size ({config.MAX_SHARES_PER_TRADE:g}) already held. Skipping entry.")
        return
    if today <= -config.MAX_DAILY_LOSS:
        log.info(f"{symbol}: daily loss limit hit ({today:+.2f}). No more trading today.")
        return
    if trades_today >= config.MAX_TRADES_PER_DAY:
        log.info(f"{symbol}: max trades per day ({config.MAX_TRADES_PER_DAY}) reached. Skipping entry.")
        return
    if blocked:
        log.info(f"{symbol}: entry skipped by APPROVED lessons.md rule(s): {', '.join(blocked)}")
        return

    price = context["price"]
    stop = round(price - config.STOP_ATR_MULT * context["atr"], 2)
    target = round(price + config.TARGET_R * (price - stop), 2)
    if not 0 < stop < price:  # a stop at entry is mandatory; never trade without a valid one
        log.error(f"{symbol}: invalid stop {stop} for price {price}. Not trading.")
        return

    items = journal.checklist(context, entry_gates(not forced, clock, qty, today, trades_today))
    grade, grade_reason = journal.grade(items)
    log.info(
        f"{symbol}: BUY {qty:g} near {price:.2f}, stop {stop:.2f} ({config.STOP_ATR_MULT:g}×ATR {context['atr']:.2f}), "
        f"target {target:.2f} ({config.TARGET_R:g}R). Grade {grade} ({grade_reason})."
    )
    if dry_run:
        log.info("Dry run: no order placed.")
        return

    order = client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target),
        stop_loss=StopLossRequest(stop_price=stop),
    ))
    log.info(f"{symbol}: submitted bracket BUY id={order.id} status={order.status.value}")

    state["entries_by_day"][day_key] = trades_today + 1
    state["open"][symbol] = {
        "symbol": symbol, "direction": "long", "model": "crossover", "qty": qty,
        "setup": journal.SETUP if not forced else "Forced test order (no signal)",
        "entry_order_id": str(order.id), "entry": None, "entry_time": None,
        "stop": stop, "target": target, "stop_order_id": None, "target_order_id": None,
        "context": context, "checklist": items, "grade": grade, "grade_reason": grade_reason,
        "adopted": False,
    }
    journal.save_state(state)

    order = wait_for_fill(client, order.id)
    if order.status == OrderStatus.FILLED:
        reconcile(clients, state)  # records the fill price and the stop/target leg ids
    else:
        log.info(f"{symbol}: not filled yet (status={order.status.value}); the next run will pick it up.")


def adopt_position(clients, state, symbol, context, dry_run):
    """Put a stop and target on a position the bot didn't open (every trade needs a stop)."""
    client = clients.trading
    position = client.get_open_position(symbol)
    qty = float(position.qty)
    entry = float(position.avg_entry_price)
    price = context["price"]
    stop = round(min(entry, price) - config.STOP_ATR_MULT * context["atr"], 2)
    target = round(entry + config.TARGET_R * (entry - stop), 2)
    log.info(
        f"{symbol}: found {qty:g} share(s) the bot didn't open (avg {entry:.2f}). "
        f"Adopting with stop {stop:.2f} and target {target:.2f}."
    )
    if dry_run:
        log.info("Dry run: no protective orders placed.")
        return

    order = client.submit_order(LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO, limit_price=target,
        take_profit=TakeProfitRequest(limit_price=target),
        stop_loss=StopLossRequest(stop_price=stop),
    ))
    target_id, stop_id = str(order.id), None
    for leg in client.get_order_by_id(order.id, GetOrderByIdRequest(nested=True)).legs or []:
        stop_id = str(leg.id)

    items = journal.checklist(context, [
        ["Entry made by the bot on a crossover signal (this position was opened outside the bot)", journal.NOT_MET],
        ["Hard limit: stop attached (added on adoption, not at entry)", journal.NOT_MET],
    ])
    grade, grade_reason = journal.grade(items)
    state["open"][symbol] = {
        "symbol": symbol, "direction": "long", "model": "crossover", "qty": qty, "setup": "Adopted position (not a bot signal)",
        "entry_order_id": None, "entry": round(entry, 2),
        "entry_time": pd.Timestamp.now(tz="UTC").isoformat(),
        "stop": stop, "target": target, "stop_order_id": stop_id, "target_order_id": target_id,
        "context": context, "checklist": items, "grade": grade, "grade_reason": grade_reason,
        "adopted": True,
    }
    journal.save_state(state)
    log.info(f"{symbol}: protective OCO placed id={order.id}")


def exit_trade(clients, state, symbol, dry_run, reason="Crossover sell signal"):
    client = clients.trading
    trade = state["open"][symbol]
    log.info(f"{symbol}: closing tracked trade ({reason}).")
    if dry_run:
        log.info("Dry run: no order placed.")
        return

    if trade["entry"] is None:  # never filled: just cancel the pending entry
        client.cancel_order_by_id(trade["entry_order_id"])
        del state["open"][symbol]
        journal.save_state(state)
        log.info(f"{symbol}: cancelled unfilled entry order.")
        return

    # Free the shares held by the stop/target orders before selling.
    for order_id in (trade.get("stop_order_id"), trade.get("target_order_id")):
        if order_id:
            try:
                client.cancel_order_by_id(order_id)
            except Exception:
                pass  # already filled or cancelled
    for order_id in (trade.get("stop_order_id"), trade.get("target_order_id")):
        if order_id:
            for _ in range(10):
                status = client.get_order_by_id(order_id).status
                if status == OrderStatus.FILLED:
                    reconcile(clients, state)  # the stop/target beat us to it
                    return
                if status in DONE_STATUSES:
                    break
                time.sleep(1)

    order = client.submit_order(MarketOrderRequest(
        symbol=symbol, qty=trade["qty"], side=OrderSide.SELL if trade["direction"] == "long" else OrderSide.BUY, time_in_force=TimeInForce.DAY,
    ))
    trade["exit_order_id"] = str(order.id)
    journal.save_state(state)
    log.info(f"{symbol}: submitted SELL id={order.id}")

    order = wait_for_fill(client, order.id)
    if order.status == OrderStatus.FILLED:
        journal.record_close(state, symbol, order.filled_avg_price, order.filled_at, reason)
    else:
        log.info(f"{symbol}: sell not filled yet (status={order.status.value}); the next run will journal it.")


# --------------------------------------------------------------------------- main loop

def log_account(client, symbols):
    account = client.get_account()
    held = ", ".join(f"{s} {held_qty(client, s):g}" for s in symbols)
    log.info(
        f"Account {account.account_number}: cash {float(account.cash):,.2f}, "
        f"equity {float(account.equity):,.2f} (today {day_pnl(account):+.2f}), "
        f"shares held: {held}"
    )


def symbol_context(clients, symbol, clock):
    bars = fetch_bars(symbol, config.LOOKBACK_DAYS, clients)
    price = latest_price(clients, symbol, float(bars["Close"].iloc[-1]))
    context = journal.build_context(bars, journal.to_et(clock.timestamp), recent_headlines(clients, symbol), price)
    return bars, context


def trade_symbol(clients, state, symbol, clock, dry_run):
    log.info(f"--- {symbol} ---")
    bars, context = symbol_context(clients, symbol, clock)

    # Which active lessons.md rules apply to today's conditions.
    applying = journal.blocking_rules(context, journal.approved_rules())
    log.info(
        f"{symbol}: today: {context['session']}, trend {context['trend']}, weekly bias {context['htf_bias']}, "
        f"volatility {context['volatility']} (ATR {context['atr_pct']:.2f}%), {context['news_count']} headline(s). "
        f"Active rules applying: {', '.join(applying) + ' (would block a buy)' if applying else 'none'}"
    )

    if symbol not in state["open"] and held_qty(clients.trading, symbol) > 0:
        adopt_position(clients, state, symbol, context, dry_run)

    signal = find_crossover(bars["Close"], config.SHORT_WINDOW, config.LONG_WINDOW)
    if signal is None:
        log.info(f"{symbol}: no crossover. Nothing to do.")
    elif signal == "buy":
        log.info(f"{symbol}: crossover detected: BUY")
        enter_trade(clients, state, symbol, bars, context, clock, dry_run)
    elif symbol in state["open"]:
        log.info(f"{symbol}: crossover detected: SELL")
        exit_trade(clients, state, symbol, dry_run)
    else:
        log.info(f"{symbol}: crossover detected: SELL, but no position to sell.")


def run(args):
    symbols = [args.symbol.upper()] if args.symbol else config.SYMBOLS
    log.info(
        f"===== Run started ({', '.join(symbols)} {config.SHORT_WINDOW}/{config.LONG_WINDOW} MA) ====="
    )
    clients = Clients()
    state = journal.load_state()
    journal.ensure_lessons_file()
    journal.session_briefing(state)
    clock = clients.trading.get_clock()

    reconcile(clients, state)

    if args.force:
        symbol = symbols[0]
        log.info(f"Forcing a {args.force} for {symbol} (signal check skipped).")
        if args.force == "buy":
            bars, context = symbol_context(clients, symbol, clock)
            enter_trade(clients, state, symbol, bars, context, clock, args.dry_run, forced=True)
        elif symbol in state["open"]:
            exit_trade(clients, state, symbol, args.dry_run, reason="Forced sell")
        else:
            log.info(f"{symbol}: no tracked trade to sell.")
        log_account(clients.trading, symbols)
        return

    if market_closed_today(clients.trading, clock):
        log.info("Market is closed today (weekend or holiday). Nothing to do.")
        return

    log_account(clients.trading, symbols)
    failed = []
    for symbol in symbols:
        # One symbol failing (bad data, rejected order) shouldn't stop the others.
        try:
            trade_symbol(clients, state, symbol, clock, args.dry_run)
        except Exception:
            log.exception(f"{symbol}: failed")
            failed.append(symbol)

    log_account(clients.trading, symbols)
    if failed:
        raise RuntimeError(f"Failed symbols: {', '.join(failed)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="don't place orders")
    parser.add_argument(
        "--force", choices=["buy", "sell"], help="skip the signal check and place this order (needs --symbol)"
    )
    parser.add_argument("--symbol", help="only run for this symbol instead of all in config.SYMBOLS")
    parser.add_argument(
        "--scheduled", action="store_true",
        help="cloud schedule: run only between 9:30 and 12:00 ET, and only once per day",
    )
    args = parser.parse_args()
    if args.force and not args.symbol:
        parser.error("--force needs --symbol, e.g. --force buy --symbol AAPL")

    if args.scheduled:
        # GitHub starts the job several times each morning (its UTC schedule ignores daylight
        # saving and can run late); only the first start inside the window does anything.
        now = now_et()
        state = journal.load_state()
        if not (9 * 60 + 30 <= now.hour * 60 + now.minute < 12 * 60):
            print(f"{now:%H:%M} ET is outside the 9:30-12:00 window; skipping.")
            return
        if state.get("last_scheduled_run") == now.date().isoformat():
            print(f"Already ran today ({now.date()}); skipping.")
            return
        state["last_scheduled_run"] = now.date().isoformat()
        journal.save_state(state)

    setup_logging()
    try:
        run(args)
    except Exception:
        log.exception("Run failed")
        sys.exit(1)
    finally:
        log.info("===== Run finished =====")


if __name__ == "__main__":
    main()
