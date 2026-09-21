import argparse

import MetaTrader5 as mt5

from config import settings
from signal_engine import SignalEngine
from data_fetcher import DataFetcher

parser = argparse.ArgumentParser(description="Print the current signal for every configured symbol")
parser.add_argument(
    "--mode", choices=["swing", "divergence", "trendline", "sr", "combo"], default=None,
    help="Strategy to evaluate. Defaults to whatever strategy.mode is set to in config.yaml",
)
args = parser.parse_args()

mode = args.mode or settings.strategy_mode
if mode == "scalp":
    print("check_signals.py doesn't support scalp mode (different timeframes/data shape) - use dry-run for that instead.")
    raise SystemExit(1)

mt5.initialize()

fetcher = DataFetcher(bars=300)
engine = SignalEngine()

DISPATCH = {
    "swing": engine.evaluate,
    "divergence": engine.evaluate_divergence,
    "trendline": engine.evaluate_trendline,
    "sr": engine.evaluate_sr,
    "combo": engine.evaluate_combo,
}

for symbol in settings.symbols:
    print(f"\n{'='*50}")
    print(f"Analyzing {symbol} - mode: {mode}")
    print('='*50)

    try:
        # Fetch data
        dfs = fetcher.get_multi_timeframe(symbol, ["M5", "H1", "H4"])
        if any(df is None for df in dfs.values()):
            print(f" No data for {symbol}")
            continue

        if mode == "combo":
            # show each sub-strategy's own call, not just the combined verdict -
            # that per-strategy visibility is the whole point of this being a
            # diagnostic tool rather than just reading the log
            for name in ("swing", "divergence", "trendline", "sr"):
                sub = DISPATCH[name](symbol, dfs)
                print(f"\n  [{name}] {sub['decision']} (score={sub['score']})")
                for reason in sub["reasons"]:
                    print(f"    - {reason}")
            print()

        result = DISPATCH[mode](symbol, dfs)

        print(f"Decision: {result['decision']}")
        print(f"Score: {result['score']}")
        print(f"Reasons:")
        for reason in result['reasons']:
            print(f"  - {reason}")

        if mode == "swing":
            # Check individual conditions — evaluate() computes indicators on its own
            # internal copy of the dataframes and never hands that back, so the debug
            # printout has to compute them again here to actually have columns to read
            h1 = engine.add_indicators(dfs["H1"])
            last = h1.iloc[-1]
            prev = h1.iloc[-2]

            print(f"\n Current Conditions:")
            print(f"  ADX: {last['adx']:.2f} (Threshold: {settings.adx_trend_threshold})")
            print(f"  RSI: {last['rsi']:.2f} (BUY < {settings.rsi_buy_ceiling}, SELL > {settings.rsi_sell_floor})")
            print(f"  Price: {last['close']:.5f}")
            print(f"  EMA50: {last['ema50']:.5f}")
            print(f"  Price vs EMA50: {'ABOVE' if last['close'] > last['ema50'] else 'BELOW'}")
            print(f"  MACD Hist: {last['macd_hist']:.5f} vs Prev: {prev['macd_hist']:.5f}")
            print(f"  MACD: {'RISING' if last['macd_hist'] > prev['macd_hist'] else 'FALLING'}")

            # Check H4 trend the same way evaluate() actually does it (via h4_bias,
            # comparing close to ema100), not by re-deriving it here
            h4_trend = engine.h4_bias(dfs["H4"])
            print(f"  H4 Trend: {h4_trend.upper()}")

    except Exception as e:
        print(f" Error: {e}")

mt5.shutdown()
