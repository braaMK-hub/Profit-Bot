import time
import MetaTrader5 as mt5

from config import settings
from logger_setup import get_logger

log = get_logger("mt5_connector")


class MT5Connector:
    """Nothing else in the codebase should call mt5.initialize()/login() directly.
    Keeping the connection logic in one place makes reconnects predictable."""

    def __init__(self):
        self.connected = False

    def connect(self):
        init_kwargs = {"path": settings.mt5_path} if settings.mt5_path else {}

        if not mt5.initialize(**init_kwargs):
            log.error(f"initialize() failed: {mt5.last_error()}")
            return False

        authorized = mt5.login(
            settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
        )
        if not authorized:
            log.error(f"login() failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

        self.connected = True
        log.info(f"Connected as {settings.mt5_login} on {settings.mt5_server}")
        return True

    def ping(self):
        # account_info() is a cheap round trip, good enough as a heartbeat check
        if mt5.account_info() is None:
            log.warning("Ping failed, terminal not responding. Reconnecting.")
            self.connected = False
            return self.reconnect()
        return True

    def reconnect(self, retries=5, delay=5):
        for attempt in range(1, retries + 1):
            log.info(f"Reconnect attempt {attempt}/{retries}")
            mt5.shutdown()
            time.sleep(delay)
            if self.connect():
                return True
        log.error("Out of reconnect attempts, giving up.")
        return False

    def shutdown(self):
        mt5.shutdown()
        self.connected = False
        log.info("MT5 connection closed.")

    def account_balance(self):
        info = mt5.account_info()
        return info.balance if info else None
