import logging
import os
import sys

def get_logger(name="trading_bot"):
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    # File handler with UTF-8 encoding
    file_handler = logging.FileHandler("logs/trading.log", encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Console handler: reason strings occasionally carry non-ASCII punctuation
    # (docstring quoting, or a file re-saved by an editor with "smart
    # punctuation" enabled). Python's default stdout encoding on Windows
    # isn't always UTF-8, so force it here (3.7+) rather than relying on
    # "never use non-ASCII anywhere" as the only defense — errors='replace'
    # means a genuinely unencodable character prints as '?' instead of
    # crashing the whole logging call or silently garbling neighboring text.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # best-effort; unusual stdout objects may not support this

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger