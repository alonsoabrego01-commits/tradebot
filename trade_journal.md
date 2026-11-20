# Trade Journal

Grades: A = all rules met, B = one borderline, C = a rule broken (or 2+ borderline). Crossover trades are read on daily candles; intraday trades (inverse fair value gap, liquidity sweep) on 5-minute candles.

## #1 · 2026-11-17 · SPY · LONG · LOSS -1.11R · Grade C

**Opened:** 2026-11-17 09:40 ET (NY AM)  
**Closed:** 2026-11-17 10:05 ET (Stop hit)

| Entry | Stop | First step | Avg exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 738.01 | 737.44 | 740.77 | 737.38 | -0.63 | -1.11R | -20.84 (33 sh) |

- Exit: 2 sh at 737.35 (10:05, Stop)
- Exit: 1 sh at 737.34 (10:05, Stop)
- Exit: 17 sh at 737.39 (10:05, Stop)
- Exit: 8 sh at 737.38 (10:05, Stop)
- Exit: 4 sh at 737.35 (10:05, Stop)
- Exit: 1 sh at 737.38 (10:05, Stop)

**Setup:** Liquidity sweep and reversal: price traded below the prior-day low (738.07) at 09:35 (extreme 737.49), then a 5-min close 738.12 back above it at 09:40. Market entry, stop behind the sweep extreme, scaling out every 2.66 (first step 3.97R). Order placed 09:40 ET.
- Reclaim: 1 bar(s) after the sweep
- Levels: premarket 735.7–741.73, prior day 738.07–749.82
- Stop distance: 0.67 per share (0.09% of price)

**Higher-timeframe bias:** bearish (weekly). The long goes against with it.

**Market context:** trend downtrend (price -3.33% vs 50-day MA, 50-day MA -0.39% over 10 days) · volatility normal (daily ATR 1.83% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Level in play at the open: prior-day low 738.07
- MET: Sweep: a 5-min bar traded below it at 09:35 (extreme 737.49)
- MET: Reclaim: 5-min close 738.12 back above it within 3 bars (09:40)
- MET: Entry signal between 09:35 and 11:30 ET
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (33 sh, $24,358); no margin
- MET: Hard limit: daily loss under $250 (today +0.00)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- BORDERLINE: Higher-timeframe (weekly) bias agrees with the trade direction
- BORDERLINE: Level reclaimed on the sweep bar itself
- MET: Stop at least 0.08% of price from entry
- MET: Entry signal by 10:45 ET

**Grade:** C (2 borderline: Higher-timeframe (weekly) bias agrees with the trade direction; Level reclaimed on the sweep bar itself)

**Post-mortem:** **(b) Weak/borderline setup.** Filter that would have kept me out: `skip_sweep_htf_not_aligned` (weekly bias not aligned with the trade direction), `skip_sweep_slow_reclaim` (level reclaimed one or two bars after the sweep, not on the sweep bar).

## #2 · 2026-11-17 · QQQ · SHORT · WIN +0.46R · Grade A

**Opened:** 2026-11-17 11:25 ET (NY AM)  
**Closed:** 2026-11-17 12:05 ET (Breakeven stop after step 1)

| Entry | Stop | First step | Avg exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 653.45 | 656.54 | 651.04 | 652.01 | +1.44 | +0.46R | +54.60 (38 sh) |

- Exit: 19 sh at 650.99 (11:45, Step 1)
- Exit: 10 sh at 653.09 (12:05, Stop)
- Exit: 5 sh at 652.93 (12:05, Stop)
- Exit: 2 sh at 653.01 (12:05, Stop)
- Exit: 1 sh at 653.03 (12:05, Stop)
- Exit: 1 sh at 653.09 (12:05, Stop)

**Setup:** Inverse fair value gap with the order flow: order flow bearish (last break of structure), then a 5-min close 653.21 below the bullish fair value gap 653.39–653.62 (formed 11:10) at 11:25. Limit at the retest of the gap (653.39), stop beyond the leg's extreme (656.49), scaling out every 2.35 (first step 0.75R). Order placed 11:25 ET.
- Gap age when inverted: 3 five-minute bar(s)
- Stop distance: 3.15 per share (0.48% of price)

**Higher-timeframe bias:** bearish (weekly). The short agrees with it.

**Market context:** trend downtrend (price -8.82% vs 50-day MA, 50-day MA -1.03% over 10 days) · volatility normal (daily ATR 2.94% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Order flow bearish: the last break of structure was down
- MET: Bullish fair value gap 653.39-653.62 formed at 11:10, within 12 bars
- MET: Inverted: 5-min close 653.21 below it at 11:25
- MET: Setup completed between 10:00 and 12:00 ET
- MET: Limit entry at the retest of the gap (653.39), good for 45 minutes
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (38 sh, $24,829); no margin
- MET: Hard limit: daily loss under $250 (today -20.94)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- MET: Higher-timeframe (weekly) bias agrees with the trade direction
- MET: Gap formed within the last 30 minutes
- MET: Stop at least 0.08% of price from entry
- MET: Stop at most 0.6% of price from entry

**Grade:** A (all rules met)

## #3 · 2026-11-18 · QQQ · SHORT · WIN +0.69R · Grade C

**Opened:** 2026-11-18 10:59 ET (NY AM)  
**Closed:** 2026-11-18 11:24 ET (Trailing stop after step 2)

| Entry | Stop | First step | Avg exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 658.50 | 662.85 | 656.13 | 655.50 | +3.00 | +0.69R | +83.86 (28 sh) |

- Exit: 14 sh at 656.12 (11:13, Step 1)
- Exit: 7 sh at 653.64 (11:20, Step 2)
- Exit: 4 sh at 656.13 (11:24, Stop)
- Exit: 2 sh at 656.14 (11:24, Stop)
- Exit: 1 sh at 656.18 (11:24, Stop)

**Setup:** Inverse fair value gap with the order flow: order flow bearish (last break of structure), then a 5-min close 657.04 below the bullish fair value gap 658.50–659.49 (formed 10:05) at 10:40. Limit at the retest of the gap (658.50), stop beyond the leg's extreme (662.80), scaling out every 2.37 (first step 0.54R). Order placed 10:40 ET.
- Gap age when inverted: 7 five-minute bar(s)
- Stop distance: 4.35 per share (0.66% of price)

**Higher-timeframe bias:** bearish (weekly). The short agrees with it.

**Market context:** trend downtrend (price -7.95% vs 50-day MA, 50-day MA -1.15% over 10 days) · volatility high (daily ATR 3.04% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Order flow bearish: the last break of structure was down
- MET: Bullish fair value gap 658.50-659.49 formed at 10:05, within 12 bars
- MET: Inverted: 5-min close 657.04 below it at 10:40
- MET: Setup completed between 10:00 and 12:00 ET
- MET: Limit entry at the retest of the gap (658.50), good for 45 minutes
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (28 sh, $18,438); no margin
- MET: Hard limit: daily loss under $250 (today +0.00)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- MET: Higher-timeframe (weekly) bias agrees with the trade direction
- BORDERLINE: Gap formed within the last 30 minutes
- MET: Stop at least 0.08% of price from entry
- BORDERLINE: Stop at most 0.6% of price from entry

**Grade:** C (2 borderline: Gap formed within the last 30 minutes; Stop at most 0.6% of price from entry)

## #4 · 2026-11-18 · SPY · LONG · WIN +1.27R · Grade C

**Opened:** 2026-11-18 11:25 ET (NY AM)  
**Closed:** 2026-11-18 12:26 ET (Closed outside the bot after step 1)

| Entry | Stop | First step | Avg exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 729.40 | 726.85 | 732.25 | 732.64 | +3.25 | +1.27R | +110.33 (34 sh) |

- Exit: 17 sh at 732.29 (12:23, Step 1)
- Exit: 17 sh at 733.00 (12:26, Closed outside the bot)

**Setup:** Liquidity sweep and reversal: price traded below the prior-day low (727.00) at 11:25 (extreme 726.90), then a 5-min close 729.83 back above it at 11:25. Market entry, stop behind the sweep extreme, scaling out every 2.63 (first step 0.95R). Order placed 11:25 ET.
- Reclaim: on the sweep bar
- Levels: premarket 732.54–737.39, prior day 727.0–739.76
- Stop distance: 2.77 per share (0.38% of price)

**Higher-timeframe bias:** bearish (weekly). The long goes against with it.

**Market context:** trend downtrend (price -4.36% vs 50-day MA, 50-day MA -0.46% over 10 days) · volatility normal (daily ATR 1.94% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Level in play at the open: prior-day low 727.00
- MET: Sweep: a 5-min bar traded below it at 11:25 (extreme 726.90)
- MET: Reclaim: 5-min close 729.83 back above it within 3 bars (11:25)
- MET: Entry signal between 09:35 and 11:30 ET
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (34 sh, $24,807); no margin
- MET: Hard limit: daily loss under $250 (today +83.97)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- BORDERLINE: Higher-timeframe (weekly) bias agrees with the trade direction
- MET: Level reclaimed on the sweep bar itself
- MET: Stop at least 0.08% of price from entry
- BORDERLINE: Entry signal by 10:45 ET

**Grade:** C (2 borderline: Higher-timeframe (weekly) bias agrees with the trade direction; Entry signal by 10:45 ET)

## #5 · 2026-11-19 · QQQ · LONG · LOSS -0.95R · Grade B

**Opened:** 2026-11-19 10:06 ET (NY AM)  
**Closed:** 2026-11-19 10:12 ET (Stop hit)

| Entry | Stop | First step | Avg exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 672.15 | 670.08 | 674.60 | 670.18 | -1.97 | -0.95R | -73.03 (37 sh) |

- Exit: 19 sh at 670.17 (10:12, Stop)
- Exit: 9 sh at 670.21 (10:12, Stop)
- Exit: 5 sh at 670.16 (10:12, Stop)
- Exit: 2 sh at 670.14 (10:12, Stop)
- Exit: 1 sh at 670.16 (10:12, Stop)
- Exit: 1 sh at 670.16 (10:12, Stop)

**Setup:** Inverse fair value gap with the order flow: order flow bullish (last break of structure), then a 5-min close 673.09 above the bearish fair value gap 671.81–672.18 (formed 10:00) at 10:05. Limit at the retest of the gap (672.18), stop beyond the leg's extreme (670.13), scaling out every 2.42 (first step 1.15R). Order placed 10:05 ET.
- Gap age when inverted: 1 five-minute bar(s)
- Stop distance: 2.10 per share (0.31% of price)

**Higher-timeframe bias:** bearish (weekly). The long goes against with it.

**Market context:** trend downtrend (price -5.89% vs 50-day MA, 50-day MA -1.25% over 10 days) · volatility normal (daily ATR 2.93% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Order flow bullish: the last break of structure was up
- MET: Bearish fair value gap 671.81-672.18 formed at 10:00, within 12 bars
- MET: Inverted: 5-min close 673.09 above it at 10:05
- MET: Setup completed between 10:00 and 12:00 ET
- MET: Limit entry at the retest of the gap (672.18), good for 45 minutes
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (37 sh, $24,871); no margin
- MET: Hard limit: daily loss under $250 (today +0.00)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- BORDERLINE: Higher-timeframe (weekly) bias agrees with the trade direction
- MET: Gap formed within the last 30 minutes
- MET: Stop at least 0.08% of price from entry
- MET: Stop at most 0.6% of price from entry

**Grade:** B (one borderline: Higher-timeframe (weekly) bias agrees with the trade direction)

**Post-mortem:** **(b) Weak/borderline setup.** Filter that would have kept me out: `skip_ifvg_htf_not_aligned` (weekly bias not aligned with the trade direction).

## #6 · 2026-11-19 · SPY · LONG · WIN +3.35R · Grade B

**Opened:** 2026-11-19 10:40 ET (NY AM)  
**Closed:** 2026-11-19 11:40 ET (Target hit)

| Entry | Stop | Target | Exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 726.96 | 725.62 | 731.34 | 731.45 | +4.49 | +3.35R | +152.66 (34 sh) |

- Exit: 34 sh at 731.45 (11:40, Step 1)

**Setup:** Liquidity sweep and reversal: price traded below the prior-day low (726.79) at 10:40 (extreme 725.67), then a 5-min close 726.89 back above it at 10:40. Market entry, stop behind the sweep extreme, fixed target 3R away (4.29 per share). Order placed 10:40 ET.
- Reclaim: on the sweep bar
- Levels: premarket 734.49–740.25, prior day 726.79–735.06
- Stop distance: 1.43 per share (0.20% of price)

**Higher-timeframe bias:** bearish (weekly). The long goes against with it.

**Market context:** trend downtrend (price -4.62% vs 50-day MA, 50-day MA -0.54% over 10 days) · volatility normal (daily ATR 1.97% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Level in play at the open: prior-day low 726.79
- MET: Sweep: a 5-min bar traded below it at 10:40 (extreme 725.67)
- MET: Reclaim: 5-min close 726.89 back above it within 3 bars (10:40)
- MET: Entry signal between 09:35 and 11:30 ET
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (34 sh, $24,720); no margin
- MET: Hard limit: daily loss under $250 (today -72.91)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- BORDERLINE: Higher-timeframe (weekly) bias agrees with the trade direction
- MET: Level reclaimed on the sweep bar itself
- MET: Stop at least 0.08% of price from entry
- MET: Entry signal by 10:45 ET

**Grade:** B (one borderline: Higher-timeframe (weekly) bias agrees with the trade direction)

## #7 · 2026-11-20 · SPY · SHORT · LOSS -0.99R · Grade A

**Opened:** 2026-11-20 09:50 ET (NY AM)  
**Closed:** 2026-11-20 09:54 ET (Stop hit)

| Entry | Stop | Target | Exit | Points | R | P&L |
|---|---|---|---|---|---|---|
| 712.89 | 715.05 | 707.17 | 715.02 | -2.13 | -0.99R | -74.55 (35 sh) |

- Exit: 35 sh at 715.02 (09:54, Stop)

**Setup:** Liquidity sweep and reversal: price traded above the premarket high (714.96) at 09:50 (extreme 715.00), then a 5-min close 713.78 back below it at 09:50. Market entry, stop behind the sweep extreme, fixed target 3R away (5.91 per share). Order placed 09:50 ET.
- Reclaim: on the sweep bar
- Levels: premarket 692.15–714.96, prior day 714.92–739.9
- Stop distance: 1.97 per share (0.28% of price)

**Higher-timeframe bias:** bearish (weekly). The short agrees with it.

**Market context:** trend downtrend (price -6.36% vs 50-day MA, 50-day MA -0.57% over 10 days) · volatility normal (daily ATR 2.06% of price) · news: 0 headline(s) in the prior 24h

**Checklist:**
- MET: Level in play at the open: premarket high 714.96
- MET: Sweep: a 5-min bar traded above it at 09:50 (extreme 715.00)
- MET: Reclaim: 5-min close 713.78 back below it within 3 bars (09:50)
- MET: Entry signal between 09:35 and 11:30 ET
- MET: One trade per day for this model
- MET: Hard limit: max 100 shares and $50,000 per position (35 sh, $24,958); no margin
- MET: Hard limit: daily loss under $250 (today +0.00)
- MET: Hard limit: max 3 trades per day
- MET: Hard limit: stop at entry (bracket order on every lot)
- MET: No APPROVED lessons.md rule blocks the entry
- MET: Higher-timeframe (weekly) bias agrees with the trade direction
- MET: Level reclaimed on the sweep bar itself
- MET: Stop at least 0.08% of price from entry
- MET: Entry signal by 10:45 ET

**Grade:** A (all rules met)

**Post-mortem:** **(c) Valid setup, normal loss.** No change needed.

