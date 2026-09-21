"""Fibonacci retracement confluence check.

Finds the most recent significant swing (high-to-low or low-to-high) in a
lookback window, computes the 23.6/38.2/50/61.8% retracement levels off it —
the same ratios both uploaded gold-trading guides walk through at length —
and reports whether the current close is sitting on one of them, within an
ATR-scaled tolerance.

This does NOT invent a standalone trading signal. Price being near "a" Fib
level means very little by itself — price is near some level most of the
time. Exactly like both guides use it, this is meant to feed into
evaluate() as one more confluence input on top of a setup that already
passed the core ADX/EMA/RSI/MACD/H4 rules, never as a trigger on its own.
"""

from typing import Optional

import pandas as pd

FIB_RATIOS = {
    "23.6%": 0.236,
    "38.2%": 0.382,
    "50.0%": 0.5,
    "61.8%": 0.618,
}


def _find_last_swing(df: pd.DataFrame, lookback: int) -> Optional[dict]:
    """Take the highest high and lowest low over the lookback window. Whichever
    extreme happened MORE RECENTLY marks the start of the current retracement
    leg: if the low came after the high, price has been falling since the
    high and we're measuring a bounce back UP from that low ("down" swing);
    if the high came after the low, we're measuring a pullback DOWN from
    that high ("up" swing)."""
    window = df.tail(lookback)
    if len(window) < lookback:
        return None

    high_idx = window["high"].idxmax()
    low_idx = window["low"].idxmin()
    high_val = window.loc[high_idx, "high"]
    low_val = window.loc[low_idx, "low"]

    if not (high_val > low_val):
        return None  # degenerate window (flat data), nothing to retrace

    if window.index.get_loc(low_idx) > window.index.get_loc(high_idx):
        return {"direction": "down", "start": high_val, "end": low_val}
    return {"direction": "up", "start": low_val, "end": high_val}


def compute_levels(df: pd.DataFrame, lookback: int) -> Optional[dict]:
    """Returns {"direction": "up"|"down", "swing_start": ..., "swing_end": ...,
    "levels": {"23.6%": price, ...}} or None if no valid swing was found
    (e.g. not enough bars yet)."""
    swing = _find_last_swing(df, lookback)
    if swing is None:
        return None

    start, end, direction = swing["start"], swing["end"], swing["direction"]
    move = abs(end - start)

    levels = {}
    for label, ratio in FIB_RATIOS.items():
        if direction == "down":
            # high (start) -> low (end): retracement bounces back UP from the low
            levels[label] = end + ratio * move
        else:
            # low (start) -> high (end): retracement pulls back DOWN from the high
            levels[label] = end - ratio * move

    return {"direction": direction, "swing_start": start, "swing_end": end, "levels": levels}


def check_confluence(df: pd.DataFrame, atr: float, lookback: int, tolerance_atr_frac: float = 0.25) -> dict:
    """Returns {"near_level": <label or None>, "level_price": <float or None>,
    "direction": "bullish" | "bearish" | None, "reasons": [...]}.

    "direction" here means the bias this level argues for: if the broader
    swing being retraced was a DOWN move, this level is acting as support
    underneath a bounce (bullish); if the swing was an UP move, it's acting
    as resistance capping a pullback (bearish) — exactly how both guides
    describe Fib levels functioning as support/resistance.

    tolerance scales with ATR (not a fixed price distance) so it adapts to
    the instrument's own volatility, the same convention every other
    distance check in this codebase already uses (SL/TP sizing, S/R zones)."""
    if atr is None or pd.isna(atr) or atr <= 0:
        return {"near_level": None, "level_price": None, "direction": None, "reasons": []}

    computed = compute_levels(df, lookback)
    if computed is None:
        return {"near_level": None, "level_price": None, "direction": None, "reasons": []}

    close = df["close"].iloc[-1]
    tolerance = tolerance_atr_frac * atr

    nearest_label, nearest_level, nearest_dist = None, None, None
    for label, level in computed["levels"].items():
        dist = abs(close - level)
        if nearest_dist is None or dist < nearest_dist:
            nearest_label, nearest_level, nearest_dist = label, level, dist

    if nearest_dist is None or nearest_dist > tolerance:
        return {"near_level": None, "level_price": None, "direction": None, "reasons": []}

    bias = "bullish" if computed["direction"] == "down" else "bearish"
    reason = (
        f"price {close:.5f} within {nearest_dist:.5f} of the {nearest_label} "
        f"Fibonacci retracement level ({nearest_level:.5f}) of the last swing"
    )
    return {"near_level": nearest_label, "level_price": nearest_level, "direction": bias, "reasons": [reason]}