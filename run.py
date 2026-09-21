import sys
import time
import datetime
import platform
import argparse
import os

import MetaTrader5 as mt5

from config import settings
from logger_setup import get_logger
from mt5_connector import MT5Connector
from data_fetcher import DataFetcher
from signal_engine import SignalEngine
from risk_manager import RiskManager
from trade_executor import TradeExecutor
from deal_monitor import DealMonitor
from helper import get_point_value, get_contract_size, get_spread_points

try:
    from dashboard import render_dashboard
    HAS_DASHBOARD = True
except ImportError:
    HAS_DASHBOARD = False

log = get_logger("run")
SWING_TIMEFRAMES = ["M30", "M5", "H1", "H4"]

# Maps strategy.mode -> the SignalEngine method that produces a decision for it.
# All five take the exact same (symbol, dfs) shape — dfs being the {"M30","M5",
# "H1","H4"} dict from SWING_TIMEFRAMES — so a plain dict lookup replaces what
# would otherwise be a growing if/elif chain. Scalp mode is NOT in here: it uses
# a different dfs shape ({"entry","trend"}) and stays on its own branch below.
STRATEGY_DISPATCH = {
    "swing": lambda engine, symbol, dfs: engine.evaluate(symbol, dfs),
    "divergence": lambda engine, symbol, dfs: engine.evaluate_divergence(symbol, dfs),
    "trendline": lambda engine, symbol, dfs: engine.evaluate_trendline(symbol, dfs),
    "sr": lambda engine, symbol, dfs: engine.evaluate_sr(symbol, dfs),
    "combo": lambda engine, symbol, dfs: engine.evaluate_combo(symbol, dfs),
}


def check_platform():
    if platform.system() != "Windows":
        log.warning(
            f"MetaTrader5's Python package is Windows-only. You're on {platform.system()} — "
            "this will likely fail to connect unless MT5 is reachable through Wine or a remote Windows host."
        )


def within_trading_hours():
    hour = datetime.datetime.utcnow().hour
    return settings.trading_start_hour <= hour < settings.trading_end_hour


def run_health_check():
    connector = MT5Connector()
    connected = connector.connect()
    print(f"MT5 connection: {'OK' if connected else 'FAILED'}")
    if not connected:
        return

    account = mt5.account_info()
    positions = mt5.positions_get() or []
    print(f"Balance: {account.balance:.2f}  Equity: {account.equity:.2f}")
    print(f"Open positions: {len(positions)}")

    try:
        with open("logs/trading.log") as f:
            lines = f.readlines()
        print(f"Last log entry: {lines[-1].strip() if lines else 'no entries yet'}")
    except FileNotFoundError:
        print("No log file yet — bot hasn't logged anything.")

    connector.shutdown()


def run_backtest(args):
    from backtester import Backtester
    bt = Backtester()
    start = datetime.datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.datetime.strptime(args.end, "%Y-%m-%d")

    if args.csv:
        report = bt.run(args.symbol, start, end, csv_path=args.csv)
    else:
        connector = MT5Connector()
        if not connector.connect():
            log.error("Could not connect to MT5 for backtest data.")
            return
        report = bt.run(args.symbol, start, end)
        connector.shutdown()

    print("\n--- Backtest Report ---")
    for k, v in (report or {}).items():
        print(f"{k}: {v}")


def graceful_shutdown(connector, risk, day_start_balance):
    log.info("Shutting down...")
    if settings.close_positions_on_exit:
        executor = TradeExecutor(risk)
        positions_before = mt5.positions_get() or []
        for pos in positions_before:
            executor.close_position(pos.symbol)

        # close_position() logs its own per-symbol failures, but this function was
        # previously logging "closed on exit" unconditionally regardless of whether
        # any of those calls actually succeeded — checking positions again here is
        # the only way to know the real outcome instead of assuming success.
        positions_after = mt5.positions_get() or []
        closed_count = len(positions_before) - len(positions_after)

        if positions_after:
            still_open = ", ".join(f"{p.symbol} ({p.volume} lots)" for p in positions_after)
            log.error(f"Shutdown close failed for {len(positions_after)} of {len(positions_before)} position(s), still OPEN: {still_open}")
            print(f"\nWARNING: {len(positions_after)} position(s) still open after shutdown attempt: {still_open}")
            print("These are still exposed to the market with no bot supervision. Check MT5 directly, don't assume they're flat.")
        else:
            log.info(f"All {closed_count} open position(s) confirmed closed on exit." if closed_count else "No open positions to close on exit.")
    else:
        log.info("Leaving open positions as-is (close_positions_on_exit=False).")

    balance = connector.account_balance()
    balance = balance if balance is not None else day_start_balance
    pnl_today = balance - day_start_balance
    summary = f"Session PnL: {pnl_today:.2f} (start {day_start_balance:.2f} -> end {balance:.2f})"
    log.info(summary)
    print(f"\n{summary}")
    connector.shutdown()


def main_loop(dashboard_mode):
    check_platform()
    connector = MT5Connector()
    if not connector.connect():
        log.error("Could not connect to MT5, exiting.")
        return

    # ============ SAFETY CHECKS FOR LIVE TRADING ============
    if not settings.dry_run:
        print("\n" + "="*60)
        print(" LIVE TRADING MODE ACTIVATED")
        print("="*60)
        balance = connector.account_balance() or 0
        print(f"Balance: ${balance:.2f}")
        print(f"Risk per trade: {settings.risk_per_trade*100}%")
        print(f"Max positions: {settings.max_positions}")
        print(f"Symbols: {settings.symbols}")
        print("="*60)
        print("  CONFIRM YOU WANT TO TRADE LIVE!")
        print("Press ENTER to continue, or CTRL+C to cancel")
        print("="*60)
        try:
            input()
        except KeyboardInterrupt:
            print("\nLive trading cancelled.")
            connector.shutdown()
            return
        
        # Additional safety: check if balance is reasonable
        if balance < 100:
            log.error(f"Balance too low: ${balance:.2f}. Minimum $100 recommended for live trading.")
            print(f" Balance too low: ${balance:.2f}. Minimum $100 required.")
            connector.shutdown()
            return
        
        if balance > 10000:
            print(f"\n  WARNING: Balance is ${balance:.2f}. This is a large amount!")
            print("Press ENTER to continue, or CTRL+C to cancel")
            try:
                input()
            except KeyboardInterrupt:
                print("\nLive trading cancelled.")
                connector.shutdown()
                return
        
        # DAILY LOSS LIMIT TRACKING
        daily_start_balance = balance
        MAX_DAILY_LOSS = 0.05  # 5% max loss per day
        log.info(f"Daily loss limit set to {MAX_DAILY_LOSS*100}% (${balance * MAX_DAILY_LOSS:.2f})")
    else:
        daily_start_balance = connector.account_balance() or 0
    # ========================================================

    fetcher = DataFetcher()
    engine = SignalEngine()
    risk = RiskManager()
    executor = TradeExecutor(risk)
    deal_monitor = DealMonitor(risk)  # only ever used in live mode, see below

    is_scalp = settings.strategy_mode == "scalp"
    loop_interval = settings.scalp_loop_interval_seconds if is_scalp else settings.loop_interval_seconds

    day_start_balance = connector.account_balance() or 0
    log.info(f"Bot starting. Dry run = {settings.dry_run}. Strategy mode = {settings.strategy_mode}.")

    if HAS_DASHBOARD and dashboard_mode == "clear":
        print(
            "Note: dashboard is repainting the console each cycle — open a second "
            "terminal if you want to tail logs/trading.log at the same time, or "
            "rerun with --dashboard-mode stream."
        )

    try:
        while True:
            if not connector.ping():
                log.error("Connection unrecoverable, stopping.")
                break

            # ============ DAILY LOSS LIMIT CHECK ============
            if not settings.dry_run:
                current_balance = connector.account_balance() or 0
                daily_loss_pct = (current_balance - daily_start_balance) / daily_start_balance if daily_start_balance > 0 else 0
                if daily_loss_pct < -MAX_DAILY_LOSS:
                    log.error(f" MAX DAILY LOSS REACHED: {daily_loss_pct*100:.2f}%")
                    log.error(f"Stopping trading for today! Loss: ${(daily_start_balance - current_balance):.2f}")
                    print(f"\n MAX DAILY LOSS REACHED: {daily_loss_pct*100:.2f}%")
                    print(f"Stopping trading. Loss: ${(daily_start_balance - current_balance):.2f}")
                    break
            # =================================================

            if not within_trading_hours():
                log.info("Outside trading hours, sleeping.")
                time.sleep(loop_interval)
                continue

            balance = connector.account_balance() or 0
            open_positions = mt5.positions_get() or []
            signals = {}

            # Check simulated exits in dry-run mode, or real SL hits in live mode —
            # MT5 doesn't push a notification for either, both have to be polled
            if settings.dry_run:
                executor.check_simulated_exits()
            else:
                deal_monitor.check_for_stopouts()

            for symbol in settings.symbols:
                try:
                    if is_scalp:
                        entry_df = fetcher.get_ohlcv(symbol, settings.scalp_entry_timeframe)
                        trend_df = fetcher.get_ohlcv(symbol, settings.scalp_trend_timeframe)
                        if entry_df is None or trend_df is None:
                            log.warning(f"Skipping {symbol}, incomplete scalp data")
                            continue
                        result = engine.evaluate_scalp(symbol, {"entry": entry_df, "trend": trend_df})
                    else:
                        dfs = fetcher.get_multi_timeframe(symbol, SWING_TIMEFRAMES)
                        if any(df is None for df in dfs.values()):
                            log.warning(f"Skipping {symbol}, incomplete data")
                            continue
                        dispatch_fn = STRATEGY_DISPATCH.get(settings.strategy_mode, STRATEGY_DISPATCH["swing"])
                        result = dispatch_fn(engine, symbol, dfs)

                    signals[symbol] = result

                    if result["decision"] == "HOLD":
                        continue
                    if executor.has_open_position(symbol) and not settings.allow_stacking:
                        continue
                    if risk.in_cooldown(symbol):
                        log.info(f"{symbol} in cooldown, skipping entry")
                        continue
                    if len(open_positions) >= settings.max_positions:
                        log.info("Max concurrent positions reached, skipping new entries this cycle")
                        break

                    if is_scalp:
                        spread = get_spread_points(symbol)
                        if spread is None:
                            log.warning(f"{symbol}: couldn't read spread, skipping entry")
                            continue
                        if spread > settings.scalp_max_spread_points:
                            log.info(f"{symbol}: spread {spread:.1f} pts exceeds max {settings.scalp_max_spread_points}, skipping entry")
                            continue

                    sl_mult = settings.scalp_atr_sl_multiplier if is_scalp else None
                    tp_mult = settings.scalp_atr_tp_multiplier if is_scalp else None

                    point_value = get_point_value(symbol)
                    lots = risk.position_size(balance, result["atr"], point_value, symbol)
                    sl, tp = risk.sl_tp_levels(result["price"], result["atr"], result["decision"], sl_multiplier=sl_mult, tp_multiplier=tp_mult)
                    reason = "; ".join(result["reasons"])

                    order_result = executor.send_order(symbol, result["decision"], lots, sl, tp, reason)
                    if order_result is not None:
                        open_positions = mt5.positions_get() or []

                except Exception as e:
                    log.error(f"Error processing {symbol}: {e}", exc_info=True)
                    continue

            if HAS_DASHBOARD:
                render_dashboard(signals, mt5.positions_get() or [], balance, mode=dashboard_mode)

            time.sleep(loop_interval)

    except KeyboardInterrupt:
        log.info("Manual stop requested.")
    finally:
        graceful_shutdown(connector, risk, day_start_balance)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="MT5 confluence trading bot")
    parser.add_argument("--backtest", action="store_true", help="Run a backtest instead of the live loop")
    parser.add_argument("--symbol", default="EURUSD", help="Symbol to backtest")
    parser.add_argument("--start", help="Backtest start date, YYYY-MM-DD")
    parser.add_argument("--end", help="Backtest end date, YYYY-MM-DD")
    parser.add_argument("--csv", help="Optional CSV file of OHLCV data for offline backtesting")
    parser.add_argument("--health", action="store_true", help="Run a one-shot health check and exit")
    parser.add_argument(
        "--dashboard-mode", choices=["clear", "stream"], default="clear",
        help="clear: repaint console each cycle (default). stream: append-only, tail-f style",
    )
    parser.add_argument("--reset-cooldowns", action="store_true", help="Clear all persisted cooldowns and exit")
    # ============ NEW ARGUMENTS FOR LIVE TRADING ============
    parser.add_argument("--config", default="config.yaml", help="Config file to use")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run mode (overrides .env)")
    # =========================================================
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()

    # ============ OVERRIDE CONFIG AND DRY-RUN ============
    if args.config:
        os.environ["BOT_CONFIG_PATH"] = args.config
        print(f" Using config: {args.config}")
    
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
        print(" Dry-run mode forced ON")
    # ======================================================

    if args.reset_cooldowns:
        RiskManager().reset_cooldowns()
        print("Cooldowns cleared.")
        sys.exit(0)

    if args.health:
        run_health_check()
        sys.exit(0)

    if args.backtest:
        if not args.start or not args.end:
            print("--backtest requires --start and --end (YYYY-MM-DD)")
            sys.exit(1)
        run_backtest(args)
        sys.exit(0)

    main_loop(args.dashboard_mode)