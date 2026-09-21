import pandas as pd
import MetaTrader5 as mt5

from config import settings
from logger_setup import get_logger
from helper import ensure_symbol_visible

log = get_logger("data_fetcher")

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}


class DataFetcher:
    def __init__(self, bars=None):
        self.bars = bars or settings.bars_to_fetch

    def get_ohlcv(self, symbol, timeframe_label):
        tf = TIMEFRAME_MAP.get(timeframe_label)
        if tf is None:
            raise ValueError(f"Unknown timeframe label: {timeframe_label}")

        if not ensure_symbol_visible(symbol):
            log.warning(f"Symbol {symbol} not available on this broker — check the exact name in Market Watch")
            return None

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, self.bars)
        if rates is None or len(rates) == 0:
            log.warning(f"No data for {symbol} {timeframe_label} (error: {mt5.last_error()})")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_multi_timeframe(self, symbol, timeframe_labels):
        return {tf: self.get_ohlcv(symbol, tf) for tf in timeframe_labels}