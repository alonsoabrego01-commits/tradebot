"""Mirror the intraday trades to a futures account (e.g. Apex on Tradovate) through a TradersPost webhook.

The bot keeps trading QQQ/SPY on Alpaca and stays the brain. When an Alpaca intraday trade gets
its first fill, the same trade is sent to TradersPost in micro futures (config.FUTURES):
  - size: config.FUTURES_RISK_PER_TRADE / (stop distance in dollars per contract), whole contracts,
    at most config.FUTURES_MAX_CONTRACTS;
  - one market order per lot, each with its own stop and take-profit as percent distances from
    the fill (so no QQQ->NQ price conversion is needed). QQQ lots take profit at step 1, 2, 3...
    like the Alpaca ladder; SPY is one lot with the fixed 3R target;
  - when the Alpaca trade is over (stop, trailing stop, 15:55 flatten, closed by hand), an `exit`
    closes whatever is left of the futures position and cancels its orders.
TradersPost cancels a position's stop when it is partly closed, so the bot never moves futures
stops itself: the futures stops stay at the initial stop, and the exit above stands in for the
trailing stop while a bot run is watching.

The webhook URL is secret: it is read from the TRADERSPOST_WEBHOOK_URL environment variable
(.env locally, a GitHub secret on Actions). Unset = off: the payloads are only logged.
A failed webhook is logged and never stops the Alpaca side.
"""
import json
import math
import os
import urllib.error
import urllib.request

import bot
import config

log = bot.log
URL_ENV = "TRADERSPOST_WEBHOOK_URL"


def enabled():
    return bool(os.getenv(URL_ENV, "").strip())


def contracts(symbol, risk_ps):
    spec = config.FUTURES.get(symbol)
    if not spec or risk_ps <= 0:
        return 0
    per_contract = risk_ps * spec["per_etf_dollar"]
    return max(0, min(config.FUTURES_MAX_CONTRACTS, math.floor(config.FUTURES_RISK_PER_TRADE / per_contract)))


def lots_for(n, single):
    """Same split as the Alpaca ladder: half of what is left at each step, rounded up."""
    if single:
        return [n] if n > 0 else []
    lots, left = [], n
    while left > 0:
        lots.append(math.ceil(left / 2))
        left -= lots[-1]
    return lots


def send(payload):
    """POST one signal. Returns True on a 2xx answer with success, False otherwise."""
    url = os.getenv(URL_ENV, "").strip()
    text = json.dumps(payload)
    if not url:
        log.info(f"Futures (TradersPost not configured, not sent): {text}")
        return False
    request = urllib.request.Request(url, data=text.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode(errors="replace")
        ok = json.loads(body).get("success", True) if body.strip().startswith("{") else True
        log.info(f"Futures: sent {text} -> {'ok' if ok else body[:200]}")
        return bool(ok)
    except urllib.error.HTTPError as e:
        log.error(f"Futures: TradersPost refused {text}: HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
    except Exception as e:
        log.error(f"Futures: could not reach TradersPost for {text}: {e}")
    return False


def open_trade(symbol, trade):
    """Send the futures version of an Alpaca trade that just got its first fill.
    Returns what was sent, to be stored on the trade as trade['futures']."""
    spec = config.FUTURES.get(symbol)
    if not spec:
        return {"contracts": 0, "why": "no futures mapping"}
    sign = 1 if trade["direction"] == "long" else -1
    entry, base, step = float(trade["entry"]), float(trade["base"]), float(trade["step"])
    risk_ps = abs(entry - float(trade["stop"]))
    n = contracts(symbol, risk_ps)
    if n < 1:
        per = risk_ps * spec["per_etf_dollar"]
        log.info(f"Futures: {symbol} trade not sent: one {spec['ticker']} risks ${per:.0f}, "
                 f"over the ${config.FUTURES_RISK_PER_TRADE:.0f} budget.")
        return {"contracts": 0, "why": "one contract is over the risk budget"}
    single = len(trade["lots"]) == 1
    sizes = lots_for(n, single)
    stop_pct = round(risk_ps / entry * 100, 4)
    sent = []
    for k, qty in enumerate(sizes, start=1):
        take_pct = round(abs(base + sign * k * step - entry) / entry * 100, 4)
        payload = {
            "ticker": spec["ticker"],
            "action": ("buy" if sign == 1 else "sell") if k == 1 else "add",
            "quantity": qty,
            "orderType": "market",
            "takeProfit": {"percent": take_pct},
            "stopLoss": {"type": "stop", "percent": stop_pct},
            "message": f"{trade['model']} {trade['direction']} lot {k}/{len(sizes)}"[:255],
        }
        sent.append(send(payload))
    risk = n * risk_ps * spec["per_etf_dollar"]
    log.info(f"Futures: {symbol} -> {spec['ticker']} {trade['direction']} {n} contract(s) in lots "
             f"{' / '.join(map(str, sizes))}, stop {stop_pct:g}% away, risk about ${risk:.0f}.")
    return {"ticker": spec["ticker"], "contracts": n, "lots": sizes, "stop_pct": stop_pct,
            "risk": round(risk, 2), "sent": sent}


def close_trade(symbol, trade, reason):
    """The Alpaca trade is over: close what is left of the futures position and cancel its orders.
    Sent once per trade (marks trade['futures']['closed']; the caller saves the state).
    Returns None when there was nothing to send."""
    futures = trade.get("futures") or {}
    if not futures.get("contracts") or futures.get("closed"):
        return None
    futures["closed"] = True
    return send({"ticker": futures["ticker"], "action": "exit", "orderType": "market", "cancel": True,
                 "message": f"{trade['model']}: {reason}"[:255]})
