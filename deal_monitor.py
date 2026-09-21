import json
import os
import time
import datetime

import MetaTrader5 as mt5

from config import settings
from trade_executor import MAGIC_NUMBER
from logger_setup import get_logger

log = get_logger("deal_monitor")

STATE_FILE = "deal_monitor_state.json"


class DealMonitor:
    """MT5 closes a stop-loss server-side and never calls back into this script —
    the bot has no event for "your SL just got hit." The only way to know is to
    poll the account's deal history after the fact and check what closed a
    position. This is the live-trading equivalent of what
    TradeExecutor.check_simulated_exits() already does for dry-run positions.

    Only registers a cooldown for deals tagged with this bot's magic number and
    a DEAL_REASON_SL close — a manual close, a TP hit, or a trade from another
    EA on the same account should never cost a symbol its cooldown.

    State (last checked time + a rolling set of seen ticket numbers) persists to
    disk so a restart doesn't miss a stop-out that happened while the bot was
    down, and doesn't double-register one it already processed. First run after
    adding this feature starts its clock from "now" — it won't retroactively
    scan for SL hits that happened before this file existed."""

    def __init__(self, risk_manager):
        self.risk_manager = risk_manager
        self.last_checked_time, self.seen_tickets = self._load_state()

    def _load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                return data.get("last_checked_time", time.time()), set(data.get("seen_tickets", []))
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"Couldn't read {STATE_FILE}, starting fresh: {e}")
        return time.time(), set()

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "last_checked_time": self.last_checked_time,
                    # only keep the recent tail, this file shouldn't grow forever
                    "seen_tickets": list(self.seen_tickets)[-500:],
                }, f, indent=2)
        except OSError as e:
            log.error(f"Couldn't write {STATE_FILE}: {e}")

    def check_for_stopouts(self):
        """Call this once per loop iteration when settings.dry_run is False.
        Looks back slightly further than the last check (small overlap) since
        MT5's history sync isn't always instant, and dedupes on ticket number
        so that overlap never double-registers the same close."""
        now = time.time()
        date_from = datetime.datetime.utcfromtimestamp(self.last_checked_time - 60)
        date_to = datetime.datetime.utcfromtimestamp(now) + datetime.timedelta(minutes=1)

        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            log.warning(f"history_deals_get returned None: {mt5.last_error()}")
            self.last_checked_time = now
            self._save_state()
            return

        for deal in deals:
            if deal.ticket in self.seen_tickets:
                continue
            self.seen_tickets.add(deal.ticket)

            if deal.magic != MAGIC_NUMBER:
                continue  # not this bot's trade, don't touch cooldowns over it
            if deal.entry != mt5.DEAL_ENTRY_OUT:
                continue  # only the closing leg of a trade matters here
            if deal.reason != mt5.DEAL_REASON_SL:
                continue  # TP hits and manual/other closes don't trigger a cooldown

            cooldown = settings.scalp_cooldown_minutes if settings.strategy_mode == "scalp" else None
            self.risk_manager.register_stopout(deal.symbol, cooldown_minutes=cooldown)
            log.info(
                f"Live SL hit detected on {deal.symbol} (ticket {deal.ticket}, "
                f"profit {deal.profit:.2f}), cooldown registered"
            )

        self.last_checked_time = now
        self._save_state()
