"""Candlestick pattern detection, built directly off the OHLC data already
fetched for every symbol — no new data source needed.

Scope is deliberately narrow: only the handful of patterns with a clear,
mechanical geometric definition (hammer/hangman, doji, three soldiers/three
crows, morning/evening star) — the same small set both uploaded gold-trading
guides call out as meaningful on their own. Left out on purpose: the more
subjective multi-day "sanpo" continuation patterns and anything that would
need human judgment to classify consistently.

Every detector takes a DataFrame ending at "now" (last row = most recently
closed candle) and returns:
    {"pattern": <name or None>, "bias": "bullish" | "bearish" | None, "reasons": [...]}

A bias of "bullish" means the pattern argues for an upward move; "bearish"
the opposite. None/None/[] means "nothing recognizable here" — that is a
normal, common result, not an error.

This module makes no trading decision by itself. signal_engine.py treats its
output the same way it already treats RSI divergence: a confluence input
that can nudge a score up or down, never a standalone trigger. A BUY/SELL
still has to come from the core ADX/EMA/RSI/MACD/H4 rules first.
"""

import pandas as pd


def _body(row) -> float:
    return abs(row["close"] - row["open"])


def _range(row) -> float:
    return row["high"] - row["low"]


def _upper_shadow(row) -> float:
    return row["high"] - max(row["open"], row["close"])


def _lower_shadow(row) -> float:
    return min(row["open"], row["close"]) - row["low"]


def _is_bullish_candle(row) -> bool:
    return row["close"] > row["open"]


def _no_signal() -> dict:
    return {"pattern": None, "bias": None, "reasons": []}


def _recent_trend(df: pd.DataFrame, lookback: int = 5) -> str:
    """Crude but honest trend-context check: compares the close `lookback`
    bars before the candle being classified to the close 1 bar before it
    (i.e. excludes the candle itself from the trend it's judged against —
    a hammer only means something relative to what came before it, not
    including its own move). Returns "unknown" if there isn't enough
    history yet, which every caller below treats as "no context, no bias"
    rather than guessing."""
    if len(df) < lookback + 2:
        return "unknown"
    start = df["close"].iloc[-(lookback + 2)]
    end = df["close"].iloc[-2]
    if end > start:
        return "up"
    if end < start:
        return "down"
    return "flat"


def detect_hammer_hangman(
    df: pd.DataFrame,
    body_max_frac: float = 0.35,
    lower_shadow_min_mult: float = 2.0,
    upper_shadow_max_frac: float = 0.15,
) -> dict:
    """Small body, long lower shadow, negligible upper shadow. The exact same
    shape reads as a bullish Hammer after a down-move or a bearish Hangman
    after an up-move — candle colour doesn't decide which, preceding trend
    does (per the guide: "hammer/hangman can be either a white or black
    coloured candle")."""
    if len(df) < 1:
        return _no_signal()

    last = df.iloc[-1]
    rng = _range(last)
    if rng <= 0:
        return _no_signal()

    body = _body(last)
    lower = _lower_shadow(last)
    upper = _upper_shadow(last)

    shape_matches = (
        body / rng <= body_max_frac
        and lower >= lower_shadow_min_mult * max(body, 1e-9)
        and upper / rng <= upper_shadow_max_frac
    )
    if not shape_matches:
        return _no_signal()

    trend = _recent_trend(df)
    if trend == "down":
        return {
            "pattern": "hammer",
            "bias": "bullish",
            "reasons": ["hammer candle after a down-move - potential bullish reversal"],
        }
    if trend == "up":
        return {
            "pattern": "hangman",
            "bias": "bearish",
            "reasons": ["hangman candle after an up-move - potential bearish reversal"],
        }
    return _no_signal()


def detect_doji(df: pd.DataFrame, body_max_frac: float = 0.08) -> dict:
    """Open ~= close. On its own this only signals indecision — the guide is
    explicit that a Doji "indicates the trend has run its course," so the
    reversal direction it implies comes from whatever trend it interrupts,
    same logic as the hammer/hangman check above."""
    if len(df) < 1:
        return _no_signal()

    last = df.iloc[-1]
    rng = _range(last)
    if rng <= 0:
        return _no_signal()

    if _body(last) / rng > body_max_frac:
        return _no_signal()

    trend = _recent_trend(df)
    if trend == "up":
        return {"pattern": "doji", "bias": "bearish", "reasons": ["doji after an up-move - trend may be exhausted"]}
    if trend == "down":
        return {"pattern": "doji", "bias": "bullish", "reasons": ["doji after a down-move - trend may be exhausted"]}
    return _no_signal()


def detect_three_soldiers_crows(df: pd.DataFrame, min_body_frac: float = 0.4) -> dict:
    """Three consecutive same-colour candles, each with a real body (not
    noise-sized) and each closing beyond the prior close. Three white =
    Soldiers (bullish), three black = Crows (bearish) — this checks the
    core shape shared by every "variation" in the guide (gaps between
    sessions are allowed elsewhere, but this bot only sees closed-bar OHLC,
    so it checks the part of the definition that's actually verifiable from
    that: successive higher/lower closes with substantial bodies)."""
    if len(df) < 3:
        return _no_signal()

    c0, c1, c2 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

    def real_body(row) -> bool:
        rng = _range(row)
        return rng > 0 and _body(row) / rng >= min_body_frac

    if not (real_body(c0) and real_body(c1) and real_body(c2)):
        return _no_signal()

    all_bullish = all(_is_bullish_candle(c) for c in (c0, c1, c2))
    all_bearish = all(not _is_bullish_candle(c) for c in (c0, c1, c2))
    higher_closes = c1["close"] > c0["close"] and c2["close"] > c1["close"]
    lower_closes = c1["close"] < c0["close"] and c2["close"] < c1["close"]

    if all_bullish and higher_closes:
        return {
            "pattern": "three_soldiers",
            "bias": "bullish",
            "reasons": ["three white soldiers - strong bullish continuation/reversal"],
        }
    if all_bearish and lower_closes:
        return {
            "pattern": "three_crows",
            "bias": "bearish",
            "reasons": ["three black crows - strong bearish continuation/reversal"],
        }
    return _no_signal()


def detect_star(df: pd.DataFrame, min_body_frac: float = 0.5, small_body_max_frac: float = 0.3) -> dict:
    """Evening/Morning Star: a long candle, then a small-bodied candle, then a
    long candle closing back into the first candle's body in the opposite
    direction. This checks the same shape as the guide's Doji Star variant
    but with an ordinary small body for the middle candle instead of
    requiring a doji — a doji middle would be a strictly stronger version of
    the same signal, not a different one, so this catches the more common
    case."""
    if len(df) < 3:
        return _no_signal()

    c0, c1, c2 = df.iloc[-3], df.iloc[-2], df.iloc[-1]

    def real_body(row, frac) -> bool:
        rng = _range(row)
        return rng > 0 and _body(row) / rng >= frac

    def small_body(row) -> bool:
        rng = _range(row)
        return rng > 0 and _body(row) / rng <= small_body_max_frac

    if not (real_body(c0, min_body_frac) and small_body(c1) and real_body(c2, min_body_frac)):
        return _no_signal()

    c0_bullish = _is_bullish_candle(c0)
    c2_bullish = _is_bullish_candle(c2)
    c0_mid = (c0["open"] + c0["close"]) / 2

    if c0_bullish and not c2_bullish and c2["close"] < c0_mid:
        return {
            "pattern": "evening_star",
            "bias": "bearish",
            "reasons": ["evening star pattern - strong bearish reversal signal"],
        }
    if not c0_bullish and c2_bullish and c2["close"] > c0_mid:
        return {
            "pattern": "morning_star",
            "bias": "bullish",
            "reasons": ["morning star pattern - strong bullish reversal signal"],
        }
    return _no_signal()


def analyze(df: pd.DataFrame) -> dict:
    """Run every detector and return the single strongest hit, if any.

    Priority order (star > three soldiers/crows > hammer/hangman > doji)
    reflects the same "rarer three-candle patterns are stronger confirmation
    than a single candle" hierarchy both guides describe — if more than one
    shape matches the same closing bar, the rarer one wins instead of
    double-counting or picking arbitrarily.

    Returns {"pattern": None, "bias": None, "reasons": []} if nothing fired.
    That's the common case, not a failure — callers should treat it as
    "no candlestick signal this bar," same as an empty divergence result.
    """
    for detector in (detect_star, detect_three_soldiers_crows, detect_hammer_hangman, detect_doji):
        result = detector(df)
        if result["pattern"] and result["bias"]:
            return result
    return _no_signal()