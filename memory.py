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


def normalize_setup(symbol: str, action: str, reason: str) -> str:
    """Turn a free-text reason string into a coarse, comparable "setup"
    signature: numbers are collapsed to '#' (ADX 24.7 and ADX 31.9 count as
    the same setup for memory purposes) while the structural wording is
    kept as-is. This is pattern normalization on text the bot already
    produced, not fabrication — nothing here invents a condition that
    wasn't actually in the original reason string."""
    stripped = re.sub(r"[-+]?\d*\.?\d+", "#", (reason or "").lower())
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


def has_prior_loss(symbol: str, action: str, reason: str) -> dict:
    """Has this exact symbol + direction + normalized-setup recorded a real
    loss in the ledger before? Returns {"found", "count", "example"}."""
    target = normalize_setup(symbol, action, reason)
    rows = load_ledger()
    losses = [
        r for r in rows
        if r.get("outcome") == "loss" and normalize_setup(r.get("symbol", ""), r.get("action", ""), r.get("reason", "")) == target
    ]
    return {"found": bool(losses), "count": len(losses), "example": losses[0] if losses else None}


def learnings_warns_about(symbol: str, action: str) -> bool:
    """Simple, auditable substring check against learnings.md — deliberately
    not NLP/fuzzy matching, so it's easy to verify by eye why a warning did
    or didn't fire."""
    _ensure_files()
    with open(LEARNINGS_PATH, encoding="utf-8") as f:
        content = f.read().lower()
    return symbol.lower() in content and action.lower() in content


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