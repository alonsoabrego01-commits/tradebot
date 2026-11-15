# Strategy settings. Three models share this account:
#   - bot.py:      daily 20/50 moving average crossover on SYMBOLS (runs once after 9:30 ET)
#   - intraday.py: QQQ opening range breakout (ORB_*) and SPY trend-day VWAP pullback (VWAP_*)

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

# QQQ opening range breakout
ORB_SYMBOL = "QQQ"
ORB_RANGE_MINUTES = 15         # Opening range = 9:30 to 9:45 ET (a multiple of 5)
ORB_STOP = "mid"               # "mid" = midpoint of the opening range, "range" = its far side
ORB_TARGET_R = 2.0             # Take-profit this many R from entry
ORB_ENTRY_CUTOFF = "10:30"     # The breakout bar must complete by this ET time
ORB_TREND_FILTER = False       # True: longs only above the ORB_TREND_MA-day average, shorts only below
ORB_TREND_MA = 20

# SPY trend-day VWAP pullback
VWAP_SYMBOL = "SPY"
VWAP_OPEN_MINUTES = 30         # The open is judged over 9:30 to 10:00 ET (a multiple of 5)
VWAP_MIN_OPEN_MOVE_ATR = 0.25  # Strong open: price at least this many daily ATRs from the 9:30 open...
VWAP_MIN_SIDE_SHARE = 0.8      #   ...and at least this share of 5-minute closes on that side of VWAP
VWAP_FAST_EMA = 9              # A 5-minute close back beyond this EMA triggers the entry
VWAP_SLOW_EMA = 21             # Pullback zone: the nearer of VWAP and this EMA
VWAP_TARGET_R = 2.0            # Take-profit this many R from entry
VWAP_ENTRY_CUTOFF = "11:00"    # The entry bar must complete by this ET time
VWAP_MIN_STOP_PCT = 0.05       # Stop is at least this % of price from entry

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
