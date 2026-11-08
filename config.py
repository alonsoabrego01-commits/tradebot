# Strategy settings. Two models share this account:
#   - bot.py:   daily 20/50 moving average crossover on SYMBOLS (runs at 9:30 ET)
#   - tenam.py: the QQQ 10am model (runs 10:00-11:00 ET, see TENAM_* below)

# --- Daily crossover (bot.py) ------------------------------------------------
SYMBOLS = ["AAPL", "SPY"]  # QQQ is traded only by the 10am model
SHORT_WINDOW = 20      # Fast moving average period (days)
LONG_WINDOW = 50       # Slow moving average period (days)
TRADE_QTY = 1          # Shares per crossover trade (capped by the hard limits)
LOOKBACK_DAYS = 100    # Trading days of history to fetch

# Stop-loss and take-profit placed with every crossover entry (as an Alpaca bracket order).
ATR_PERIOD = 14        # Days used for Average True Range
STOP_ATR_MULT = 2.0    # Stop sits this many ATRs below entry
TARGET_R = 3.0         # Target sits this many R above entry (1R = entry - stop)

# --- QQQ 10am model (tenam.py) -----------------------------------------------
TENAM_SYMBOL = "QQQ"
TENAM_RISK_PER_TRADE = 250.0   # Dollars from entry to stop; shares = risk / stop distance
TENAM_STOP_BUFFER = 0.05       # Stop sits this far beyond the manipulation extreme
TENAM_FIB_LOW = 0.62           # O10 must sit between these retracements of the
TENAM_FIB_HIGH = 0.79          #   manipulation-to-extreme range
TENAM_TARGET_R = 3.0           # Take-profit is the nearer of prior-day high/low and this many R
TENAM_CANCEL_AT = "11:00"      # Unfilled entry orders are cancelled at this ET time
TENAM_FLATTEN_AT = "15:55"     # An open 10am trade is closed at this ET time

# ---------------------------------------------------------------------------
# HARD LIMITS. Set by the account owner. Only a human edits these.
# lessons.md rules can't override them, and the bot never changes them.
# A stop at entry on every trade is enforced in code and can't be turned off.
# ---------------------------------------------------------------------------
MAX_SHARES_PER_TRADE = 100      # Max shares in any one trade / held per symbol
MAX_POSITION_VALUE = 50_000.0   # Max dollar value of any one position (no margin)
MAX_DAILY_LOSS = 250.0          # Stop trading for the day once today's loss reaches this many dollars
MAX_TRADES_PER_DAY = 3          # Max new entries per day across both models
TENAM_MAX_TRADES_PER_DAY = 1    # The 10am model takes at most one trade per day

# Journal / review settings
REVIEW_EVERY = 20         # Write a lessons.md review after every N closed trades
MIN_EXAMPLES = 5          # A proposed rule needs at least this many losing trades showing the pattern
SWING_DAYS = 20           # Swing high/low window for the Fib retracement of a crossover entry
