import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from config import settings
from signal_engine import SignalEngine
from risk_manager import RiskManager
from logger_setup import get_logger
from helper import get_contract_size, ensure_symbol_visible

log = get_logger("backtester")

WARMUP_BARS = 200


class Backtester:
    def __init__(self, engine=None):
        self.engine = engine or SignalEngine()
        self.risk = RiskManager()
        self.last_trades = []  # populated by run(); read by replay.py's memory system

    def fetch_history(self, symbol, timeframe_label, start_date, end_date):
        from data_fetcher import TIMEFRAME_MAP
        tf = TIMEFRAME_MAP[timeframe_label]

        # Live trading (data_fetcher.py) always calls this before fetching —
        # copy_rates_range() can silently come back empty for a symbol that
        # isn't yet selected in Market Watch. This was missing here, which is
        # the likely cause of "Not enough historical data. Got 0 bars" for a
        # symbol MT5 hasn't been asked to show yet in this session.
        if not ensure_symbol_visible(symbol):
            log.warning(f"Symbol {symbol} not available on this broker — check the exact name in Market Watch")
            return None

        rates = mt5.copy_rates_range(symbol, tf, start_date, end_date)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def load_csv(self, path):
        df = pd.read_csv(path, parse_dates=["time"], index_col="time")
        return df[["open", "high", "low", "close", "volume"]]

    def run(self, symbol, start_date, end_date, csv_path=None, initial_balance=10000.0):
        if csv_path:
            h1_raw = self.load_csv(csv_path)
            h4_raw = (
                h1_raw.resample("4h")
                .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                .dropna()
            )
        else:
            h1_raw = self.fetch_history(symbol, "H1", start_date, end_date)
            h4_raw = self.fetch_history(symbol, "H4", start_date, end_date)

        if h1_raw is None or h4_raw is None or len(h1_raw) < WARMUP_BARS + 10:
            log.error(f"Not enough historical data. Got {len(h1_raw) if h1_raw is not None else 0} bars, need {WARMUP_BARS + 10}.")
            return None

        log.info(f"Raw data: {len(h1_raw)} bars fetched for {symbol}")

        mode = settings.strategy_mode
        multi_strategy_modes = {"divergence", "trendline", "sr", "combo"}
        is_multi_strategy = mode in multi_strategy_modes

        if is_multi_strategy:
            log.info(
                f"Backtesting strategy mode '{mode}' by replaying SignalEngine's real "
                f"evaluate_{mode}() bar-by-bar on a growing raw-OHLCV slice — same code "
                f"path as live trading, no shortcuts. That means indicators get "
                f"recomputed from scratch on every bar instead of once on the full "
                f"series, so this runs noticeably slower than swing-mode backtesting. "
                f"Expect it to take a while on a long date range."
            )
            # still need atr/close/ema50 per bar for the fill/SL/TP model and the
            # periodic sample logging below. Computing this once on the full series
            # is fine here — unlike the swing decision logic's precompute (see the
            # class docstring), EMA/RSI/ATR/ADX are backward-looking-only formulas,
            # so the value at bar i is identical whether computed on the full
            # series or on a slice ending at i. The thing that genuinely needs a
            # bar-by-bar slice is swing detection (_find_swings looks `order`
            # bars into the future to confirm a pivot) — which is exactly what
            # _decide_multistrategy below does by only ever handing the evaluator
            # data up to and including the current bar.
            h1 = self.engine.add_indicators(h1_raw)
        else:
            h1 = self.engine.add_indicators(h1_raw)
            h4 = self.engine.add_indicators(h4_raw)
            h4 = h4.copy()
            h4["h4_trend"] = np.where(
                h4["close"] > h4["ema100"], "bullish",
                np.where(h4["close"] < h4["ema100"], "bearish", "neutral"),
            )
            h1 = pd.merge_asof(
                h1.sort_index(), h4[["h4_trend"]].sort_index(),
                left_index=True, right_index=True, direction="backward",
            )

        log.info(f"Data shape after indicators: {h1.shape}")

        # Show a sample of bars
        sample = h1.iloc[WARMUP_BARS:WARMUP_BARS+5]
        for idx, row in sample.iterrows():
            log.info(f"Sample bar: close={row['close']:.5f}, ema50={row['ema50']:.5f}")

        balance = initial_balance
        equity_curve = [balance]
        trades = []
        open_trade = None

        signal_count = 0
        decision_count = 0

        for i in range(WARMUP_BARS, len(h1) - 1):
            row = h1.iloc[i]
            next_row = h1.iloc[i + 1]

            if open_trade:
                hit_sl, hit_tp = self._check_exit(row, open_trade)
                if hit_sl or hit_tp:
                    exit_price = open_trade["sl"] if hit_sl else open_trade["tp"]
                    pnl = self._pnl(open_trade, exit_price)
                    balance += pnl
                    trades.append({
                        "direction": open_trade["direction"],
                        "entry": open_trade["entry"],
                        "exit": exit_price,
                        "entry_time": open_trade["entry_time"],
                        "exit_time": row.name,
                        "pnl": pnl,
                        "result": "SL" if hit_sl else "TP",
                        "symbol": open_trade["symbol"],
                        "reason": open_trade.get("reason", "unspecified setup"),
                        "lots": open_trade["lots"],
                    })
                    if hit_sl:
                        self.risk.register_stopout(symbol)
                    open_trade = None

            # --- REMOVED cooldown check for backtesting ---
            if not open_trade:
                if is_multi_strategy:
                    decision, reason = self._decide_multistrategy(symbol, h1_raw, h4_raw, i, mode)
                else:
                    decision, reason = self._decide(h1, i)
                decision_count += 1
                if decision in ("BUY", "SELL"):
                    signal_count += 1
                    log.info(f"SIGNAL #{signal_count} at bar {i}: {decision}")
                    entry = next_row["open"]
                    atr = row["atr"]
                    sl, tp = self.risk.sl_tp_levels(entry, atr, decision)
                    open_trade = {
                        "direction": decision, "entry": entry, "sl": sl, "tp": tp,
                        "entry_time": next_row.name, "lots": 0.1, "symbol": symbol,
                        "reason": reason,
                    }

            equity_curve.append(balance)

            # Log every 500 bars
            if i % 500 == 0:
                log.info(f"Bar {i}: close={row['close']:.5f}, ema50={row['ema50']:.5f}, adx={row['adx']:.2f}")

        log.info(f"Total decisions made: {decision_count}")
        log.info(f"Total signals generated: {signal_count}")
        log.info(f"Total trades executed: {len(trades)}")
        self.last_trades = trades  # exposed for replay.py's memory system to read real outcomes from
        return self._report(trades, equity_curve, initial_balance)

    def _decide_multistrategy(self, symbol, h1_raw, h4_raw, i, mode):
        """Bar-by-bar replay for the new evaluators (divergence/trendline/sr/combo).
        Slices raw OHLCV up to and including bar i and calls the actual
        SignalEngine method — the same one live trading calls — rather than a
        separate hand-rolled approximation like swing's _decide() below. This is
        what makes the swing-detection-based strategies (trendline, sr) correct
        here: a swing near the tail of the slice genuinely can't be confirmed yet
        with only order=3 lookback bars available, exactly like it couldn't be in
        real time. The cost is real: this recomputes indicators from scratch on
        an ever-growing slice every single bar, so a multi-strategy backtest over
        a long date range takes a while — that's a deliberate accuracy-over-speed
        trade-off, not an oversight.

        self.engine is the SAME SignalEngine instance across the whole backtest
        loop, so evaluate_sr's zone-flip memory persists correctly across bars
        here too, not just in live trading.

        Returns (decision, reason) — reason is the real "; ".join(reasons) the
        evaluator itself already produced, not a separate invented summary."""
        h1_slice = h1_raw.iloc[:i + 1]
        ts = h1_raw.index[i]
        h4_slice = h4_raw[h4_raw.index <= ts]
        if len(h4_slice) < 5:
            return "HOLD", "not enough H4 history yet"  # not enough H4 history yet for combo's swing sub-vote to use

        dfs = {"H1": h1_slice, "H4": h4_slice}

        if mode == "divergence":
            result = self.engine.evaluate_divergence(symbol, dfs)
        elif mode == "trendline":
            result = self.engine.evaluate_trendline(symbol, dfs)
        elif mode == "sr":
            result = self.engine.evaluate_sr(symbol, dfs)
        elif mode == "combo":
            result = self.engine.evaluate_combo(symbol, dfs)
        else:
            result = self.engine.evaluate(symbol, dfs)  # shouldn't happen, falls back to swing

        return result["decision"], "; ".join(result.get("reasons", [])) or f"{mode} signal"

    def _decide(self, h1, i):
        """Super simple: price vs EMA50.

        Returns (decision, reason). The decision logic itself is unchanged —
        reason is a plain-English snapshot of the same booleans this method
        already computes, added so replay.py's memory system has something
        real to key a "setup" on. Nothing here is invented after the fact:
        every clause in the reason string maps to a condition that was
        actually just evaluated above it."""
        last = h1.iloc[i]
        prev = h1.iloc[i - 1]

        macd_rising = last["macd_hist"] > prev["macd_hist"]
        macd_falling = last["macd_hist"] < prev["macd_hist"]

        price_above_ema = last["close"] > last["ema50"]
        price_below_ema = last["close"] < last["ema50"]
        strong_trend = last["adx"] > 20
        not_overbought = last["rsi"] < 70
        not_oversold = last["rsi"] > 30
        h4_bullish = last["h4_trend"] == "bullish"
        h4_bearish = last["h4_trend"] == "bearish"
        #neutral_rsi = 40 < last["rsi"] < 60

        if price_above_ema and strong_trend and macd_rising and not_overbought and (h4_bullish or last["h4_trend"] == "neutral"):
            reason = f"EMA50 bullish crossover, ADX {last['adx']:.1f} trending, MACD rising, RSI {last['rsi']:.1f}"
            return "BUY", reason
        elif price_below_ema and strong_trend and macd_falling and not_oversold and (h4_bearish or last["h4_trend"] == "neutral"):
            reason = f"EMA50 bearish crossover, ADX {last['adx']:.1f} trending, MACD falling, RSI {last['rsi']:.1f}"
            return "SELL", reason
        return "HOLD", "no crossover confluence"
        
    def _check_exit(self, row, trade):
        if trade["direction"] == "BUY":
            hit_sl = row["low"] <= trade["sl"]
            hit_tp = row["high"] >= trade["tp"]
        else:
            hit_sl = row["high"] >= trade["sl"]
            hit_tp = row["low"] <= trade["tp"]
        if hit_sl and hit_tp:
            hit_tp = False
        return hit_sl, hit_tp

    def _pnl(self, trade, exit_price):
        """PnL = price move x lots x contract size. The forex-standard
        100,000 units/lot was previously hardcoded here regardless of
        instrument — correct for EURUSD-style pairs, but wrong by 1000x for
        gold (100 oz/lot) and by 100,000x for BTC (1 unit/lot), which is
        exactly what was producing impossible results like a -700% max
        drawdown on XAUUSDm. get_contract_size() now supplies the right
        multiplier per symbol, the same one live trading already uses via
        helper.py, restoring apples-to-apples comparability between
        symbols. This still doesn't require a live MT5 connection to be
        correct for gold/silver/oil/crypto — see the reordering in
        helper.get_contract_size()."""
        contract_size = get_contract_size(trade["symbol"])
        move = (exit_price - trade["entry"]) if trade["direction"] == "BUY" else (trade["entry"] - exit_price)
        return move * trade["lots"] * contract_size

    def _report(self, trades, equity_curve, initial_balance):
        if not trades:
            log.warning("No trades generated during backtest window.")
            return {
                "total_trades": 0, "total_return_pct": 0, "sharpe_ratio": 0,
                "max_drawdown_pct": 0, "win_rate_pct": 0, "profit_factor": 0,
                "avg_trade_duration": "n/a", "final_balance": initial_balance,
            }

        df = pd.DataFrame(trades)
        final_balance = initial_balance + df["pnl"].sum()
        total_return_pct = (final_balance - initial_balance) / initial_balance * 100

        equity = pd.Series(equity_curve)
        returns = equity.pct_change().dropna()
        sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0

        running_max = equity.cummax()
        drawdown = (equity - running_max) / running_max
        max_drawdown_pct = drawdown.min() * 100

        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]
        win_rate_pct = len(wins) / len(df) * 100
        gross_profit = wins["pnl"].sum()
        gross_loss = abs(losses["pnl"].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        df["duration"] = pd.to_datetime(df["exit_time"]) - pd.to_datetime(df["entry_time"])

        report = {
            "total_trades": len(df),
            "total_return_pct": round(total_return_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "win_rate_pct": round(win_rate_pct, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "avg_trade_duration": str(df["duration"].mean()),
            "final_balance": round(final_balance, 2),
        }
        log.info(f"Backtest report: {report}")
        return report