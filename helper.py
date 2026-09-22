import MetaTrader5 as mt5

def ensure_symbol_visible(symbol: str) -> bool:
    """Make sure a symbol is selected in Market Watch so copy_rates works.
    Returns True if the symbol is available, False if the broker doesn't have it."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return False  # broker doesn't recognize this symbol name at all
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            return False
    return True

def get_contract_size(symbol: str) -> int:
    """Get the contract size (units per lot) for a symbol.

    Name-based checks come FIRST, before any MT5 lookup. Originally this
    called mt5.symbol_info(symbol) and returned the forex default of 100000
    immediately if that came back None, before ever checking whether the
    symbol was gold/silver/oil/crypto — which meant get_contract_size()
    silently gave the wrong answer for XAUUSDm/BTCUSDm etc. any time MT5
    wasn't connected yet (e.g. a CSV-only backtest with no MT5 session),
    even though the correct answer only ever depended on the symbol's own
    name, not on any MT5 data. MT5 is only consulted now as a last resort,
    for symbols this function doesn't recognize by name at all."""
    if "JPY" in symbol:
        return 100000  # 100,000 units per standard lot for JPY pairs

    # XAUUSD (Gold) special handling
    if "XAU" in symbol or "GOLD" in symbol:
        return 100  # 100 ounces per standard lot

    # XAGUSD (Silver) special handling
    if "XAG" in symbol or "SILVER" in symbol:
        return 5000  # 5,000 ounces per standard lot

    # Oil
    if "WTI" in symbol or "OIL" in symbol:
        return 1000  # 1,000 barrels per standard lot

    # Crypto CFDs (BTCUSDm etc.) — usually 1 unit of the coin per lot, not
    # a forex-style 100,000.
    if "BTC" in symbol:
        return 1

    info = mt5.symbol_info(symbol)
    if info is None:
        return 100000  # unrecognized symbol AND no MT5 data to check against - forex default

    # Default for forex (EURUSD, GBPUSD, etc.)
    return 100000

def get_point_value(symbol: str) -> float:
    """Calculate the point value for a symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return 1.0
    
    # Get contract size
    contract_size = get_contract_size(symbol)
    
    # For XAUUSD, calculate point value differently
    if "XAU" in symbol or "GOLD" in symbol:
        # Gold: 1 point (0.01) move on 1 standard lot = $1
        # Actually, it depends on the broker. Using MT5's values:
        return info.trade_tick_value / info.trade_tick_size
    
    # For forex, use MT5's calculation
    return info.trade_tick_value / info.trade_tick_size


def get_spread_points(symbol: str):
    """Current bid/ask spread in points. Matters way more for scalping than swing
    trading — a 2-pip spread is background noise on a 40-pip swing target, but it's
    a huge chunk of a 5-pip scalp target. Returns None if tick/symbol data isn't
    available so callers can decide how to handle that rather than silently trading
    on a bad spread reading."""
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None or not info.point:
        return None
    return (tick.ask - tick.bid) / info.point