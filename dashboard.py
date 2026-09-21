from rich.table import Table
from rich.console import Console

console = Console()


def render_dashboard(signals: dict, positions: list, balance: float, mode: str = "clear"):
    """mode="clear": repaints the console every cycle (default, looks nice standalone).
    mode="stream": append-only, doesn't clear, plays nice if you're also tailing
    logs/trading.log in the same terminal."""
    table = Table(title=f"Trading Bot — Balance: {balance:.2f}")
    table.add_column("Symbol")
    table.add_column("Signal")
    table.add_column("Score")
    table.add_column("Open Position")

    open_by_symbol = {p.symbol: p for p in positions} if positions else {}

    for symbol, result in signals.items():
        pos_info = "-"
        if symbol in open_by_symbol:
            p = open_by_symbol[symbol]
            side = "BUY" if p.type == 0 else "SELL"
            pos_info = f"{side} {p.volume} lots, PnL {p.profit:.2f}"
        table.add_row(symbol, result["decision"], str(result["score"]), pos_info)

    if mode == "stream":
        console.print(table)
    else:
        console.clear()
        console.print(table)
