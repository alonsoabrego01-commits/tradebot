# Strategy settings. Three models share this account:
#   - bot.py:      daily 20/50 moving average crossover on SYMBOLS (runs once after 9:30 ET)
#   - intraday.py: QQQ inverse fair value gap (IFVG_*) and SPY liquidity sweep and reversal (SWEEP_*)

# --- Daily crossover (bot.py) ------------------------------------------------
SYMBOLS = ["AAPL"]    # QQQ and SPY are traded only by the intraday models
SHORT_WINDOW = 20      # Fast moving average period (days)
LONG_WINDOW = 50       # Slow moving average period (days)
TRADE_QTY = 1          # Shares per crossover trade (capped by the hard limits)
LOOKBACK_DAYS = 100    # Trading days of history to fetch

# Stop-loss and take-profit placed with every crossover entry (as an Alpaca bracket order).
ATR_PERIOD = 14        # Days used for Average True Range
STOP_ATR_MULT = 2.0    # Stop sits this many ATRs below entry
TARGET_R = 3.0         # Target sits this many R above entry (1R = entry - stop)

# --- Intraday models (intraday.py) -------------------------------------------
INTRADAY_RISK_PER_TRADE = 125.0     # Dollars from entry to stop; two losers = the $250 daily loss limit
INTRADAY_POSITION_VALUE = 25_000.0  # Max dollars per intraday position, so both fit without margin
INTRADAY_STOP_BUFFER = 0.05         # Stop sits this far beyond its level
INTRADAY_FLATTEN_AT = "15:55"       # Open intraday trades are closed at this ET time
# Scale-out: every time price gains another step from entry, half of what is left is closed
# and the stop on the rest moves up to one step behind (entry after step 1, step 1 after step 2...).
SCALE_STEP_PCT = 0.36               # Step size as % of entry price (about 100 Nasdaq futures points)

# QQQ inverse fair value gap, traded with the order flow
IFVG_SYMBOL = "QQQ"
IFVG_STRUCTURE_FROM = "08:00"  # Swings and gaps are read on 5-minute bars from this ET time
IFVG_FIRST_ORDER = "10:00"     # Orders are only placed for setups completing from this ET time...
IFVG_LAST_ORDER = "12:00"      #   ...to this one
IFVG_ORDER_MINUTES = 45        # An unfilled limit order is cancelled after this long
IFVG_MAX_GAP_BARS = 12         # The gap must have formed within this many 5-minute bars (one hour)
IFVG_MIN_STOP_PCT = 0.05       # Stop is at least this % of price from entry

# SPY liquidity sweep and reversal
SWEEP_SYMBOL = "SPY"
SWEEP_RECLAIM_BARS = 3         # The sweep bar or one of the next two must close back inside the level
SWEEP_PREMARKET_END = "09:15"  # Premarket range = 4:00 ET to this time (later minutes are not free data by 9:35)
SWEEP_FIRST_ENTRY = "09:35"    # The reclaim bar must complete at or after this ET time
SWEEP_ENTRY_CUTOFF = "11:30"   # The reclaim bar must complete by this ET time
SWEEP_MIN_STOP_PCT = 0.05      # Stop is at least this % of price from entry
SWEEP_TARGET_R = 3.0           # Fixed target at this many R; one bracket, no scale-out

# Futures mirror (webhook.py): intraday trades are also sent to TradersPost -> Tradovate (e.g. Apex)
# when the TRADERSPOST_WEBHOOK_URL environment variable is set. Unset = off (payloads only logged).
FUTURES = {  # ratio: futures price / ETF price (fallback; measured live from Yahoo when possible)
    "QQQ": {"ticker": "MNQ1!", "yahoo": "MNQ=F", "ratio": 41.2, "point_value": 2.0, "tick": 0.25},  # MNQ $2 a point
    # SPY at a smaller size: Apex 50K test on 2026 with QQQ at $350 + SPY at $100 passed 100%
    # (median 44 days vs 50 for QQQ alone); SPY at $150-200: 94%; at $350: 78%.
    "SPY": {"ticker": "MES1!", "yahoo": "MES=F", "ratio": 10.0, "point_value": 5.0, "tick": 0.25,
            "risk": 100.0, "min_one": False},  # MES $5 a point
}
FUTURES_RISK_PER_TRADE = 350.0  # Dollars to the stop per futures trade, unless FUTURES[symbol]["risk"] is set
FUTURES_MIN_ONE_CONTRACT_UP_TO = 1000.0  # If one contract risks more than the budget, still trade 1 if
                                         # its risk is at most this (2026 worst: ~$890; Apex test still 100%)
FUTURES_MAX_CONTRACTS = 10      # Max micro contracts per futures trade

# ---------------------------------------------------------------------------
# HARD LIMITS. Set by the account owner. Only a human edits these.
# lessons.md rules can't override them, and the bot never changes them.
# A stop at entry on every trade is enforced in code and can't be turned off.
# ---------------------------------------------------------------------------
MAX_SHARES_PER_TRADE = 100      # Max shares in any one trade / held per symbol
MAX_POSITION_VALUE = 50_000.0   # Max dollar value of any one position (no margin)
MAX_DAILY_LOSS = 250.0          # Stop trading for the day once today's loss reaches this many dollars
MAX_TRADES_PER_DAY = 3          # Max new entries per day across all models
INTRADAY_MAX_TRADES_PER_MODEL = 1  # Each intraday model takes at most one trade per day

# Journal / review settings
REVIEW_EVERY = 20         # Write a lessons.md review after every N closed trades
MIN_EXAMPLES = 5          # A proposed rule needs at least this many losing trades showing the pattern
SWING_DAYS = 20           # Swing high/low window for the Fib retracement of a crossover entry
