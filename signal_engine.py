import pandas as pd
import numpy as np
from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import OnBalanceVolumeIndicator

from config import settings
from logger_setup import get_logger

log = get_logger("signal_engine")

# `ta`'s ADXIndicator raises a hard IndexError on input shorter than ~2x its
# window instead of degrading to NaN like the other indicators do. That has to
# be checked BEFORE add_indicators() runs, not after — the per-strategy
# min_bars_needed checks below all happen too late to catch this on their own,
# since they check the length of the already-indicatored frame.
MIN_BARS_FOR_INDICATORS = 30


class SignalEngine:
    """Turns raw OHLCV into a decision plus a plain-English reason list, so the log
    file explains itself instead of just spitting out "BUY" with no context."""

    def __init__(self):
        # per-symbol memory of S/R zones that got broken and flipped role (resistance
        # that became support, or vice versa). Lives on the instance because run.py
        # and Backtester both create ONE SignalEngine and reuse it for the whole
        # session/backtest — that's what makes this persistence actually work across
        # loop iterations / replayed bars, not a coincidence.
        self._sr_flip_state = {}

    def add_indicators(self, df: pd.DataFrame, ema_periods=(20, 50, 100, 200), rsi_period=14, adx_period=14) -> pd.DataFrame:
        # `ta`'s API is class-based (fit-then-extract), unlike pandas_ta's function
        # calls that hand back ready-made columns — that's the main adjustment here,
        # the underlying math is the same standard indicators.
        # Periods are parameterized so scalp mode can reuse this with fast EMAs/RSI
        # instead of duplicating the whole indicator stack.
        df = df.copy()

        fast, slow = ema_periods[0], ema_periods[1]
        df["ema_fast"] = EMAIndicator(df["close"], window=fast).ema_indicator()
        df["ema_slow"] = EMAIndicator(df["close"], window=slow).ema_indicator()
        # keep the old column names populated too when the full swing ribbon is used,
        # so evaluate() (swing mode) doesn't need to change at all
        if len(ema_periods) >= 4:
            df["ema20"] = df["ema_fast"]
            df["ema50"] = df["ema_slow"]
            df["ema100"] = EMAIndicator(df["close"], window=ema_periods[2]).ema_indicator()
            df["ema200"] = EMAIndicator(df["close"], window=ema_periods[3]).ema_indicator()

        df["adx"] = self._safe_wilder_indicator(
            lambda: ADXIndicator(df["high"], df["low"], df["close"], window=adx_period).adx(),
            adx_period, df.index,
        )

        # MACD line and signal line as their own columns, not just the histogram —
        # divergence detection needs the raw line (an oscillator with its own swing
        # highs/lows), the histogram alone can't tell you if the line itself made a
        # higher or lower high. macd_hist keeps its existing meaning/name unchanged,
        # nothing that already reads macd_hist needs to change.
        macd_calc = MACD(df["close"])
        df["macd_line"] = macd_calc.macd()
        df["macd_signal"] = macd_calc.macd_signal()
        df["macd_hist"] = macd_calc.macd_diff()

        df["rsi"] = RSIIndicator(df["close"], window=rsi_period).rsi()

        # bollinger_pband() is %B directly, no manual (close - lower) / (upper - lower) needed
        df["bb_percent"] = BollingerBands(df["close"], window=20, window_dev=2).bollinger_pband()

        df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
        atr_window = 14
        df["atr"] = self._safe_wilder_indicator(
            lambda: AverageTrueRange(df["high"], df["low"], df["close"], window=atr_window).average_true_range(),
            atr_window, df.index,
        )
        return df

    def _safe_wilder_indicator(self, compute_fn, window: int, index: pd.Index) -> pd.Series:
        """ADX and ATR both use `ta`'s Wilder-smoothing implementation, which does
        a raw positional array write during its warmup period and throws
        IndexError/ValueError outright on input shorter than ~2x its window —
        unlike every other indicator in add_indicators(), which just NaN-pads the
        warmup period gracefully. Confirmed empirically: window=14 needs >= 28
        bars for ADX, >= 14 for ATR; below that it raises rather than degrading.

        This guards both, in one place, so EVERY caller — evaluate(), evaluate_scalp(),
        and all three new strategies — is protected without each needing its own
        length check for this specific failure mode. A too-short result comes back
        as an all-NaN column (built against the real index, not a bare range —
        misaligned-index NaN-fill is a pandas quirk to lean on by accident, not by
        design) so every downstream comparison against it (adx > threshold, price
        within atr-fraction of a line, etc.) evaluates to False — exactly "not
        enough signal to act," reached without any evaluator special-casing it."""
        n_bars = len(index)
        if n_bars < 2 * window:
            return pd.Series([float("nan")] * n_bars, index=index)
        try:
            return compute_fn()
        except (IndexError, ValueError) as e:
            log.warning(f"Indicator computation failed unexpectedly even above the safe bar threshold ({n_bars} bars, window {window}): {e}")
            return pd.Series([float("nan")] * n_bars, index=index)

    def _find_swings(self, series: pd.Series, order=3):
        """Plain pivot detection: a point is a swing high/low if it's the extreme
        value in a window of `order` bars on each side. No scipy needed for this."""
        highs, lows = [], []
        for i in range(order, len(series) - order):
            seg = series.iloc[i - order: i + order + 1]
            if series.iloc[i] == seg.max():
                highs.append(i)
            if series.iloc[i] == seg.min():
                lows.append(i)
        return highs, lows

    def _hold_result(self, symbol: str, df: pd.DataFrame, reasons: list) -> dict:
        """Shared by every new evaluator: a HOLD caused by "nothing to work with"
        (not enough bars, no valid line, no zone nearby) still has to explain
        itself — an empty reasons list on a HOLD is exactly the failure mode this
        spec calls out, so every early-exit in this file routes through here."""
        if len(df) == 0:
            return {"symbol": symbol, "decision": "HOLD", "score": 0, "atr": 0.0, "price": 0.0, "reasons": reasons}
        last = df.iloc[-1]
        atr = last["atr"] if "atr" in df.columns and pd.notna(last["atr"]) else 0.0
        return {"symbol": symbol, "decision": "HOLD", "score": 0, "atr": atr, "price": last["close"], "reasons": reasons}

    def detect_rsi_divergence(self, df: pd.DataFrame, lookback=None, tolerance=None) -> dict:
        """Compares the last two swing highs (or lows) in price against RSI at those
        same points. A swing only counts if the price move clears `tolerance`
        (filters out noise that isn't a real swing), and divergence strength scales
        with how clean the mismatch is: -2/+2 for a clear contradiction, -1/+1 for
        a borderline one. Still a heuristic, not textbook divergence software, but
        strong enough now to actually gate trades when settings.divergence_strict
        is on, instead of just nudging the score.

        This is swing mode's built-in veto check — a different, older, narrower
        thing than the standalone evaluate_divergence() strategy below. Kept as-is,
        untouched, per the "don't modify evaluate()" rule."""
        lookback = lookback or settings.divergence_lookback
        tolerance = tolerance or settings.divergence_tolerance

        window = df.tail(lookback).reset_index(drop=True)
        if len(window) < lookback:
            return {"direction": "none", "strength": 0}

        swing_highs, _ = self._find_swings(window["high"], order=3)
        _, swing_lows = self._find_swings(window["low"], order=3)

        direction, strength = "none", 0

        if len(swing_highs) >= 2:
            i1, i2 = swing_highs[-2], swing_highs[-1]
            price1, price2 = window["high"].iloc[i1], window["high"].iloc[i2]
            rsi1, rsi2 = window["rsi"].iloc[i1], window["rsi"].iloc[i2]
            price_change = (price2 - price1) / price1 if price1 else 0
            rsi_change = (rsi2 - rsi1) / max(abs(rsi1), 1e-9)

            if price_change > tolerance and rsi_change < -tolerance:
                direction = "bearish"
                strength = -2 if abs(rsi_change) > tolerance * 2 else -1

        if direction == "none" and len(swing_lows) >= 2:
            i1, i2 = swing_lows[-2], swing_lows[-1]
            price1, price2 = window["low"].iloc[i1], window["low"].iloc[i2]
            rsi1, rsi2 = window["rsi"].iloc[i1], window["rsi"].iloc[i2]
            price_change = (price2 - price1) / price1 if price1 else 0
            rsi_change = (rsi2 - rsi1) / max(abs(rsi1), 1e-9)

            if price_change < -tolerance and rsi_change > tolerance:
                direction = "bullish"
                strength = 2 if abs(rsi_change) > tolerance * 2 else 1

        return {"direction": direction, "strength": strength}

    def h4_bias(self, h4_df: pd.DataFrame) -> str:
        h4 = self.add_indicators(h4_df)
        last = h4.iloc[-1]
        if last["close"] > last["ema100"]:
            return "bullish"
        if last["close"] < last["ema100"]:
            return "bearish"
        return "neutral"

    def evaluate(self, symbol: str, dfs: dict) -> dict:
        """dfs = {"M5": df, "H1": df, "H4": df}. Decision made on H1, H4 vetoes trades
        against the bigger trend, divergence can now also veto when strict mode is on."""
        h1 = self.add_indicators(dfs["H1"])
        h4_trend = self.h4_bias(dfs["H4"])

        last = h1.iloc[-1]
        prev = h1.iloc[-2]

        macd_rising = last["macd_hist"] > prev["macd_hist"]
        macd_falling = last["macd_hist"] < prev["macd_hist"]
        divergence = self.detect_rsi_divergence(h1)

        trending = last["adx"] > settings.adx_trend_threshold
        score = 1 if trending else 0
        reasons = [f"ADX {last['adx']:.1f} {'confirms' if trending else 'fails to confirm'} a trend"]

        buy_conditions = (
            trending
            and last["close"] > last["ema50"]
            and last["rsi"] < settings.rsi_buy_ceiling
            and macd_rising
        )
        sell_conditions = (
            trending
            and last["close"] < last["ema50"]
            and last["rsi"] > settings.rsi_sell_floor
            and macd_falling
        )

        decision = "HOLD"
        if buy_conditions and h4_trend != "bearish":
            decision = "BUY"
            score += 3
            reasons.append("price > EMA50, RSI not overbought, MACD histogram rising")
        elif sell_conditions and h4_trend != "bullish":
            decision = "SELL"
            score += 3
            reasons.append("price < EMA50, RSI not oversold, MACD histogram falling")
        elif buy_conditions and h4_trend == "bearish":
            reasons.append("BUY setup vetoed, H4 trend is bearish")
        elif sell_conditions and h4_trend == "bullish":
            reasons.append("SELL setup vetoed, H4 trend is bullish")

        # divergence contradicting the signal: strong ones veto outright in strict mode,
        # weak ones just cost the setup a point like before
        contradicts_buy = decision == "BUY" and divergence["direction"] == "bearish"
        contradicts_sell = decision == "SELL" and divergence["direction"] == "bullish"

        if contradicts_buy or contradicts_sell:
            if settings.divergence_strict and abs(divergence["strength"]) >= 2:
                reasons.append(f"{divergence['direction']} RSI divergence (strength {divergence['strength']}) vetoed the trade")
                decision = "HOLD"
            else:
                score += divergence["strength"]  # negative, just dents the score
                reasons.append(f"{divergence['direction']} RSI divergence (strength {divergence['strength']}) noted, not strong enough to veto")

        if decision == "BUY" and last["obv"] > h1["obv"].iloc[-5]:
            score += 1
            reasons.append("OBV rising, volume backs the move")
        if decision == "SELL" and last["obv"] < h1["obv"].iloc[-5]:
            score += 1
            reasons.append("OBV falling, volume backs the move")

        result = {
            "symbol": symbol,
            "decision": decision,
            "score": score,
            "atr": last["atr"],
            "price": last["close"],
            "h4_trend": h4_trend,
            "divergence": divergence,
            "reasons": reasons,
        }
        log.info(f"{symbol}: {decision} (score={score}) - {'; '.join(reasons)}")
        return result

    def trend_bias_fast(self, trend_df: pd.DataFrame, ema_fast: int, ema_slow: int) -> str:
        """Lighter trend filter for scalp mode — an EMA cross on a faster timeframe
        (e.g. M15) rather than H4's ema100, since waiting for H4 to confirm would
        mean the scalp opportunity is long gone by the time it does."""
        df = self.add_indicators(trend_df, ema_periods=(ema_fast, ema_slow))
        last = df.iloc[-1]
        if last["ema_fast"] > last["ema_slow"]:
            return "bullish"
        if last["ema_fast"] < last["ema_slow"]:
            return "bearish"
        return "neutral"

    def evaluate_scalp(self, symbol: str, dfs: dict) -> dict:
        """dfs = {"entry": df, "trend": df}. Scalping needs fast reactions: short
        EMAs, a short RSI period, and a looser ADX threshold since momentum bursts
        on M1/M5 don't print the same sustained trend swing setups look for.

        Deliberately skips RSI divergence here — that check needs a real lookback
        window of swing structure, and on a 1-minute chart 30-40 bars is maybe
        half an hour of noise, not a meaningful swing. Use divergence in swing
        mode, not here."""
        ema_fast = settings.scalp_ema_fast
        ema_slow = settings.scalp_ema_slow
        rsi_period = settings.scalp_rsi_period
        adx_threshold = settings.scalp_adx_threshold

        entry = self.add_indicators(dfs["entry"], ema_periods=(ema_fast, ema_slow), rsi_period=rsi_period)
        trend = self.trend_bias_fast(dfs["trend"], ema_fast=ema_fast, ema_slow=ema_slow)

        last = entry.iloc[-1]
        prev = entry.iloc[-2]

        macd_rising = last["macd_hist"] > prev["macd_hist"]
        macd_falling = last["macd_hist"] < prev["macd_hist"]

        trending = last["adx"] > adx_threshold
        score = 1 if trending else 0
        reasons = [f"ADX {last['adx']:.1f} {'confirms' if trending else 'fails to confirm'} momentum"]

        # fast/slow EMA cross plus a MACD kick does the heavy lifting here; RSI is
        # just a filter against entering right into an already-exhausted spike
        buy_conditions = trending and last["ema_fast"] > last["ema_slow"] and 30 < last["rsi"] < 70 and macd_rising
        sell_conditions = trending and last["ema_fast"] < last["ema_slow"] and 30 < last["rsi"] < 70 and macd_falling

        decision = "HOLD"
        if buy_conditions and trend != "bearish":
            decision = "BUY"
            score += 2
            reasons.append("fast EMA > slow EMA, RSI mid-range, MACD histogram rising")
        elif sell_conditions and trend != "bullish":
            decision = "SELL"
            score += 2
            reasons.append("fast EMA < slow EMA, RSI mid-range, MACD histogram falling")
        elif buy_conditions and trend == "bearish":
            reasons.append("BUY setup vetoed, higher-timeframe trend is bearish")
        elif sell_conditions and trend == "bullish":
            reasons.append("SELL setup vetoed, higher-timeframe trend is bullish")

        result = {
            "symbol": symbol,
            "decision": decision,
            "score": score,
            "atr": last["atr"],
            "price": last["close"],
            "trend": trend,
            "reasons": reasons,
        }
        log.info(f"[SCALP] {symbol}: {decision} (score={score}) - {'; '.join(reasons)}")
        return result

    # ------------------------------------------------------------------
    # Divergence strategy — standalone evaluator, distinct from the swing-mode
    # veto check above (detect_rsi_divergence). That one only ever looks at RSI
    # and only ever dents/blocks a swing signal that already exists. This one is
    # a full strategy in its own right: RSI, MACD line, or OBV, regular or
    # hidden divergence, its own confirmation and scoring rules.
    # ------------------------------------------------------------------

    def _oscillator_divergence(self, window: pd.DataFrame, osc_col: str, min_gap_bars: int) -> dict:
        """Finds the most recent divergence — regular or hidden — between price
        swings and one oscillator column, within `window`. Regular divergence
        (price and oscillator disagree on direction) suggests a reversal; hidden
        divergence (price and oscillator agree on direction, but by different
        margins) suggests the existing trend is likely to continue. Both are
        legitimate divergence patterns, they just mean opposite things for what
        comes next."""
        swing_highs, _ = self._find_swings(window["high"], order=3)
        _, swing_lows = self._find_swings(window["low"], order=3)

        def _filter_gap(idxs):
            # drop pivots that formed too close together to represent a real
            # separate swing — that's noise, not structure — then keep only the
            # last few (spec asks for 4-6 recent swings, not the whole history)
            filtered = []
            for idx in idxs:
                if not filtered or idx - filtered[-1] >= min_gap_bars:
                    filtered.append(idx)
            return filtered[-6:]

        swing_highs = _filter_gap(swing_highs)
        swing_lows = _filter_gap(swing_lows)

        best = {"type": "none", "strength": 0, "swing_idx": None, "bars_ago": None}

        if len(swing_lows) >= 2:
            i1, i2 = swing_lows[-2], swing_lows[-1]
            price1, price2 = window["low"].iloc[i1], window["low"].iloc[i2]
            osc1, osc2 = window[osc_col].iloc[i1], window[osc_col].iloc[i2]
            osc_change_pct = abs(osc2 - osc1) / max(abs(osc1), 1e-9)
            strength = 2 if osc_change_pct > settings.divergence_tolerance * 2 else 1

            if price2 < price1 and osc2 > osc1:
                best = {"type": "regular_bullish", "strength": strength, "swing_idx": i2, "bars_ago": len(window) - 1 - i2}
            elif price2 > price1 and osc2 < osc1:
                best = {"type": "hidden_bullish", "strength": strength, "swing_idx": i2, "bars_ago": len(window) - 1 - i2}

        if best["type"] == "none" and len(swing_highs) >= 2:
            i1, i2 = swing_highs[-2], swing_highs[-1]
            price1, price2 = window["high"].iloc[i1], window["high"].iloc[i2]
            osc1, osc2 = window[osc_col].iloc[i1], window[osc_col].iloc[i2]
            osc_change_pct = abs(osc2 - osc1) / max(abs(osc1), 1e-9)
            strength = 2 if osc_change_pct > settings.divergence_tolerance * 2 else 1

            if price2 > price1 and osc2 < osc1:
                best = {"type": "regular_bearish", "strength": strength, "swing_idx": i2, "bars_ago": len(window) - 1 - i2}
            elif price2 < price1 and osc2 > osc1:
                best = {"type": "hidden_bearish", "strength": strength, "swing_idx": i2, "bars_ago": len(window) - 1 - i2}

        return best

    def _is_confirmed_reversal(self, window: pd.DataFrame, swing_idx: int, direction: str, confirm_bars: int) -> bool:
        """Don't signal on the bar the swing itself forms — wait `confirm_bars`
        bars and check price has actually moved to the reversal side of that
        swing bar's own range. Deliberately simple: this is a confirmation
        filter to avoid firing on a pivot that hasn't gone anywhere yet, not the
        entry trigger itself."""
        if swing_idx is None or swing_idx + confirm_bars >= len(window):
            return False  # not enough bars have passed since the swing to confirm anything yet
        swing_bar = window.iloc[swing_idx]
        last_close = window["close"].iloc[-1]
        if direction == "up":
            return last_close > swing_bar["high"]
        return last_close < swing_bar["low"]

    def evaluate_divergence(self, symbol: str, dfs: dict) -> dict:
        """dfs = {"H1": df, "H4": df, ...}. H4 is accepted for shape-consistency
        with the other strategies but not consulted here — the divergence rules
        as specified are self-contained on H1, no higher-timeframe veto in them."""
        raw_h1 = dfs["H1"]
        if len(raw_h1) < MIN_BARS_FOR_INDICATORS:
            return self._hold_result(symbol, raw_h1, [f"only {len(raw_h1)} H1 bars available, need at least {MIN_BARS_FOR_INDICATORS} before indicators can even be computed"])
        h1 = self.add_indicators(raw_h1)

        lookback = settings.divstrat_lookback_bars
        min_gap = settings.divstrat_min_swing_gap_bars
        confirm_bars = settings.divstrat_confirmation_bars
        strength_threshold = settings.divstrat_strength_threshold
        oscillator = settings.divstrat_oscillator

        min_bars_needed = lookback + confirm_bars + 10
        if len(h1) < min_bars_needed:
            return self._hold_result(symbol, h1, [f"not enough H1 history for divergence strategy (need {min_bars_needed}, have {len(h1)})"])

        window = h1.tail(lookback).reset_index(drop=True)
        osc_columns = {"RSI": "rsi", "MACD": "macd_line", "OBV": "obv"}

        if oscillator == "any_two":
            candidates = {name: self._oscillator_divergence(window, col, min_gap) for name, col in osc_columns.items()}
            type_votes = {}
            for name, result in candidates.items():
                if result["type"] != "none":
                    type_votes.setdefault(result["type"], []).append((name, result))

            agreeing_type, agreeing = None, []
            for t, votes in type_votes.items():
                if len(votes) >= 2:
                    agreeing_type, agreeing = t, votes
                    break

            if agreeing_type is None:
                if type_votes:
                    return self._hold_result(symbol, h1, [f"divergence found ({', '.join(type_votes.keys())}) but not confirmed by 2+ oscillators"])
                return self._hold_result(symbol, h1, [f"no divergence found across RSI/MACD/OBV in the last {lookback} bars"])

            best = agreeing[0][1]
            strength = max(r["strength"] for _, r in agreeing)
            confirming_names = [n for n, _ in agreeing]
            extra_confirmations = len(confirming_names) - 1
            label = oscillator
        else:
            col = osc_columns.get(oscillator, "rsi")
            best = self._oscillator_divergence(window, col, min_gap)
            strength = best["strength"]
            confirming_names = [oscillator] if best["type"] != "none" else []
            extra_confirmations = 0
            label = oscillator

        if best["type"] == "none":
            return self._hold_result(symbol, h1, [f"no {label} divergence found in the last {lookback} bars"])

        if strength < strength_threshold:
            return self._hold_result(symbol, h1, [f"{best['type'].replace('_', ' ')} divergence found (strength {strength}) but below threshold {strength_threshold}"])

        direction = "up" if "bullish" in best["type"] else "down"
        confirmed = self._is_confirmed_reversal(window, best["swing_idx"], direction, confirm_bars)

        reasons = [f"{best['type'].replace('_', ' ')} {label} divergence over {best['bars_ago']} bars"]
        for name in confirming_names[1:]:
            reasons.append(f"{name} agrees")

        if not confirmed:
            reasons.append(f"waiting {confirm_bars} confirmation bar(s)")
            return self._hold_result(symbol, h1, reasons)

        decision = "BUY" if "bullish" in best["type"] else "SELL"
        score = (3 if strength >= 2 else 2) + extra_confirmations
        last = h1.iloc[-1]

        result = {"symbol": symbol, "decision": decision, "score": score, "atr": last["atr"], "price": last["close"], "reasons": reasons}
        log.info(f"[DIVERGENCE] {symbol}: {decision} (score={score}) - {'; '.join(reasons)}")
        return result

    # ------------------------------------------------------------------
    # Trendline strategy
    # ------------------------------------------------------------------

    def _fit_line(self, indices: list, values: list) -> dict:
        """Degree-1 fit through a handful of swing points, plus R-squared so a
        line can be rejected when it doesn't actually describe the swings —
        forcing a straight line through scattered pivots is worse than having
        no line at all."""
        if len(indices) < 2:
            return None
        x = np.array(indices, dtype=float)
        y = np.array(values, dtype=float)
        coeffs = np.polyfit(x, y, 1)
        y_pred = np.polyval(coeffs, x)
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1.0 if ss_tot == 0 else max(0.0, 1 - ss_res / ss_tot)
        return {"slope": coeffs[0], "coeffs": coeffs, "r_squared": r_squared}

    def evaluate_trendline(self, symbol: str, dfs: dict) -> dict:
        """dfs = {"H1": df, "H4": df, ...}. H4 accepted for shape-consistency,
        not consulted — same reasoning as evaluate_divergence above."""
        raw_h1 = dfs["H1"]
        if len(raw_h1) < MIN_BARS_FOR_INDICATORS:
            return self._hold_result(symbol, raw_h1, [f"only {len(raw_h1)} H1 bars available, need at least {MIN_BARS_FOR_INDICATORS} before indicators can even be computed"])
        h1 = self.add_indicators(raw_h1)

        lookback = settings.trendline_swing_lookback
        min_swings = settings.trendline_min_swings_required
        min_r2 = settings.trendline_min_r_squared
        touch_frac = settings.trendline_touch_atr_fraction
        break_frac = settings.trendline_break_atr_fraction
        require_slope = settings.trendline_require_slope_direction

        min_bars_needed = lookback + 10
        if len(h1) < min_bars_needed:
            return self._hold_result(symbol, h1, [f"not enough H1 history for trendline strategy (need {min_bars_needed}, have {len(h1)})"])

        window = h1.tail(lookback).reset_index(drop=True)
        last = window.iloc[-1]
        current_idx = len(window) - 1
        atr = last["atr"]
        close = last["close"]

        swing_highs, _ = self._find_swings(window["high"], order=3)
        _, swing_lows = self._find_swings(window["low"], order=3)

        resistance = None
        if len(swing_highs) >= min_swings:
            idxs = swing_highs[-min_swings:]
            vals = [window["high"].iloc[i] for i in idxs]
            fit = self._fit_line(idxs, vals)
            if fit and fit["r_squared"] >= min_r2:
                resistance = fit
                resistance["value_now"] = float(np.polyval(fit["coeffs"], current_idx))
                resistance["swings_used"] = len(idxs)

        support = None
        if len(swing_lows) >= min_swings:
            idxs = swing_lows[-min_swings:]
            vals = [window["low"].iloc[i] for i in idxs]
            fit = self._fit_line(idxs, vals)
            if fit and fit["r_squared"] >= min_r2:
                support = fit
                support["value_now"] = float(np.polyval(fit["coeffs"], current_idx))
                support["swings_used"] = len(idxs)

        if resistance is None and support is None:
            return self._hold_result(symbol, h1, [
                f"no trendline cleared R-squared >= {min_r2} (need {min_swings}+ matching swings, "
                f"have {len(swing_highs)} highs / {len(swing_lows)} lows in the last {lookback} bars)"
            ])

        resistance_break = (
            resistance is not None
            and close > resistance["value_now"] + break_frac * atr
            and (not require_slope or resistance["slope"] <= 0)
        )
        support_break = (
            support is not None
            and close < support["value_now"] - break_frac * atr
            and (not require_slope or support["slope"] >= 0)
        )
        support_touch = support is not None and 0 <= (close - support["value_now"]) <= touch_frac * atr
        resistance_touch = resistance is not None and 0 <= (resistance["value_now"] - close) <= touch_frac * atr

        decision, score, reasons = "HOLD", 0, []

        if resistance_break:
            decision, score = "BUY", 3
            reasons.append(f"broke downsloping/flat resistance at {resistance['value_now']:.5f} (R-squared={resistance['r_squared']:.2f}, last {resistance['swings_used']} highs)")
        elif support_break:
            decision, score = "SELL", 3
            reasons.append(f"broke upsloping/flat support at {support['value_now']:.5f} (R-squared={support['r_squared']:.2f}, last {support['swings_used']} lows)")
        elif support_touch:
            decision, score = "BUY", 2
            reasons.append(f"touched and held upsloping support from last {support['swings_used']} lows at {support['value_now']:.5f} (R-squared={support['r_squared']:.2f})")
        elif resistance_touch:
            decision, score = "SELL", 2
            reasons.append(f"touched and held downsloping resistance from last {resistance['swings_used']} highs at {resistance['value_now']:.5f} (R-squared={resistance['r_squared']:.2f})")
        else:
            details = []
            if resistance:
                details.append(f"resistance at {resistance['value_now']:.5f} (R-squared={resistance['r_squared']:.2f}), {(close - resistance['value_now']) / atr:.2f} ATR away")
            if support:
                details.append(f"support at {support['value_now']:.5f} (R-squared={support['r_squared']:.2f}), {(close - support['value_now']) / atr:.2f} ATR away")
            return self._hold_result(symbol, h1, [f"no touch or break yet - {'; '.join(details)}"])

        # bonus point: an upsloping support and a roughly flat resistance both
        # favoring the same direction is a cleaner read than either line alone.
        # "horizontal-ish" has no spec'd threshold, so this is a documented
        # heuristic, not a configurable one — a slope this close to flat isn't
        # meaningfully trending either way over the lookback window
        if resistance and support and decision == "BUY":
            horizontal_ish = abs(resistance["slope"]) < 0.05 * atr
            if support["slope"] > 0 and horizontal_ish:
                score += 1
                reasons.append("upsloping support and horizontal-ish resistance both favor the long side")

        result = {"symbol": symbol, "decision": decision, "score": score, "atr": atr, "price": close, "reasons": reasons}
        log.info(f"[TRENDLINE] {symbol}: {decision} (score={score}) - {'; '.join(reasons)}")
        return result

    # ------------------------------------------------------------------
    # Support / Resistance strategy
    # ------------------------------------------------------------------

    def _cluster_levels(self, window: pd.DataFrame, atr: float, cluster_frac: float, min_touches: int, max_zones: int) -> list:
        """Merges nearby swing highs/lows into zones instead of treating every
        pivot as its own level — real S/R is a band, not a single exact price."""
        swing_highs, _ = self._find_swings(window["high"], order=3)
        _, swing_lows = self._find_swings(window["low"], order=3)

        levels = sorted([window["high"].iloc[i] for i in swing_highs] + [window["low"].iloc[i] for i in swing_lows])
        if not levels:
            return []

        cluster_width = cluster_frac * atr
        clusters, current = [], [levels[0]]
        for lvl in levels[1:]:
            if lvl - current[-1] <= cluster_width:
                current.append(lvl)
            else:
                clusters.append(current)
                current = [lvl]
        clusters.append(current)

        zones = [{"lo": min(c), "hi": max(c), "touches": len(c)} for c in clusters if len(c) >= min_touches]
        zones.sort(key=lambda z: z["touches"], reverse=True)  # keep the most-touched (strongest) zones first
        return zones[:max_zones]

    def evaluate_sr(self, symbol: str, dfs: dict) -> dict:
        """dfs = {"H1": df, "H4": df, ...}. H4 accepted for shape-consistency,
        not consulted — same reasoning as the other two new evaluators."""
        raw_h1 = dfs["H1"]
        if len(raw_h1) < MIN_BARS_FOR_INDICATORS:
            return self._hold_result(symbol, raw_h1, [f"only {len(raw_h1)} H1 bars available, need at least {MIN_BARS_FOR_INDICATORS} before indicators can even be computed"])
        h1 = self.add_indicators(raw_h1)

        lookback = settings.sr_lookback
        cluster_frac = settings.sr_cluster_atr_fraction
        min_touches = settings.sr_min_touches
        approach_frac = settings.sr_approach_atr_fraction
        break_frac = settings.sr_break_atr_fraction
        max_zones = settings.sr_max_zones
        flip_persist_bars = settings.sr_flip_persist_bars

        min_bars_needed = lookback + 10
        if len(h1) < min_bars_needed:
            return self._hold_result(symbol, h1, [f"not enough H1 history for S/R strategy (need {min_bars_needed}, have {len(h1)})"])

        current_time = h1.index[-1]  # captured before reset_index(drop=True) below discards it
        window = h1.tail(lookback).reset_index(drop=True)
        last = window.iloc[-1]
        atr, close = last["atr"], last["close"]

        zones = self._cluster_levels(window, atr, cluster_frac, min_touches, max_zones)

        # Age out flipped zones by real elapsed H1 bars since the flip, not by how
        # many times this function has been called. Call-count drifts with
        # loop_interval_seconds (different across strategy modes) and, in
        # backtesting, silently stalls whenever a trade is already open (this
        # function isn't even invoked those bars) — counting against the data's
        # own timeline instead of the call cadence is correct in both cases and
        # invariant to both problems.
        flips = []
        for f in self._sr_flip_state.get(symbol, []):
            bars_since_flip = (h1.index > f["flip_time"]).sum()
            if bars_since_flip <= flip_persist_bars:
                flips.append(f)

        if not zones and not flips:
            self._sr_flip_state[symbol] = flips
            return self._hold_result(symbol, h1, [f"no S/R zones with {min_touches}+ touches found in the last {lookback} bars"])

        def _distance(zone):
            if close < zone["lo"]:
                return (zone["lo"] - close) / atr
            if close > zone["hi"]:
                return (close - zone["hi"]) / atr
            return 0.0

        candidates = [("zone", z) for z in zones] + [("flip", f) for f in flips]
        nearby = [(kind, z) for kind, z in candidates if _distance(z) <= approach_frac]

        if not nearby:
            self._sr_flip_state[symbol] = flips
            return self._hold_result(symbol, h1, ["no zone nearby"])

        kind, active = min(nearby, key=lambda kz: _distance(kz[1]))
        zone_desc = f"{active['lo']:.5f}-{active['hi']:.5f} ({active['touches']} touches)"
        if kind == "flip":
            zone_desc += f" [flipped to {active['flipped_to']}]"

        if active["lo"] <= close <= active["hi"]:
            self._sr_flip_state[symbol] = flips
            return self._hold_result(symbol, h1, [f"price currently inside zone {zone_desc}, no clear approach direction"])

        role = active["flipped_to"] if kind == "flip" else ("support" if close >= active["hi"] else "resistance")

        decision, score, reasons = "HOLD", 0, []

        if role == "resistance":
            if close > active["hi"] + break_frac * atr:
                decision, score = "BUY", 3
                reasons.append(f"broke resistance at {zone_desc}")
                flips.append({"lo": active["lo"], "hi": active["hi"], "touches": active["touches"], "flipped_to": "support", "flip_time": current_time})
            elif close < active["hi"]:
                decision, score = "SELL", 2
                reasons.append(f"approaching resistance at {zone_desc}, rejected")
            else:
                reasons.append(f"approaching resistance at {zone_desc}, no rejection yet")
        else:  # role == "support"
            if close < active["lo"] - break_frac * atr:
                decision, score = "SELL", 3
                reasons.append(f"broke support at {zone_desc}")
                flips.append({"lo": active["lo"], "hi": active["hi"], "touches": active["touches"], "flipped_to": "resistance", "flip_time": current_time})
            elif close > active["lo"]:
                decision, score = "BUY", 2
                reasons.append(f"approaching support at {zone_desc}, rejected")
            else:
                reasons.append(f"approaching support at {zone_desc}, no rejection yet")

        self._sr_flip_state[symbol] = flips

        if decision == "HOLD":
            return self._hold_result(symbol, h1, reasons)

        result = {"symbol": symbol, "decision": decision, "score": score, "atr": atr, "price": close, "reasons": reasons}
        log.info(f"[SR] {symbol}: {decision} (score={score}) - {'; '.join(reasons)}")
        return result

    # ------------------------------------------------------------------
    # Combo mode — runs swing + all three new strategies and only signals when
    # at least 2 of the 4 agree on the same direction.
    # ------------------------------------------------------------------

    def evaluate_combo(self, symbol: str, dfs: dict) -> dict:
        results = {
            "swing": self.evaluate(symbol, dfs),
            "divergence": self.evaluate_divergence(symbol, dfs),
            "trendline": self.evaluate_trendline(symbol, dfs),
            "sr": self.evaluate_sr(symbol, dfs),
        }

        buy_votes = [name for name, r in results.items() if r["decision"] == "BUY"]
        sell_votes = [name for name, r in results.items() if r["decision"] == "SELL"]
        tally_str = ", ".join(f"{name}={r['decision']}" for name, r in results.items())

        # strictly more votes than the other side, not just >= 2 on one side —
        # a 2-2 split is a genuine disagreement and should HOLD, not coin-flip
        if len(buy_votes) >= 2 and len(buy_votes) > len(sell_votes):
            decision, agree_count = "BUY", len(buy_votes)
        elif len(sell_votes) >= 2 and len(sell_votes) > len(buy_votes):
            decision, agree_count = "SELL", len(sell_votes)
        else:
            decision, agree_count = "HOLD", max(len(buy_votes), len(sell_votes))

        # all four evaluators read off the same H1 bar, so swing's atr/price are
        # exactly the same numbers the others computed too — no need to redo it
        atr, price = results["swing"]["atr"], results["swing"]["price"]

        if decision == "HOLD":
            log.info(f"combo: {symbol} HOLD ({tally_str}) - {agree_count}/4 agree, not enough to take a signal")
            reasons = [f"{name}: {'; '.join(r['reasons'])}" for name, r in results.items()]
            return {"symbol": symbol, "decision": "HOLD", "score": agree_count, "atr": atr, "price": price, "reasons": reasons}

        log.info(f"combo: {symbol} {decision} ({tally_str}) -> {agree_count}/4 agree, taking signal")
        winning_reasons = [f"{name}: {reason}" for name, r in results.items() if r["decision"] == decision for reason in r["reasons"]]
        score = sum(r["score"] for r in results.values() if r["decision"] == decision)

        return {"symbol": symbol, "decision": decision, "score": score, "atr": atr, "price": price, "reasons": winning_reasons}
