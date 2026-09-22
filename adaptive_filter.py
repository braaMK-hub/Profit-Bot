"""Memory-enabled decision filter.

Wraps a raw BUY/SELL decision from the backtester's rule engine and checks
it against real recorded memory (data/ledger.csv, data/learnings.md) before
allowing it through. Never invents a warning: if memory has nothing to say
about this exact symbol/direction/setup, the decision passes through
unchanged and the caller is told memory is still cold for it.

This module makes no trading decision on its own beyond "allow" or "skip" —
it never turns a HOLD into a trade, and it never fabricates a reason to
skip one that memory doesn't actually support.
"""

import memory


def check_against_memory(symbol: str, action: str, reason: str) -> dict:
    """Returns one of:
      {"allow": True,  "note": None,                         "cold": False} - no memory objection
      {"allow": False, "note": "<why skipped>",               "cold": False} - a real prior loss or
                                                                                learnings.md entry says skip
      {"allow": True,  "note": "<no memory yet for this setup>", "cold": True} - ledger is empty
    """
    prior_loss = memory.has_prior_loss(symbol, action, reason)
    if prior_loss["found"]:
        example = prior_loss["example"]
        note = (
            f"skipped: {symbol} {action} has lost on this exact setup before "
            f"({prior_loss['count']}x, e.g. pnl={example['pnl']} on {example['timestamp']})"
        )
        return {"allow": False, "note": note, "cold": False}

    if memory.learnings_warns_about(symbol, action):
        return {"allow": False, "note": f"skipped: learnings.md warns about {symbol} {action} setups", "cold": False}

    ledger_rows = memory.load_ledger()
    if not ledger_rows:
        return {
            "allow": True,
            "note": "no memory yet for this symbol/setup - run `python replay.py --mode raw` first to build real history",
            "cold": True,
        }

    return {"allow": True, "note": None, "cold": False}