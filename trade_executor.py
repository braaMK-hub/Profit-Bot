import time
import MetaTrader5 as mt5

from config import settings
from logger_setup import get_logger
from helper import get_contract_size

log = get_logger("trade_executor")

MAGIC_NUMBER = 123456  # tags every order this bot places, so deal_monitor.py can
                        # tell "our trade closed" apart from a manual close or another EA


class TradeExecutor:
    def __init__(self, risk_manager):
        self.risk_manager = risk_manager
        self._simulated_positions = {}  # Track simulated positions in dry run mode
        self._orders = []  # Track all orders for history
        
    def has_open_position(self, symbol: str) -> bool:
        """Check if there's an open position for the symbol (real or simulated)."""
        if settings.dry_run:
            return symbol in self._simulated_positions
        
        positions = mt5.positions_get(symbol=symbol)
        return bool(positions)

    def send_order(self, symbol: str, direction: str, lots: float, sl: float, tp: float,
                    reason: str, max_retries: int = 3):
        """Send an order (simulated or real)."""
        
        if settings.dry_run:
            # Check if we already have a simulated position for this symbol
            if symbol in self._simulated_positions:
                log.info(f"[DRY RUN] Already have simulated {self._simulated_positions[symbol]['direction']} position for {symbol}, skipping duplicate order")
                return None
            
            # Get current price for simulated entry
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                log.error(f"No tick data for {symbol}, simulated order aborted")
                return None
            
            entry_price = tick.ask if direction == "BUY" else tick.bid
            
            log.info(f"[DRY RUN] SIMULATED {direction} {symbol} lots={lots} sl={sl:.5f} tp={tp:.5f} - {reason}")
            
            # Store the simulated position
            self._simulated_positions[symbol] = {
                "direction": direction,
                "lots": lots,
                "entry": entry_price,
                "sl": sl,
                "tp": tp,
                "time": time.time(),
                "reason": reason
            }
            
            # Track order in history
            self._orders.append({
                "symbol": symbol,
                "direction": direction,
                "lots": lots,
                "entry": entry_price,
                "sl": sl,
                "tp": tp,
                "reason": reason,
                "time": time.time(),
                "status": "OPEN"
            })
            
            log.info(f"[DRY RUN] Position opened for {symbol}: {direction} @ {entry_price:.5f}")
            return {"retcode": "dry_run", "symbol": symbol, "direction": direction, "entry": entry_price}

        # --- REAL ORDER EXECUTION (only if not dry run) ---
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error(f"No tick data for {symbol}, order aborted")
            return None

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if direction == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lots,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": MAGIC_NUMBER,
            "comment": "Bot trade",  # MT5 truncates comments past 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": 0,
        }

        # Exponential backoff: 1s, 2s, 4s between retries
        delay = 1
        for attempt in range(1, max_retries + 1):
            result = mt5.order_send(request)
            if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"Filled: {direction} {symbol} lots={lots} @ {request['price']} (attempt {attempt})")
                return result

            code = result.retcode if result else "no result"
            comment = result.comment if result else mt5.last_error()
            log.error(f"Order attempt {attempt}/{max_retries} failed for {symbol}: retcode={code}, comment={comment}")

            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2
                fresh_tick = mt5.symbol_info_tick(symbol)
                if fresh_tick:
                    request["price"] = fresh_tick.ask if direction == "BUY" else fresh_tick.bid

        log.error(f"Giving up on {symbol} order after {max_retries} attempts.")
        return None

    def close_position(self, symbol: str):
        """Close a position (real or simulated)."""
        
        # Close simulated position
        if settings.dry_run:
            if symbol in self._simulated_positions:
                pos = self._simulated_positions[symbol]
                log.info(f"[DRY RUN] CLOSING simulated {pos['direction']} position for {symbol} (lots={pos['lots']})")
                
                # Update order history
                for order in self._orders:
                    if order["symbol"] == symbol and order["status"] == "OPEN":
                        order["status"] = "CLOSED"
                        order["close_time"] = time.time()
                        break
                
                del self._simulated_positions[symbol]
                log.info(f"[DRY RUN] Position closed for {symbol}")
            else:
                log.warning(f"[DRY RUN] No simulated position found for {symbol}")
            return

        # Close real position
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            log.warning(f"No real position found for {symbol}")
            return

        for pos in positions:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                log.error(f"No tick data for {symbol}, cannot close position")
                continue
                
            opposite = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            price = tick.bid if opposite == mt5.ORDER_TYPE_SELL else tick.ask

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.volume,
                "type": opposite,
                "position": pos.ticket,
                "price": price,
                "deviation": 10,
                "magic": MAGIC_NUMBER,
                "comment": "manual close",
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"Closed {symbol} position at {price:.5f}")
            else:
                log.error(f"Failed to close {symbol} position: {result.comment if result else 'unknown error'}")

    def close_position_by_ticket(self, pos, comment: str = "quick profit close") -> bool:
        """Close ONE specific position by ticket, leaving any other open
        positions on the same symbol untouched.

        close_position(symbol) above closes EVERY position on a symbol -
        that's fine when risk.allow_multiple_positions_per_symbol is False
        (there's only ever one to close), but once stacking is enabled
        (multiple concurrent positions on the same symbol) it's the wrong
        tool: closing "the position on XAUUSDm that just hit its profit
        target" must not also close three other XAUUSDm positions that
        haven't gotten there yet. This is that missing per-ticket close.

        Real positions only - dry-run's _simulated_positions dict is
        one-per-symbol and doesn't model stacking (see README limitations),
        so in dry-run this falls back to close_position(symbol), which is
        exactly correct there since there's at most one position anyway."""
        symbol = pos.symbol

        if settings.dry_run:
            self.close_position(symbol)
            return True

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            log.error(f"No tick data for {symbol}, cannot close ticket {pos.ticket}")
            return False

        opposite = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if opposite == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": opposite,
            "position": pos.ticket,
            "price": price,
            "deviation": 10,
            "magic": MAGIC_NUMBER,
            "comment": comment[:31],  # MT5 truncates comments past 31 chars anyway
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"Closed {symbol} ticket {pos.ticket} at {price:.5f} (real pnl ${pos.profit:.2f})")
            return True

        log.error(f"Failed to close {symbol} ticket {pos.ticket}: {result.comment if result else mt5.last_error()}")
        return False

    def check_simulated_exits(self):
        """Check if simulated positions hit SL or TP and close them automatically."""
        if not settings.dry_run:
            return
            
        for symbol in list(self._simulated_positions.keys()):
            pos = self._simulated_positions[symbol]
            
            # Get current price
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            
            # For BUY: use bid price (exit price)
            # For SELL: use ask price (exit price)
            current_price = tick.bid if pos["direction"] == "BUY" else tick.ask
            
            # Check SL hit
            if pos["direction"] == "BUY" and current_price <= pos["sl"]:
                log.info(f"[DRY RUN] STOP LOSS hit for {symbol} (BUY @ {current_price:.5f}, SL={pos['sl']:.5f})")
                self._close_simulated_position(symbol, pos, current_price, "SL")
                
            elif pos["direction"] == "SELL" and current_price >= pos["sl"]:
                log.info(f"[DRY RUN] STOP LOSS hit for {symbol} (SELL @ {current_price:.5f}, SL={pos['sl']:.5f})")
                self._close_simulated_position(symbol, pos, current_price, "SL")
                
            # Check TP hit
            elif pos["direction"] == "BUY" and current_price >= pos["tp"]:
                log.info(f"[DRY RUN] TAKE PROFIT hit for {symbol} (BUY @ {current_price:.5f}, TP={pos['tp']:.5f})")
                self._close_simulated_position(symbol, pos, current_price, "TP")
                
            elif pos["direction"] == "SELL" and current_price <= pos["tp"]:
                log.info(f"[DRY RUN] TAKE PROFIT hit for {symbol} (SELL @ {current_price:.5f}, TP={pos['tp']:.5f})")
                self._close_simulated_position(symbol, pos, current_price, "TP")

    def _close_simulated_position(self, symbol: str, pos: dict, exit_price: float, reason: str):
        """Close a simulated position and log the result."""
        # Same fix as backtester.py's _pnl(): the 100,000 forex-lot multiplier
        # was hardcoded here regardless of instrument, which would have made
        # every dry-run XAUUSDm/BTCUSDm SL/TP PnL log wildly wrong (1000x too
        # large for gold) the moment a dry-run position actually closed.
        contract_size = get_contract_size(symbol)
        if pos["direction"] == "BUY":
            pnl = (exit_price - pos["entry"]) * pos["lots"] * contract_size
        else:
            pnl = (pos["entry"] - exit_price) * pos["lots"] * contract_size
        
        log.info(f"[DRY RUN] Position closed for {symbol}: {reason} | PnL: ${pnl:.2f} | Entry: {pos['entry']:.5f} | Exit: {exit_price:.5f}")
        
        # Update order history
        for order in self._orders:
            if order["symbol"] == symbol and order["status"] == "OPEN":
                order["status"] = reason
                order["close_time"] = time.time()
                order["exit_price"] = exit_price
                order["pnl"] = pnl
                break
        
        # Remove from simulated positions
        del self._simulated_positions[symbol]
        
        # Register stopout if SL hit
        if reason == "SL":
            cooldown = settings.scalp_cooldown_minutes if settings.strategy_mode == "scalp" else None
            self.risk_manager.register_stopout(symbol, cooldown_minutes=cooldown)

    def get_simulated_pnl(self) -> float:
        """Get total PnL from all closed simulated positions."""
        total_pnl = 0.0
        for order in self._orders:
            if order["status"] in ["SL", "TP", "CLOSED"] and "pnl" in order:
                total_pnl += order["pnl"]
        return total_pnl

    def get_open_positions(self) -> list:
        """Get list of open positions (simulated or real)."""
        if settings.dry_run:
            positions = []
            for symbol, pos in self._simulated_positions.items():
                positions.append({
                    "symbol": symbol,
                    "direction": pos["direction"],
                    "lots": pos["lots"],
                    "entry": pos["entry"],
                    "sl": pos["sl"],
                    "tp": pos["tp"],
                    "type": 0 if pos["direction"] == "BUY" else 1  # Match MT5 format
                })
            return positions
        
        return mt5.positions_get() or []