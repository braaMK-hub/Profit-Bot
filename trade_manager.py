"""Active trade management: move a live position's stop loss to breakeven
once price has moved a configurable distance in its favour, then trail it
behind price at an ATR-scaled distance.

Both uploaded gold-trading guides spend more of their worked examples on
managing an open trade than on picking the entry — every single example in
UK Gold Trading Experts' guide moves the stop to breakeven and then trails
it in stages. This bot's risk_manager.py currently sets SL/TP once at entry
and never revisits it; this module is the missing piece.

Live positions only. A dry-run simulated position's SL/TP is already
polled every loop by TradeExecutor.check_simulated_exits() — this module
only ever calls mt5.order_send(TRADE_ACTION_SLTP) against real broker
positions, and manage_open_positions() is a no-op whenever settings.dry_run
is True.

No fake state, no simulated fills: every distance here is computed off the
position's real MT5 fields (price_open, price_current, sl) and the
symbol's real current ATR, and a stop is only ever moved via a real
order_send call whose result is checked before assuming it worked.
"""

import MetaTrader5 as mt5

from config import settings
from logger_setup import get_logger

log = get_logger("trade_manager")


class TradeManager:
    def __init__(self, executor=None):
        # Per-ticket bookkeeping: once a position has been moved to
        # breakeven, we don't want to keep re-deciding that from scratch
        # every loop off potentially noisy price ticks. MT5 is still queried
        # fresh every call for the position's actual current sl/price — this
        # set only remembers WHETHER breakeven has already been done for a
        # ticket, not any state that could go stale.
        self._breakeven_done = set()
        # Needed for "quick_profit" mode, which closes a specific ticket via
        # executor.close_position_by_ticket() the instant it hits its target.
        # Not needed for the default "trail" mode (that only moves SL/TP,
        # never closes anything itself).
        self.executor = executor

    def manage_open_positions(self, atr_by_symbol: dict):
        """Call once per loop, live mode only. atr_by_symbol = {symbol: atr}
        for whatever symbols the bot evaluated this cycle. Trailing distance
        scales with the symbol's own current ATR — the same convention
        every other distance in this codebase already uses (SL/TP sizing,
        S/R zones, Fibonacci tolerance) — rather than a fixed pip amount
        that would be meaningless across instruments as different as
        XAUUSDm and BTCUSDm.

        settings.trade_mgmt_mode picks which exit style runs:
          "trail" (default) - the breakeven-then-trailing-stop logic below,
            unchanged from before.
          "quick_profit" - close a position outright the instant its real
            floating profit reaches settings.trade_mgmt_quick_profit_usd,
            instead of trailing anything. See _manage_quick_profit()."""
        if settings.dry_run or not settings.trade_mgmt_enabled:
            return

        positions = mt5.positions_get() or []

        if settings.trade_mgmt_mode == "quick_profit":
            self._manage_quick_profit(positions)
            return

        for pos in positions:
            atr = atr_by_symbol.get(pos.symbol)
            if atr is None or atr <= 0:
                continue  # no fresh ATR this cycle (symbol wasn't evaluated) — skip, don't guess
            self._manage_one(pos, atr)

    def _manage_one(self, pos, atr: float):
        is_buy = pos.type == mt5.ORDER_TYPE_BUY
        entry = pos.price_open
        current = pos.price_current
        current_sl = pos.sl

        favorable_move = (current - entry) if is_buy else (entry - current)
        if favorable_move <= 0:
            return  # trade isn't in profit yet, nothing to protect

        breakeven_trigger = settings.trade_mgmt_breakeven_atr_multiplier * atr
        trail_start_trigger = settings.trade_mgmt_trail_start_atr_multiplier * atr
        trail_distance = settings.trade_mgmt_trail_distance_atr_multiplier * atr

        # Stage 1: move to breakeven (+ a small buffer so it isn't an exact
        # wash after spread/commission) once price has moved far enough in
        # our favour. Only ever moves the stop TOWARD breakeven.
        if pos.ticket not in self._breakeven_done and favorable_move >= breakeven_trigger:
            buffer = settings.trade_mgmt_breakeven_buffer_atr_fraction * atr
            new_sl = entry + buffer if is_buy else entry - buffer
            if self._is_improvement(is_buy, new_sl, current_sl):
                if self._modify_sl(pos, new_sl):
                    self._breakeven_done.add(pos.ticket)
                    current_sl = new_sl
                    log.info(f"{pos.symbol} ticket {pos.ticket}: moved SL to breakeven+buffer ({new_sl:.5f})")
            else:
                # current stop is already at or past this point somehow (e.g.
                # a tighter manual stop) — nothing to do, but don't keep
                # re-attempting every loop
                self._breakeven_done.add(pos.ticket)

        # Stage 2: once price has moved further still, trail the stop behind
        # price at a fixed ATR distance. _is_improvement() below guarantees
        # this only ever tightens the stop, never loosens it back out — a
        # pullback can't undo protection that's already been locked in.
        if favorable_move >= trail_start_trigger:
            new_sl = current - trail_distance if is_buy else current + trail_distance
            if self._is_improvement(is_buy, new_sl, current_sl):
                if self._modify_sl(pos, new_sl):
                    log.info(f"{pos.symbol} ticket {pos.ticket}: trailed SL to {new_sl:.5f} (price {current:.5f})")

    def _is_improvement(self, is_buy: bool, new_sl: float, current_sl: float) -> bool:
        """A BUY's stop only ever moves up; a SELL's stop only ever moves
        down. current_sl of 0.0 means no stop is currently set on the
        position (shouldn't normally happen since TradeExecutor always sets
        one on entry, but treated defensively as "any real stop is an
        improvement over none")."""
        if current_sl == 0.0:
            return True
        return new_sl > current_sl if is_buy else new_sl < current_sl

    def _modify_sl(self, pos, new_sl: float) -> bool:
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "sl": round(new_sl, 5),
            "tp": pos.tp,  # leave the existing take-profit untouched
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True

        code = result.retcode if result else "no result"
        comment = result.comment if result else mt5.last_error()
        log.error(f"Failed to modify SL for {pos.symbol} ticket {pos.ticket}: retcode={code}, comment={comment}")
        return False

    def _manage_quick_profit(self, positions):
        """Close any position the instant its real floating profit reaches
        settings.trade_mgmt_quick_profit_usd, leaving every other open
        position (including other stacked positions on the same symbol)
        untouched.

        Uses MT5's own pos.profit field directly - that's the broker's own
        authoritative $ P&L for the position, already correct for contract
        size, currency, and swap, with no manual recomputation needed (the
        same category of bug fixed earlier in backtester.py/trade_executor.py,
        where a hardcoded forex-lot multiplier was used for every symbol -
        reading the real number from MT5 sidesteps that whole class of bug).

        This is a genuinely different exit philosophy from "trail" mode: it
        takes small wins immediately rather than trying to ride a bigger
        one, and unlike trailing it can and does fully close positions,
        every loop, as fast as settings.runtime.loop_interval_seconds
        allows. It never touches SL - the stop-loss set at entry by
        RiskManager remains the only thing capping the downside on a
        position that hasn't reached quick_profit_usd yet."""
        threshold = settings.trade_mgmt_quick_profit_usd
        if self.executor is None:
            log.error("quick_profit mode is on but TradeManager has no executor reference - cannot close positions")
            return

        for pos in positions:
            if pos.profit >= threshold:
                log.info(f"{pos.symbol} ticket {pos.ticket}: quick-profit hit (${pos.profit:.2f} >= ${threshold:.2f}), closing")
                self.executor.close_position_by_ticket(pos, comment="quick profit")
                self.forget(pos.ticket)

    def forget(self, ticket: int):
        """Call when a position closes (SL/TP hit, manual close, etc.) so
        that if MT5 ever reuses a ticket number for a brand new position on
        the same symbol, it doesn't inherit stale breakeven-done state from
        the old one. Not currently wired into run.py's shutdown/close paths
        — optional cleanup, not required for correctness since a stale
        ticket in this set just means a future coincidentally-same-numbered
        position skips re-triggering an already-done breakeven move, which
        is harmless."""
        self._breakeven_done.discard(ticket)