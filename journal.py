"""Learning loop shared by both models: trade journal, post-mortems, reviews, rules.

Files (next to this script):
  trades_state.json  open and closed trades (the bots' memory between runs)
  trade_journal.md   one entry per closed trade, with a post-mortem for every loss
  lessons.md         rules (PENDING / APPROVED) + a review every REVIEW_EVERY trades

Rule approval: reviews add proposals to lessons.md as "- PENDING skip_<id>: ...".
The bots only apply a rule once you change PENDING to APPROVED. Rules can only
skip entries; hard limits live in config.py and nothing in lessons.md touches them.
"""

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime

import pandas as pd

import config

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "trades_state.json")
JOURNAL_FILE = os.path.join(BASE, "trade_journal.md")
LESSONS_FILE = os.path.join(BASE, "lessons.md")

log = logging.getLogger("tradebot")

SETUP = f"{config.SHORT_WINDOW}/{config.LONG_WINDOW} MA golden cross"
TENAM_SETUP = "QQQ 10am model"
FIB_LEVELS = (0.62, 0.65, 0.705, 0.79)
MET, BORDERLINE, NOT_MET = "MET", "BORDERLINE", "NOT MET"

# Setup-quality checks run at every entry. Each is MET or BORDERLINE on the checklist,
# and each can become an entry filter ("skip_<id>") once you approve it in lessons.md.
#   id: (checklist text, what "borderline" means, test that returns True when borderline)
CROSSOVER_CONDITIONS = {
    "weak_cross": (
        "Cross strength: MA spread at least 0.10% of price",
        "MA spread at the cross under 0.10% of price",
        lambda c: c["spread_pct"] < 0.10,
    ),
    "high_volatility": (
        "Volatility: ATR at most 3% of price",
        "ATR above 3% of price",
        lambda c: c["atr_pct"] > 3.0,
    ),
    "below_50ma": (
        "Price above the 50-day MA",
        "price below the 50-day MA",
        lambda c: c["price_vs_ma50_pct"] < 0,
    ),
    "falling_50ma": (
        "50-day MA rising over the last 10 days",
        "50-day MA falling over the last 10 days",
        lambda c: c["ma50_slope_pct"] < 0,
    ),
    "htf_not_bullish": (
        "Higher-timeframe (weekly) bias agrees with the long",
        "weekly bias not bullish",
        lambda c: c["htf_bias"] != "bullish",
    ),
    "heavy_news": (
        "News: fewer than 5 headlines in the prior 24h",
        "5+ news headlines in the 24h before entry",
        lambda c: c["news_count"] >= 5,
    ),
}


def htf_aligned(c):
    return (c["direction"] == "long" and c["htf_bias"] == "bullish") or (
        c["direction"] == "short" and c["htf_bias"] == "bearish"
    )


TENAM_CONDITIONS = {
    "tenam_htf_not_aligned": (
        "Higher-timeframe (weekly) bias agrees with the trade direction",
        "weekly bias not aligned with the trade direction",
        lambda c: not htf_aligned(c),
    ),
    "tenam_weak_wick": (
        "Sweep candle has a clear rejection wick (at least 30% of its range)",
        "weak rejection wick on the sweep candle (under 30% of its range)",
        lambda c: c["wick_ratio"] < 0.30,
    ),
    "tenam_tight_stop": (
        "Stop at least $0.40 from entry",
        "stop under $0.40 from entry",
        lambda c: c["risk_ps"] < 0.40,
    ),
    "tenam_low_rr": (
        "Planned reward at least 2R",
        "target under 2R (prior-day level close to entry)",
        lambda c: c["planned_rr"] < 2.0,
    ),
    "tenam_late": (
        "Order placed by 10:40 ET",
        "order placed after 10:40 ET",
        lambda c: c["placed_minute"] > 10 * 60 + 40,
    ),
    "tenam_heavy_news": (
        "News: fewer than 5 QQQ headlines in the prior 24h",
        "5+ QQQ headlines in the 24h before entry",
        lambda c: c["news_count"] >= 5,
    ),
}

ALL_CONDITIONS = {**CROSSOVER_CONDITIONS, **TENAM_CONDITIONS}

LESSONS_TEMPLATE = """# Lessons

## Rules
<!--
The bots read this section at the start of every run and before every entry.
Only rules marked APPROVED are active. Reviews add proposals as PENDING:
  change "- PENDING skip_..." to "- APPROVED skip_..." to activate a rule,
  or to "- REJECTED skip_..." to dismiss it (it won't be proposed again).
Rules can only skip entries. Hard limits (max shares/value per trade, max daily loss,
stop on every trade, max trades per day) live in config.py and nothing here changes them.
-->
_No rules proposed yet._

## Reviews
"""


# --------------------------------------------------------------------------- state

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"open": {}, "closed": [], "last_review_at": 0, "entries_by_day": {}}
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("entries_by_day", {})
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- context

def atr(df, period):
    prev_close = df["Close"].shift(1)
    true_range = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return float(true_range.rolling(period).mean().iloc[-1])


def session_label(ts):
    minutes = ts.hour * 60 + ts.minute
    if minutes < 9 * 60 + 30:
        return "pre-market"
    if minutes < 12 * 60:
        return "NY AM"
    if minutes < 16 * 60:
        return "NY PM"
    return "after-hours"


def fib_label(retrace):
    if retrace < FIB_LEVELS[0]:
        return "above 0.62 (shallow)"
    if retrace > FIB_LEVELS[-1]:
        return "below 0.79 (deep)"
    return f"{min(FIB_LEVELS, key=lambda lvl: abs(lvl - retrace)):g}"


def fib_location(df, price):
    """Where the entry sits as a retracement of the last SWING_DAYS swing (0 = swing high, 1 = swing low)."""
    swing = df.tail(config.SWING_DAYS)
    high, low = float(swing["High"].max()), float(swing["Low"].min())
    retrace = (high - price) / (high - low) if high > low else 0.0
    return {
        "swing_high": round(high, 2), "swing_low": round(low, 2), "fib_retrace": round(retrace, 3),
        "fib_level": fib_label(retrace), "in_ote": FIB_LEVELS[0] <= retrace <= FIB_LEVELS[-1],
    }


def wick_strength(ratio):
    return "strong" if ratio >= 0.5 else "moderate" if ratio >= 0.3 else "weak" if ratio >= 0.1 else "none"


def rejection_wick(bar):
    """Lower wick of the signal-day candle, as a share of its range."""
    rng = float(bar["High"] - bar["Low"])
    ratio = float(min(bar["Open"], bar["Close"]) - bar["Low"]) / rng if rng > 0 else 0.0
    return {"wick_ratio": round(ratio, 2), "wick_strength": wick_strength(ratio)}


def liquidity_taken(df):
    """Did any of the last 5 days trade below the 20-day low that came before them?"""
    recent, prior = df.tail(5), df.iloc[-25:-5]
    prior_low = float(prior["Low"].min())
    swept = recent[recent["Low"] < prior_low]
    if swept.empty:
        return "none (prior 20-day low untouched)"
    return f"took the prior 20-day low ({prior_low:.2f}) on {swept.index[-1].date()}"


def weekly_bias(df):
    weekly = df["Close"].resample("W-FRI").last().dropna()
    ma = weekly.rolling(10).mean()
    if len(weekly) < 13 or pd.isna(ma.iloc[-3]):
        return "neutral"
    if weekly.iloc[-1] > ma.iloc[-1] and ma.iloc[-1] > ma.iloc[-3]:
        return "bullish"
    if weekly.iloc[-1] < ma.iloc[-1] and ma.iloc[-1] < ma.iloc[-3]:
        return "bearish"
    return "neutral"


def daily_trend(df, price):
    """Trend and volatility from completed daily bars, shared by both models."""
    long_ma = df["Close"].rolling(config.LONG_WINDOW).mean()
    ma50 = float(long_ma.iloc[-1])
    ma50_slope = (ma50 / float(long_ma.iloc[-11]) - 1) * 100
    price_vs_ma50 = (price / ma50 - 1) * 100
    atr_value = atr(df, config.ATR_PERIOD)
    atr_pct = atr_value / price * 100
    if price_vs_ma50 > 0 and ma50_slope > 0.25:
        trend = "uptrend"
    elif price_vs_ma50 < 0 and ma50_slope < -0.25:
        trend = "downtrend"
    else:
        trend = "sideways"
    return {
        "trend": trend,
        "volatility": "low" if atr_pct < 1.5 else "high" if atr_pct > 3.0 else "normal",
        "atr": round(atr_value, 2),
        "atr_pct": round(atr_pct, 2),
        "price_vs_ma50_pct": round(price_vs_ma50, 2),
        "ma50_slope_pct": round(ma50_slope, 2),
        "htf_bias": weekly_bias(df),
    }


def build_context(df, now_et, headlines, price):
    """Crossover context from completed daily bars (Open/High/Low/Close) at decision time."""
    short_ma = df["Close"].rolling(config.SHORT_WINDOW).mean()
    long_ma = df["Close"].rolling(config.LONG_WINDOW).mean()
    return {
        "session": session_label(now_et),
        "direction": "long",
        "price": round(price, 2),
        "spread_pct": round(abs(float(short_ma.iloc[-1]) - float(long_ma.iloc[-1])) / price * 100, 3),
        "liquidity": liquidity_taken(df),
        "news_count": len(headlines),
        "headlines": headlines[:3],
        **daily_trend(df, price),
        **fib_location(df, price),
        **rejection_wick(df.iloc[-1]),
    }


def checklist(context, gates, conditions=CROSSOVER_CONDITIONS):
    """gates: [[text, MET/NOT MET], ...] from the bot (signal, market open, hard limits).
    Items are [text, status, rule_id]; rule_id ties a BORDERLINE item to its filter rule."""
    items = [[g[0], g[1], None] for g in gates]
    for rule_id, (text, _, test) in conditions.items():
        items.append([text, BORDERLINE if test(context) else MET, rule_id])
    return items


def _items(trade):
    return [(i[0], i[1], i[2] if len(i) > 2 else None) for i in trade["checklist"]]


def grade(items):
    broken = [i[0] for i in items if i[1] == NOT_MET]
    weak = [i[0] for i in items if i[1] == BORDERLINE]
    if broken:
        return "C", "rule broken: " + "; ".join(broken)
    if len(weak) >= 2:
        return "C", f"{len(weak)} borderline: " + "; ".join(weak)
    if weak:
        return "B", "one borderline: " + weak[0]
    return "A", "all rules met"


# --------------------------------------------------------------------------- rules

def ensure_lessons_file():
    if not os.path.exists(LESSONS_FILE):
        with open(LESSONS_FILE, "w", encoding="utf-8") as f:
            f.write(LESSONS_TEMPLATE)


def _rules_by_status():
    ensure_lessons_file()
    with open(LESSONS_FILE, encoding="utf-8") as f:
        text = f.read()
    rules = defaultdict(list)
    for status, rule_id in re.findall(r"^\s*-\s*(APPROVED|PENDING|REJECTED)\s+([\w-]+)", text, re.MULTILINE):
        rules[status].append(rule_id)
    return rules, text


def approved_rules():
    """Condition ids marked APPROVED in lessons.md. Unknown ids are ignored, never guessed at."""
    approved = set()
    for rule_id in _rules_by_status()[0]["APPROVED"]:
        cond = rule_id.removeprefix("skip_")
        if cond in ALL_CONDITIONS:
            approved.add(cond)
        else:
            log.warning(f"lessons.md: ignoring unknown rule '{rule_id}'")
    return approved


def pending_rules():
    return _rules_by_status()[0]["PENDING"]


def blocking_rules(context, approved, conditions=CROSSOVER_CONDITIONS):
    return [f"skip_{cid}" for cid in sorted(approved) if cid in conditions and conditions[cid][2](context)]


def session_briefing(state):
    """Start of session: what lessons.md and the last 20 journal entries say before trading."""
    approved, pending = sorted(approved_rules()), pending_rules()
    log.info(
        "Active rules (APPROVED in lessons.md): "
        + (", ".join(f"skip_{c} ({ALL_CONDITIONS[c][1]})" for c in approved) if approved else "none")
        + (f" | {len(pending)} PENDING awaiting your approval: {', '.join(pending)}" if pending else "")
    )
    recent = state["closed"][-20:]
    if not recent:
        log.info("Journal: no closed trades yet.")
        return
    wins = sum(1 for t in recent if t["pnl"] > 0)
    grades = Counter(t["grade"] for t in recent)
    classes = Counter(t["loss_class"] for t in recent if t.get("loss_class"))
    log.info(
        f"Last {len(recent)} trades: win rate {wins / len(recent):.0%}, "
        f"avg R {sum(t['r'] for t in recent) / len(recent):+.2f}, "
        f"grades {dict(sorted(grades.items()))}, losses by class {dict(sorted(classes.items())) or '{}'}"
    )


# --------------------------------------------------------------------------- closing trades

def to_et(ts):
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("America/New_York")


def fmt_time(iso):
    return to_et(iso).strftime("%Y-%m-%d %H:%M ET")


def record_close(state, symbol, exit_price, exit_time, exit_reason):
    trade = state["open"].pop(symbol)
    sign = 1 if trade["direction"] == "long" else -1
    risk = abs(trade["entry"] - trade["stop"])
    points = (float(exit_price) - trade["entry"]) * sign
    trade.update(
        exit=round(float(exit_price), 2),
        exit_time=to_et(exit_time).isoformat(),
        exit_reason=exit_reason,
        points=round(points, 2),
        pnl=round(points * trade["qty"], 2),
        r=round(points / risk, 2) if risk > 0 else 0.0,
    )
    if trade["pnl"] < 0:
        trade["loss_class"], trade["post_mortem"] = post_mortem(trade)
    state["closed"].append(trade)
    save_state(state)
    write_journal_entry(trade, len(state["closed"]))
    log.info(
        f"{symbol}: trade closed ({exit_reason}) at {trade['exit']:.2f}, "
        f"P&L {trade['pnl']:+.2f} ({trade['r']:+.2f}R, grade {trade['grade']}). Journal updated."
    )
    maybe_review(state)
    return trade


def post_mortem(trade):
    """(a) rule broken, (b) weak/borderline setup, (c) valid setup, normal loss."""
    items = _items(trade)
    broken = [text for text, status, _ in items if status == NOT_MET]
    weak = [rid for _, status, rid in items if status == BORDERLINE and rid]
    rule_filters = [f"`skip_{rid}` ({ALL_CONDITIONS[rid][1]})" for rid in weak if rid in ALL_CONDITIONS]
    if broken:
        filters = [f"enforce \"{t}\" before entering" for t in broken] + rule_filters
        return "a", "**(a) Rule broken:** " + "; ".join(broken) + ". Filter that would have kept me out: " + ", ".join(filters) + "."
    if rule_filters:
        return "b", "**(b) Weak/borderline setup.** Filter that would have kept me out: " + ", ".join(rule_filters) + "."
    return "c", "**(c) Valid setup, normal loss.** No change needed."


def _crossover_setup_lines(trade, ctx):
    return [
        f"**Setup:** {trade['setup']} (MA spread {ctx['spread_pct']:.3f}% of price). "
        f"Stop {config.STOP_ATR_MULT:g}×ATR ({ctx['atr']:.2f}), target {config.TARGET_R:g}R.",
        f"- Fib level of entry: {ctx['fib_level']} (retracement {ctx['fib_retrace']:.3f} of the "
        f"{config.SWING_DAYS}-day swing {ctx['swing_low']:.2f}–{ctx['swing_high']:.2f}); "
        f"OTE zone: {'yes' if ctx['in_ote'] else 'no'}",
        f"- Rejection wick: {ctx['wick_strength']} (lower wick {ctx['wick_ratio']:.0%} of the signal-day range)",
        f"- Liquidity before entry: {ctx['liquidity']}",
    ]


def _tenam_setup_lines(trade, ctx):
    side = "below" if trade["direction"] == "long" else "above"
    back = "above" if trade["direction"] == "long" else "below"
    return [
        f"**Setup:** {trade['setup']}: wick {side} the {ctx['swept_level']} ({ctx['swept_price']:.2f}) at "
        f"{ctx['sweep_time']}, then a 1-min close back {back} O10 ({ctx['o10']:.2f}) at {ctx['reclaim_time']}. "
        f"Limit at O10, stop {config.TENAM_STOP_BUFFER:.2f} beyond the manipulation extreme, "
        f"target = {ctx['tp_basis']} (planned {ctx['planned_rr']:.2f}R). Order placed {ctx['placed_at']}.",
        f"- Fib level of entry: {ctx['fib_level']} (O10 at a {ctx['fib_retrace']:.3f} retracement of the "
        f"manipulation {ctx['manip']:.2f} → extreme {ctx['extreme']:.2f} range); OTE zone: {'yes' if ctx['in_ote'] else 'no'}",
        f"- Rejection wick: {ctx['wick_strength']} ({ctx['wick_ratio']:.0%} of the sweep candle's range)",
        f"- Liquidity before entry: {ctx['liquidity']}",
    ]


def write_journal_entry(trade, number):
    ctx = trade["context"]
    lines = []
    if not os.path.exists(JOURNAL_FILE):
        lines += [
            "# Trade Journal",
            "",
            "Grades: A = all rules met, B = one borderline, C = a rule broken (or 2+ borderline). "
            "Crossover trades measure Fib, wick and liquidity on daily candles; 10am trades on 1-minute candles.",
            "",
        ]
    result = "WIN" if trade["pnl"] > 0 else "LOSS" if trade["pnl"] < 0 else "BREAKEVEN"
    agrees = "agrees" if htf_aligned({**ctx, "direction": trade["direction"]}) else (
        "neutral" if ctx["htf_bias"] == "neutral" else "goes against")
    setup = _tenam_setup_lines(trade, ctx) if trade.get("model") == "10am" else _crossover_setup_lines(trade, ctx)
    lines += [
        f"## #{number} · {fmt_time(trade['entry_time'])[:10]} · {trade['symbol']} · {trade['direction'].upper()} · "
        f"{result} {trade['r']:+.2f}R · Grade {trade['grade']}",
        "",
        f"**Opened:** {fmt_time(trade['entry_time'])} ({ctx['session']})  ",
        f"**Closed:** {fmt_time(trade['exit_time'])} ({trade['exit_reason']})",
        "",
        "| Entry | Stop | Target | Exit | Points | R | P&L |",
        "|---|---|---|---|---|---|---|",
        f"| {trade['entry']:.2f} | {trade['stop']:.2f} | {trade['target']:.2f} | {trade['exit']:.2f} "
        f"| {trade['points']:+.2f} | {trade['r']:+.2f}R | {trade['pnl']:+.2f} ({trade['qty']:g} sh) |",
        "",
        *setup,
        "",
        f"**Higher-timeframe bias:** {ctx['htf_bias']} (weekly). The {trade['direction']} {agrees} with it.",
        "",
        f"**Market context:** trend {ctx['trend']} (price {ctx['price_vs_ma50_pct']:+.2f}% vs 50-day MA, "
        f"50-day MA {ctx['ma50_slope_pct']:+.2f}% over 10 days) · volatility {ctx['volatility']} "
        f"(daily ATR {ctx['atr_pct']:.2f}% of price) · news: {ctx['news_count']} headline(s) in the prior 24h",
    ]
    lines += [f"  - {h}" for h in ctx["headlines"]]
    lines += ["", "**Checklist:**"]
    lines += [f"- {status}: {text}" for text, status, _ in _items(trade)]
    lines += ["", f"**Grade:** {trade['grade']} ({trade['grade_reason']})"]
    if trade.get("post_mortem"):
        lines += ["", f"**Post-mortem:** {trade['post_mortem']}"]
    lines += ["", ""]
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


# --------------------------------------------------------------------------- reviews

def stats_table(trades, key, title):
    groups = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    rows = [f"**By {title}**", "", f"| {title.capitalize()} | Trades | Win rate | Avg R | P&L |", "|---|---|---|---|---|"]
    for name, group in sorted(groups.items()):
        wins = sum(1 for t in group if t["pnl"] > 0)
        rows.append(
            f"| {name} | {len(group)} | {wins / len(group):.0%} | "
            f"{sum(t['r'] for t in group) / len(group):+.2f}R | {sum(t['pnl'] for t in group):+.2f} |"
        )
    return rows + [""]


def maybe_review(state):
    total = len(state["closed"])
    if total - state.get("last_review_at", 0) < config.REVIEW_EVERY:
        return
    write_review(state["closed"])
    state["last_review_at"] = total - total % config.REVIEW_EVERY
    save_state(state)


def _has_pattern(trade, rule_id):
    return any(rid == rule_id and status == BORDERLINE for _, status, rid in _items(trade))


def write_review(closed):
    ensure_lessons_file()
    numbered = list(enumerate(closed, start=1))
    wins = sum(1 for t in closed if t["pnl"] > 0)
    out = [
        f"\n### Review after {len(closed)} trades ({datetime.now():%Y-%m-%d})",
        "",
        f"Overall: {len(closed)} trades, win rate {wins / len(closed):.0%}, "
        f"avg R {sum(t['r'] for t in closed) / len(closed):+.2f}, P&L {sum(t['pnl'] for t in closed):+.2f}. "
        f"Losses by class: {dict(sorted(Counter(t['loss_class'] for t in closed if t.get('loss_class')).items()))}.",
        "",
    ]
    out += stats_table(closed, lambda t: t["setup"], "setup")
    out += stats_table(closed, lambda t: t["context"].get("fib_level", "n/a"), "Fib level")
    out += stats_table(closed, lambda t: t["context"]["session"], "session")
    out += stats_table(closed, lambda t: t["grade"], "grade")
    out += stats_table(closed, lambda t: f"{t['symbol']} {t['direction']}", "symbol and direction")

    out += [
        "**Patterns in losers vs winners**",
        "",
        "| Pattern | Losers with it | Winners with it | Avg R with | Avg R without |",
        "|---|---|---|---|---|",
    ]
    proposals = []
    for cid, (_, desc, _) in ALL_CONDITIONS.items():
        with_it = [(n, t) for n, t in numbered if _has_pattern(t, cid)]
        if not with_it:
            continue
        without = [t for _, t in numbered if not _has_pattern(t, cid)]
        losers = [(n, t) for n, t in with_it if t["pnl"] < 0]
        winners = len(with_it) - len(losers)
        avg_with = sum(t["r"] for _, t in with_it) / len(with_it)
        avg_without = sum(t["r"] for t in without) / len(without) if without else 0.0
        out.append(f"| {desc} | {len(losers)} | {winners} | {avg_with:+.2f}R | {avg_without:+.2f}R |")
        # 5+ losers and rare in winners (no more than a third as many winners as losers).
        if len(losers) >= config.MIN_EXAMPLES and winners * 3 <= len(losers):
            proposals.append((cid, desc, losers, winners, avg_with, avg_without))
    out.append("")

    rules, text = _rules_by_status()
    already = {r for status in rules.values() for r in status}
    out += ["**Proposed rules** (added to Rules as PENDING; only APPROVED rules are active)", ""]
    if not proposals:
        out.append(f"None. No pattern shows up in {config.MIN_EXAMPLES}+ losers while staying rare in winners.")
    for cid, desc, losers, winners, avg_with, avg_without in proposals:
        dates = ", ".join(f"{fmt_time(t['entry_time'])[:10]} {t['symbol']} (#{n}, {t['r']:+.2f}R)" for n, t in losers)
        status = "" if f"skip_{cid}" not in already else " (already listed under Rules)"
        out.append(
            f"- `skip_{cid}`: skip entries when {desc}. In {len(losers)} losers vs {winners} winners; "
            f"avg R {avg_with:+.2f} with it vs {avg_without:+.2f} without. Supporting trades: {dates}{status}"
        )
        if f"skip_{cid}" in already:
            continue  # keep whatever you decided
        line = (f"- PENDING skip_{cid}: skip entries when {desc} "
                f"(proposed {datetime.now():%Y-%m-%d}; {len(losers)} losers, {winners} winners; see review below)")
        if "_No rules proposed yet._" in text:
            text = text.replace("_No rules proposed yet._", line, 1)
        else:
            text = text.replace("\n## Reviews", f"{line}\n\n## Reviews", 1)
    out.append("")

    text = text.rstrip("\n") + "\n" + "\n".join(out) + "\n"
    with open(LESSONS_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    log.info(f"Wrote review of {len(closed)} trades to lessons.md ({len(proposals)} proposed rule(s)).")
