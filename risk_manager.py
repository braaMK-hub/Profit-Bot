import json
import os
import time
import datetime

from config import settings
from logger_setup import get_logger

log = get_logger("risk_manager")

COOLDOWN_FILE = "cooldowns.json"


class RiskManager:
    def __init__(self):
        self.cooldown_until = self._load()

    def _load(self):
        if os.path.exists(COOLDOWN_FILE):
            try:
                with open(COOLDOWN_FILE, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"Couldn't read {COOLDOWN_FILE}, starting with no cooldowns: {e}")
        return {}

    def _save(self):
        try:
            with open(COOLDOWN_FILE, "w") as f:
                json.dump(self.cooldown_until, f, indent=2)
        except OSError as e:
            log.error(f"Couldn't write {COOLDOWN_FILE}: {e}")

    def position_size(self, balance: float, atr: float, point_value: float, symbol: str = None) -> float:
        """Fixed fractional sizing: risk_per_trade% of balance divided by the dollar
        distance to stop loss. point_value = $ value of one price unit per lot."""
        risk_amount = balance * settings.risk_per_trade
        sl_distance = atr * settings.atr_sl_multiplier

        if sl_distance <= 0 or point_value <= 0:
            log.warning("Bad sl_distance or point_value, falling back to minimum lot size")
            return 0.01

        # Import helpers here to avoid circular imports
        from helper import get_contract_size
        
        contract_size = get_contract_size(symbol) if symbol else 100000
        
        # Calculate lots
        lots = risk_amount / (sl_distance * point_value)
        lots = round(max(lots, 0.01), 2)
        
        log.info(f"Position size: {lots} lots for {symbol} (risk: ${risk_amount:.2f}, SL distance: {sl_distance:.5f})")
        return lots

    def sl_tp_levels(self, entry_price: float, atr: float, direction: str, sl_multiplier: float = None, tp_multiplier: float = None):
        sl_mult = sl_multiplier if sl_multiplier is not None else settings.atr_sl_multiplier
        tp_mult = tp_multiplier if tp_multiplier is not None else settings.atr_tp_multiplier
        sl_dist = atr * sl_mult
        tp_dist = atr * tp_mult
        if direction == "BUY":
            return entry_price - sl_dist, entry_price + tp_dist
        return entry_price + sl_dist, entry_price - tp_dist

    def in_cooldown(self, symbol: str) -> bool:
        expiry = self.cooldown_until.get(symbol)
        if expiry is None:
            return False
        if time.time() >= expiry:
            del self.cooldown_until[symbol]
            self._save()
            return False
        return True

    def register_stopout(self, symbol: str, cooldown_minutes: int = None):
        minutes = cooldown_minutes if cooldown_minutes is not None else settings.cooldown_minutes
        expiry = time.time() + minutes * 60
        self.cooldown_until[symbol] = expiry
        self._save()
        log.info(f"{symbol} stopped out, cooldown until {datetime.datetime.fromtimestamp(expiry)}")

    def reset_cooldowns(self, symbol: str = None):
        if symbol:
            self.cooldown_until.pop(symbol, None)
            log.info(f"Cooldown cleared for {symbol}")
        else:
            self.cooldown_until = {}
            log.info("All cooldowns cleared")
        self._save()