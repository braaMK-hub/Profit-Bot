# MT5 Confluence Trading Bot

Modular, event-loop-based bot for MetaTrader 5. Analyzes H1 for the decision, H4 for
trend bias/veto, and pulls M5 for future use (not wired into logic yet — don't assume
it's doing anything until you build that).

## Setup

1. Windows only (MetaTrader5 Python package requires the MT5 terminal installed locally).
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your real login/server. Leave `DRY_RUN=True`
   until you've actually watched it run for a while.
4. (Optional) tune `config.yaml` — indicator thresholds, risk %, trading hours, divergence
   strictness. Delete it and the bot just uses the hardcoded defaults in `config.py`.
5. `python run.py`

## CLI

- `python run.py` — live/dry-run loop (default `--dashboard-mode clear`).
- `python run.py --dashboard-mode stream` — append-only dashboard, safe to also tail
  `logs/trading.log` in the same terminal.
- `python run.py --health` — one-shot connection/balance/position check, then exits.
- `python run.py --reset-cooldowns` — clears `cooldowns.json` and exits.
- `python run.py --backtest --symbol EURUSD --start 2024-01-01 --end 2024-06-01` —
  runs the historical replay and prints a performance report. Add `--csv path.csv`
  to backtest off a saved CSV instead of pulling from MT5 (needs `time, open, high,
  low, close, volume` columns).

## How the decision gets made

- Trend: ADX > 25 has to hold before anything else matters.
- BUY: price > EMA50, RSI < 60, MACD histogram rising, and H4 isn't bearish.
- SELL: price < EMA50, RSI > 40, MACD histogram falling, and H4 isn't bullish.
- RSI divergence, when found, docks a point from the score instead of blocking the trade
  outright — it's a heuristic based on the last 20 bars, not textbook divergence detection.
- OBV direction adds a point if it agrees with the trade direction.

Every decision and its reasoning gets written to `logs/trading.log`, including HOLDs,
so you can actually audit why it did or didn't do something.

## Risk

Fixed fractional sizing — `risk_per_trade` (default 2%) of account balance, sized off
ATR-based stop distance rather than fixed pips. SL = entry ± 1.5×ATR, TP = entry ± 3×ATR
(1:2 reward:risk). A symbol that gets stopped out sits in a cooldown before it's
allowed to re-enter (persisted to `cooldowns.json`, see below).

By default the bot won't open a second position on a symbol that already has one
open. Set `risk.allow_multiple_positions_per_symbol: true` in `config.yaml` to
allow pyramiding into the same symbol — `risk.max_positions` still caps the total
across all symbols combined regardless, so this can't run away unbounded.

## Divergence detection

Finds swing highs/lows over `divergence.lookback` bars (plain pivot detection, no
scipy), requires the price move to clear `divergence.tolerance` before it counts as a
real swing (filters noise), and scores the mismatch -2 to +2. With `divergence.strict:
true` in config.yaml, a strength-2 divergence against the signal direction vetoes the
trade outright instead of just docking the score. Still a heuristic — it's pivot-based
pattern matching, not a citation-worthy divergence algorithm. Don't treat a veto as
proof the setup was bad, just as one more filter. Swing mode only — see below for why
scalp mode skips it.

## Strategy modes

Set `strategy.mode` in `config.yaml` to `swing` (default), `scalp`, `divergence`,
`trendline`, `sr`, or `combo`.

**swing** — the original logic: decision on H1, EMA20/50/100/200 ribbon, RSI(14),
divergence check, H4 trend veto, ATR SL/TP at the multipliers under `risk:`.

**scalp** — a separate, faster code path (`SignalEngine.evaluate_scalp`), not the
swing logic with smaller numbers bolted on:
- Decision timeframe and trend-filter timeframe are both configurable under `scalp:`
  (`entry_timeframe` default M1, `trend_timeframe` default M15). H4 is way too slow
  to gate an M1 entry — by the time H4 confirms, the move is over — so the trend
  filter here is just a fast/slow EMA cross on the trend timeframe instead of the
  ema100 check swing mode uses.
- Fast EMA cross (`ema_fast`/`ema_slow`, default 9/21) plus a MACD histogram kick
  does the entry logic, RSI(7) is a loose mid-range filter (30-70) rather than the
  tighter swing bands — it's there to avoid entering into an already-exhausted
  spike, not to gate the whole setup.
- No divergence check. Divergence detection needs real swing structure to compare
  against; 30-40 bars on a 1-minute chart is half an hour of noise, not a swing.
- Own ATR multipliers (`atr_sl_multiplier`/`atr_tp_multiplier`, default 0.8/1.2 —
  much tighter than swing's 1.5/3, because scalp targets are small on purpose),
  own cooldown (`cooldown_minutes`, default 5), own loop interval
  (`loop_interval_seconds`, default 5).
- A spread filter (`max_spread_points`) that swing mode doesn't have. On a 5-15
  point scalp target, a 3-point spread is a huge chunk of the edge — the bot skips
  the entry entirely if the current spread exceeds this, logged as a skip, not a HOLD.

**divergence** — standalone strategy under `divergence_strategy:` (not the same
thing as swing's built-in divergence veto above — different config section, different
code path). Finds swing highs/lows on H1, compares them against RSI, MACD's raw
line, or OBV (`oscillator: RSI | MACD | OBV | any_two`), and classifies regular vs.
hidden divergence. `strength_threshold` gates weak signals out; `require_confirmation_bars`
delays the signal until price has actually started moving in the reversal direction,
not just the bar the swing formed.

**trendline** — under `trendline_strategy:`. Fits a line through the last
`min_swings_required` swing highs/lows with `numpy.polyfit`, rejects the fit if
R² < `min_r_squared` (a line forced through scattered pivots isn't a real trendline).
Signals on a touch-and-reject or a confirmed break, both measured in ATR fractions
(`touch_atr_fraction` / `break_atr_fraction`) so the thresholds scale with volatility
instead of being fixed pips.

**sr** — under `sr_strategy:`. Clusters swing highs/lows into zones within
`cluster_atr_fraction` of each other, keeps zones with at least `min_touches`,
and signals on an approach-and-reject or a confirmed break. A broken zone flips
role (resistance that breaks becomes support) and keeps acting in that new role
for `flip_persist_bars` H1 bars — tracked against the bar's own timestamp, not a
call counter, so it doesn't drift if you change `loop_interval_seconds` or if the
backtester skips a few bars while a trade is open.

**combo** — runs swing + divergence + trendline + sr on the same H1/H4 data every
cycle and only takes a signal if at least 2 of the 4 agree on the same direction
(a 2-2 split HOLDs, it isn't a tiebreaker coin flip). The log line spells out the
tally every time, e.g. `combo: XAUUSDm SELL (swing=SELL, divergence=HOLD,
trendline=SELL, sr=HOLD) -> 2/4 agree, taking signal`.

**Before you flip `strategy.mode` on live**: your current `config.yaml` has
`dry_run: false`. Test any mode change in dry-run first — `python run.py --dry-run`
forces it regardless of what's configured — before trusting it with real orders.
`trading_hours` matters more for scalp specifically than the others — scalping
through low-liquidity hours is how spread widening quietly eats the whole edge.

## Backtester

`backtester.py` replays H1 bar-by-bar (fills simulated at next bar's open, SL/TP
checked against that bar's high/low) and reports total return, Sharpe, max drawdown,
win rate, profit factor, and average trade duration. Read the docstring at the top of
`Backtester` before trusting the numbers — indicators aren't walk-forward recomputed
for swing mode, PnL uses a rough $-per-price-unit approximation, and Sharpe's
annualization factor is borrowed from daily-return convention applied to hourly bars.
Good for comparing strategy variants against each other, not a substitute for a
proper execution-accurate simulator before real money is involved.

`strategy.mode` in `config.yaml` controls which strategy gets backtested — swing
uses a fast single-pass indicator computation over the whole series; `divergence`,
`trendline`, `sr`, and `combo` replay the real `SignalEngine` evaluator bar-by-bar
on a growing raw-OHLCV slice, same code path live trading uses. That's noticeably
slower (indicators recompute every bar instead of once) — expect it to take longer
on a long date range for those four modes specifically. Scalp mode isn't backtested
at all currently; it always runs the swing `_decide()` logic regardless of what
`strategy.mode` says if set to `scalp`.

## Known limitations, don't pretend these aren't there

- Cooldowns persist to `cooldowns.json` now, but position tracking itself is still
  in-process (MT5 is the source of truth for open positions, which is fine, but don't
  expect the bot to remember mid-cycle state across a hard crash).
- Live SL hits are detected by polling MT5's deal history each loop (`deal_monitor.py`),
  filtered to this bot's magic number and `DEAL_REASON_SL` closes only — a manual close,
  a TP hit, or another EA's trade on the same account never costs a symbol its cooldown.
  Its own state (`deal_monitor_state.json`) starts fresh the first time you run this —
  it does not retroactively scan for SL hits that happened before this file existed.
- `point_value` calculation trusts MT5's `trade_tick_value`, sanity check it against
  your broker's contract specs, especially for XAUUSD where lot math gets weird.
- Health check is one-shot and standalone — it doesn't talk to a running bot process,
  it just reconnects and reports current state plus the last log line. There's no
  shared daemon state to query.
- **If you edit `cooldowns.json` or run `--reset-cooldowns` while `run.py` is already
  running, it does nothing to that running process.** `RiskManager` loads the file once
  at startup and never re-reads it. You have to restart the bot for a cleared cooldown
  to actually take effect.
- `DRY_RUN` in `.env` always overrides `runtime.dry_run` in `config.yaml` if it's set
  at all — check `.env` first if the bot seems to be running in the wrong mode.
- `add_indicators()` guards against a real `ta` library quirk: `ADXIndicator` and
  `AverageTrueRange` both throw outright (not a graceful NaN) on input shorter than
  ~2x their window, instead of degrading like every other indicator here does. Below
  that threshold both columns come back as NaN instead, which naturally resolves to
  HOLD everywhere downstream rather than crashing — this only matters if MT5 or a
  CSV ever hands back fewer than ~28 bars.
- `get_contract_size()` in `helper.py` computes a value that isn't actually wired
  into position sizing — `get_point_value()` derives everything from MT5's own
  `trade_tick_value`/`trade_tick_size`, which is already contract-size-aware at the
  broker level. `get_contract_size()` is closer to scaffolding for a future
  per-symbol override than something currently affecting live sizing math.


# MT5 Trading Bot — Command Reference

All commands assume you're in the project folder (where `run.py` lives) in
Command Prompt or PowerShell, with your venv (if you use one) activated.


**Check this before every session, it's bitten you once already:**
`.env`'s `DRY_RUN` always overrides `config.yaml`'s `runtime.dry_run`. If `.env`
says `True`, you're in dry-run no matter what the yaml says.

## Health check — always run this first when starting a session

```
python run.py --health
```

One-shot: connects to MT5, reports connection status, balance, equity, open
position count, and the last line written to `logs/trading.log`. Doesn't touch
the market, doesn't trade, just tells you if the wiring is intact before you
commit to anything longer.

## Debug the signal, no trading involved

```
python check_signals.py
```

Loops every symbol in `settings.symbols`, prints the current decision, score,
and every ADX/RSI/EMA/MACD condition it evaluated. Use this when you're
confused about *why* the bot did or didn't take a trade — it's cheaper than
digging through the log for the same info.

## Running the bot for real (or dry-run)

```
python run.py
```

Live/dry-run loop, uses whatever `.env` + `config.yaml` currently say. Runs
until you `Ctrl+C` it or it hits the daily loss limit.

```
python run.py --dry-run
```

Forces dry-run regardless of what `.env`/`config.yaml` say. Use this any time
you've changed strategy parameters and want to sanity-check behavior before
letting it near real orders again.

```
python run.py --dashboard-mode stream
```

Append-only dashboard instead of the console-clearing one — use this if you
also want to `tail` the log file in the same terminal.

**After changing `.env`, `config.yaml`, or any `.py` file: stop the running
bot (`Ctrl+C`) and start it again.** Nothing reloads live. This includes
cooldown resets — see below.

## Cooldowns

```
python run.py --reset-cooldowns
```

Clears `cooldowns.json` and exits immediately — it does not talk to a
currently-running bot process. If `run.py` is already running in another
window, this does nothing for it until you restart that process too.

Nuclear option if the state file itself looks corrupted or you just want a
completely clean slate:

```
del cooldowns.json
del deal_monitor_state.json
```

(PowerShell also accepts `Remove-Item cooldowns.json`.) Restart the bot after.

## Backtesting

```
python run.py --backtest --symbol EURUSD --start 2024-01-01 --end 2024-06-01
```

Pulls historical H1/H4 data from your connected MT5 terminal and replays it.
Needs MT5 open and logged in — it's not a separate data source.

```
python run.py --backtest --symbol EURUSD --start 2024-01-01 --end 2024-06-01 --csv path\to\data.csv
```

Same thing but off a CSV instead of live MT5 history — useful if the terminal
isn't running or you want to test against a fixed dataset. CSV needs columns
`time, open, high, low, close, volume`.

**Reminder that matters:** `strategy.mode: scalp` isn't backtested — it silently
falls back to replaying swing logic regardless. Divergence/trendline/sr/combo ARE
properly backtested now (bar-by-bar, real evaluator), but noticeably slower than
swing — don't queue up a huge date range for those four without expecting a wait.

## Watching logs live

PowerShell:

```
Get-Content logs\trading.log -Wait -Tail 20
```

Command Prompt doesn't have a real `tail -f` equivalent — use PowerShell for
this, or just open the log in a text editor and refresh it.

## Quick troubleshooting checklist

- Bot won't connect → `python run.py --health` first, check the error it prints.
- Signal doesn't match what you expect → `python check_signals.py`.
- Cooldown won't clear → did you restart the actual running process, not just
  run `--reset-cooldowns` in a second window?
- Not sure if you're live or simulated → check `.env`'s `DRY_RUN` line, not
  `config.yaml` — `.env` wins.
- Backtest numbers look too good/bad to be real → check whether you're
  actually testing the strategy you think you are (see scalp caveat above),
  and re-read the limitations section in `README.md` before trusting it.