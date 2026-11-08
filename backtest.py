"""Replay past data through the bot's crossover check. Places no orders.

Run:  python backtest.py         (last 365 days, every symbol in config.SYMBOLS)
      python backtest.py 730     (last 730 days)
"""

import sys
import warnings
from datetime import datetime, timedelta

import yfinance as yf

import config
from bot import find_crossover

warnings.filterwarnings("ignore")


def backtest(symbol, days):
    start = datetime.now() - timedelta(days=days + 120)  # warm-up room for the long MA
    closes = yf.Ticker(symbol).history(start=start.strftime("%Y-%m-%d"))["Close"]
    cutoff = closes.index[-1] - timedelta(days=days)

    print(f"{symbol} {config.SHORT_WINDOW}/{config.LONG_WINDOW} crossovers, last {days} days")
    signals = []
    for i in range(config.LONG_WINDOW + 1, len(closes) + 1):
        window = closes.iloc[:i].tail(config.LOOKBACK_DAYS)
        if window.index[-1] < cutoff:
            continue
        signal = find_crossover(window, config.SHORT_WINDOW, config.LONG_WINDOW, verbose=False)
        if signal:
            signals.append((window.index[-1].date(), signal, window.iloc[-1]))
            print(f"  {window.index[-1].date()}  {signal.upper():4}  at {window.iloc[-1]:.2f}")

    if not signals:
        print("  No crossovers in this period.")
        return 0.0

    # Rough result of following the signals with TRADE_QTY shares (long only, like the bot).
    pnl, entry = 0.0, None
    for _, signal, price in signals:
        if signal == "buy" and entry is None:
            entry = price
        elif signal == "sell" and entry is not None:
            pnl += (price - entry) * config.TRADE_QTY
            entry = None
    open_note = ""
    if entry is not None:
        pnl += (closes.iloc[-1] - entry) * config.TRADE_QTY
        open_note = " (includes an open position marked at the latest close)"
    print(f"  Approx. profit/loss: {pnl:+.2f}{open_note}")
    return pnl


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    total = 0.0
    for symbol in config.SYMBOLS:
        total += backtest(symbol, days)
        print()
    print(f"Total approx. profit/loss: {total:+.2f}")


if __name__ == "__main__":
    main()
