"""Two-mode replay harness for the swing/crossover strategy.

    python replay.py --mode raw    --symbol XAUUSDm --start 2024-01-01 --end 2024-06-01
    python replay.py --mode memory --symbol XAUUSDm --start 2024-01-01 --end 2024-06-01
    python replay.py --reset-memory

(Equivalent of the requested `npm run replay:raw`, `npm run replay:memory`,
`npm run memory:reset` — this project is Python/MT5, not Node, so these are
CLI flags on one script rather than npm scripts. Same behavior, same two
data files, same CSV header.)

Memory quality rules enforced here:
  - --mode raw NEVER consults memory. It is always the same honest,
    unfiltered baseline Backtester.run() already computes — nothing about
    the strategy's own decision logic changes based on mode.
  - --mode raw, after finishing, appends every REAL trade outcome it
    produced to data/ledger.csv, and writes a plain-English lesson to
    data/learnings.md for any REAL losing trade whose setup isn't already
    documented. If it finds no losses, it writes nothing — no seeding.
  - --mode memory never blocks a trade on a guess. Only a real ledger loss
    or a real learnings.md entry (both written by a previous --mode raw
    run) can turn a would-be BUY/SELL into a skip. With an empty ledger it
    says so explicitly and tells you to run --mode raw first, per spec,
    rather than silently behaving like raw or inventing a warning.
  - No invented candles: both modes pull the exact same real historical
    OHLCV data through Backtester (MT5 or a real --csv file) — replay.py
    never synthesizes bars to manufacture a losing setup.
"""

import argparse
import datetime as dt

import memory
import adaptive_filter
from backtester import Backtester
from mt5_connector import MT5Connector
from logger_setup import get_logger

log = get_logger("replay")


def _run_backtest(symbol, start, end, csv_path=None):
    bt = Backtester()
    if csv_path:
        report = bt.run(symbol, start, end, csv_path=csv_path)
        return bt, report

    connector = MT5Connector()
    if not connector.connect():
        raise SystemExit("Could not connect to MT5 for replay data.")
    report = bt.run(symbol, start, end)
    connector.shutdown()
    return bt, report


def replay_raw(symbol, start, end, csv_path=None):
    print(f"\n=== replay:raw - {symbol} {start.date()} to {end.date()} (memory ignored) ===")
    bt, report = _run_backtest(symbol, start, end, csv_path)

    if not report or report.get("total_trades", 0) == 0:
        print("No trades generated this run - nothing real to record to memory.")
        return report

    new_lessons = 0
    for trade in bt.last_trades:
        outcome = "win" if trade["pnl"] > 0 else "loss"
        memory.append_ledger_row(
            symbol=trade["symbol"], action=trade["direction"], price=trade["entry"],
            quantity=trade.get("lots", 0.1), reason=trade.get("reason", "unspecified setup"),
            mode="raw", outcome=outcome, pnl=trade["pnl"],
        )
        if outcome == "loss":
            wrote = memory.append_learning(
                trade["symbol"], trade["direction"], trade.get("reason", "unspecified setup"),
                trade["pnl"], trade["entry_time"], trade["exit_time"],
            )
            new_lessons += int(wrote)

    print(f"Recorded {len(bt.last_trades)} real trade outcome(s) to data/ledger.csv")
    if new_lessons:
        print(f"Wrote {new_lessons} new lesson(s) to data/learnings.md")
    else:
        print("No new lessons written - no undocumented losing setups found this run")

    print("\n--- Raw Backtest Report ---")
    for k, v in report.items():
        print(f"{k}: {v}")
    return report


def replay_memory(symbol, start, end, csv_path=None):
    print(f"\n=== replay:memory - {symbol} {start.date()} to {end.date()} ===")

    ledger_rows = memory.load_ledger()
    if not ledger_rows:
        print("No memory recorded yet for any symbol/setup.")
        print("Run `python replay.py --mode raw` first to build real trade history before memory can help.")
        print("Proceeding to show what the raw strategy would have done (nothing to filter against yet):\n")

    bt, report = _run_backtest(symbol, start, end, csv_path)

    if not report or report.get("total_trades", 0) == 0:
        print("No trades generated this run.")
        return report

    allowed, skipped = 0, 0
    for trade in bt.last_trades:
        check = adaptive_filter.check_against_memory(trade["symbol"], trade["direction"], trade.get("reason", ""))
        if check["allow"]:
            allowed += 1
            if check["cold"]:
                log.info(f"{trade['symbol']} {trade['direction']} at {trade['entry_time']}: {check['note']}")
        else:
            skipped += 1
            log.info(f"{trade['symbol']} {trade['direction']} at {trade['entry_time']}: {check['note']}")

    print(f"\nCompared against raw: {allowed} of {allowed + skipped} raw trades would have been allowed through "
          f"memory; {skipped} would have been skipped on a real prior warning.")

    print("\n--- Raw Backtest Report (for reference; memory only flags which entries it would "
          "have skipped, it does not yet re-simulate the equity curve with those entries removed) ---")
    for k, v in report.items():
        print(f"{k}: {v}")

    if skipped == 0 and ledger_rows:
        print("\nMemory had real history for this symbol but found nothing to warn about - all "
              "entries would have gone through unchanged.")
    return report


def main():
    parser = argparse.ArgumentParser(description="Two-mode (raw vs memory) replay harness for the swing strategy")
    parser.add_argument("--mode", choices=["raw", "memory"], help="raw: ignore memory (baseline). memory: check entries against data/ledger.csv + data/learnings.md")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD, defaults to 180 days before --end")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD, defaults to today (UTC)")
    parser.add_argument("--csv", default=None, help="Optional CSV file instead of live MT5 history")
    parser.add_argument("--reset-memory", action="store_true", help="Wipe data/ledger.csv and data/learnings.md back to empty (equivalent of npm run memory:reset)")
    args = parser.parse_args()

    if args.reset_memory:
        memory.reset_memory()
        print("Memory reset: data/ledger.csv truncated to header only, data/learnings.md cleared. Nothing reseeded.")
        return

    if not args.mode:
        parser.error("--mode raw|memory is required (or pass --reset-memory on its own)")

    end = dt.datetime.strptime(args.end, "%Y-%m-%d") if args.end else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    start = dt.datetime.strptime(args.start, "%Y-%m-%d") if args.start else end - dt.timedelta(days=180)

    if args.mode == "raw":
        replay_raw(args.symbol, start, end, args.csv)
    else:
        replay_memory(args.symbol, start, end, args.csv)


if __name__ == "__main__":
    main()