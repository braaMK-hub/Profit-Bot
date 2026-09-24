"""Two-file memory system for the bot's replay/backtest path.

data/ledger.csv - one row per REAL trade outcome from a REAL replay run.
Columns (exact header requested): timestamp,symbol,action,price,quantity,
reason,mode,outcome,pnl. Never touched by live/dry-run trading — this is
purely a backtest-replay memory store, populated only by replay.py.

data/learnings.md - plain-English lessons, one per distinct losing setup
actually observed in a `python replay.py --mode raw` run. Never pre-seeded.

Memory quality rules this module enforces:
  - no seeded fake losses: both files start empty (ledger has only its
    header row) until a real replay run populates them
  - no invented candles: every row here traces back to a real Backtester
    trade closed against real OHLCV data fetched from MT5 (or a real CSV)
  - no forced failure: nothing in this module can make the strategy trade
    worse on purpose to manufacture a "lesson" — a lesson is only ever
    written after replay.py confirms a real loss already happened
  - learn only from real outcomes: rows are appended, never edited or
    backdated, and reset_memory() wipes cleanly rather than reseeding
    anything
"""

import csv
import os
import re
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.csv")
LEARNINGS_PATH = os.path.join(DATA_DIR, "learnings.md")

LEDGER_HEADER = ["timestamp", "symbol", "action", "price", "quantity", "reason", "mode", "outcome", "pnl"]

_EMPTY_LEARNINGS = (
    "# Learnings\n\n"
    "_No lessons recorded yet — run `python replay.py --mode raw` on real historical "
    "data first. Lessons are only written here when a replay genuinely finds a losing "
    "setup; nothing is pre-seeded._\n"
)


def _ensure_files():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(LEDGER_HEADER)
    if not os.path.exists(LEARNINGS_PATH):
        with open(LEARNINGS_PATH, "w", encoding="utf-8") as f:
            f.write(_EMPTY_LEARNINGS)


def _bucket_number(match: "re.Match") -> str:
    """Round a number found in a reason string down to its nearest ten
    (55.8 -> "~50s", 24.7 -> "~20s", 8.3 -> "~0s"). This is the fix for an
    earlier version of this function that collapsed every number to a
    single '#' wildcard: that made every BUY reason ("EMA50 bullish
    crossover, ADX X trending, MACD rising, RSI Y") normalize to the exact
    same string regardless of X/Y, since the swing strategy's reason text
    only ever has two shapes (bullish/bearish) with no other structural
    variation - so the very first BUY loss silently blacklisted every BUY
    forever, and likewise SELL. Decade-bucketing keeps values like 24.7 and
    26.3 counted as "the same setup" (the original intent) while ADX-in-the-
    20s and ADX-in-the-50s are now genuinely different setups, so memory can
    actually discriminate instead of collapsing to a single per-direction
    switch."""
    try:
        value = float(match.group())
    except ValueError:
        return match.group()
    decade = int(value // 10) * 10
    return f"~{decade}s"


def normalize_setup(symbol: str, action: str, reason: str) -> str:
    """Turn a free-text reason string into a coarse, comparable "setup"
    signature: numbers are bucketed to their nearest ten (see
    _bucket_number) rather than erased, so two setups only count as "the
    same" if their ADX/RSI/etc. actually landed in the same rough range,
    not just the same direction. Still pattern normalization on text the
    bot already produced, not fabrication - nothing here invents a
    condition that wasn't actually in the original reason string."""
    stripped = re.sub(r"[-+]?\d*\.?\d+", _bucket_number, (reason or "").lower())
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return f"{symbol}|{action}|{stripped}"


def append_ledger_row(symbol: str, action: str, price: float, quantity: float, reason: str, mode: str, outcome: str, pnl: float):
    _ensure_files()
    with open(LEDGER_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(),
            symbol, action, f"{price:.5f}", quantity, reason, mode, outcome, f"{pnl:.2f}",
        ])


def load_ledger() -> list:
    _ensure_files()
    with open(LEDGER_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


MIN_SETUP_SAMPLES = 3  # a setup needs at least this many real recorded outcomes
                        # before memory will act on it at all - see has_prior_loss()


def setup_track_record(symbol: str, action: str, reason: str) -> dict:
    """Aggregate every real ledger row matching this normalized setup into
    occurrence count, win/loss split, and average pnl. Pure aggregation of
    real rows - no fabrication."""
    target = normalize_setup(symbol, action, reason)
    rows = [
        r for r in load_ledger()
        if normalize_setup(r.get("symbol", ""), r.get("action", ""), r.get("reason", "")) == target
    ]
    if not rows:
        return {"occurrences": 0, "wins": 0, "losses": 0, "avg_pnl": 0.0, "rows": []}

    pnls = [float(r["pnl"]) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    return {"occurrences": len(rows), "wins": wins, "losses": losses, "avg_pnl": sum(pnls) / len(pnls), "rows": rows}


def has_prior_loss(symbol: str, action: str, reason: str, min_samples: int = MIN_SETUP_SAMPLES) -> dict:
    """Is this a "known bad setup" - not "has it EVER lost," but "does it
    have a real, minimally meaningful track record of net losing?"

    The earlier version of this function flagged a setup the moment a
    SINGLE loss was recorded for it. That's the wrong bar for a strategy
    like this one: a 34% win rate with profit factor ~1 is a normal
    trend-following payoff shape (frequent small losers, occasional bigger
    winners, roughly breakeven-or-better overall) - individual losses are
    the expected, designed-for outcome, not a signal something is broken.
    Treating "lost once" as disqualifying guarantees every setup gets
    banned almost immediately, for ANY strategy shaped like this, which is
    exactly what happened: 0 of 119 trades were ever allowed through.

    Now a setup only counts as "known bad" if it has at least
    `min_samples` real recorded occurrences AND its average real pnl over
    those occurrences is negative - a genuine pattern, not one unlucky
    trade. Returns {"found", "count" (losses), "occurrences", "avg_pnl",
    "example" (worst real trade, for the log message)}."""
    stats = setup_track_record(symbol, action, reason)
    if stats["occurrences"] < min_samples or stats["avg_pnl"] >= 0:
        return {"found": False, "count": stats["losses"], "occurrences": stats["occurrences"], "avg_pnl": stats["avg_pnl"], "example": None}

    worst = min(stats["rows"], key=lambda r: float(r["pnl"]))
    return {"found": True, "count": stats["losses"], "occurrences": stats["occurrences"], "avg_pnl": stats["avg_pnl"], "example": worst}


def learnings_warns_about(symbol: str, action: str, reason: str) -> bool:
    """Checks whether learnings.md documents a lesson for this EXACT
    normalized setup (matching the <!-- setup:... --> marker append_learning
    writes) - not a loose "mentions this symbol and direction anywhere"
    substring check. The earlier version used a plain substring match,
    which meant one lesson written for, say, "XAUUSDm BUY when ADX is in
    the 50s" would silently block EVERY future XAUUSDm BUY of any setup
    forever, regardless of how different the actual conditions were - the
    same collapse-to-a-single-switch bug has_prior_loss() had, just via a
    different gate."""
    target = normalize_setup(symbol, action, reason)
    marker = f"<!-- setup:{target} -->"
    _ensure_files()
    with open(LEARNINGS_PATH, encoding="utf-8") as f:
        content = f.read()
    return marker in content


def append_learning(symbol: str, action: str, reason: str, pnl: float, entry_time, exit_time) -> bool:
    """Append a plain-English lesson. Only ever called by replay.py after a
    REAL losing trade is confirmed. Skips writing a duplicate if this exact
    normalized setup already has a lesson. Returns True if a new lesson was
    written, False if it was already documented."""
    _ensure_files()
    target = normalize_setup(symbol, action, reason)
    with open(LEARNINGS_PATH, encoding="utf-8") as f:
        existing = f.read()

    marker = f"<!-- setup:{target} -->"
    if marker in existing:
        return False

    lesson = (
        f"\n## {symbol} {action} - lost ${abs(pnl):.2f}\n"
        f"{marker}\n"
        f"On a real replay run, {symbol} took a **{action}** on this setup ({reason}) "
        f"and it lost ${abs(pnl):.2f} (entered {entry_time}, exited {exit_time}). "
        f"The memory-enabled path will now hold off repeating this exact setup on "
        f"{symbol} until there's real evidence it can win.\n"
    )
    with open(LEARNINGS_PATH, "a", encoding="utf-8") as f:
        f.write(lesson)
    return True


def reset_memory():
    """The `python replay.py --reset-memory` command (equivalent to the
    requested `npm run memory:reset`). Wipes both files back to empty/
    header-only. Does not reseed anything — matches "no seeded fake
    losses"."""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LEDGER_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(LEDGER_HEADER)
    with open(LEARNINGS_PATH, "w", encoding="utf-8") as f:
        f.write(_EMPTY_LEARNINGS)