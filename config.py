import os
import yaml
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = os.getenv("BOT_CONFIG_PATH", "config.yaml")


def _load_yaml():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


_yaml_cfg = _load_yaml()  # empty dict if the file doesn't exist, everything falls back to hardcoded defaults


def _get(path, default):
    """Dig into the yaml dict with a dotted path. Missing file or missing key both
    just fall through to the default, so config.yaml is fully optional."""
    node = _yaml_cfg
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


@dataclass
class Settings:
    # --- MT5 login, from .env, never from config.yaml (don't commit credentials) ---
    mt5_login: int = int(os.getenv("MT5_LOGIN", "0"))
    mt5_password: str = os.getenv("MT5_PASSWORD", "")
    mt5_server: str = os.getenv("MT5_SERVER", "")
    mt5_path: str = os.getenv("MT5_PATH", "")

    # --- universe ---
    symbols: list = field(default_factory=lambda: _get("symbols", ["EURUSD", "GBPUSD", "XAUUSD"]))

    # --- risk ---
    risk_per_trade: float = _get("risk.risk_per_trade", 0.02)
    atr_sl_multiplier: float = _get("risk.atr_sl_multiplier", 1.5)
    atr_tp_multiplier: float = _get("risk.atr_tp_multiplier", 3.0)
    cooldown_minutes: int = _get("risk.cooldown_minutes", 30)
    max_positions: int = _get("risk.max_positions", 5)

    # --- signal thresholds ---
    adx_trend_threshold: float = _get("indicators.adx_trend_threshold", 25.0)
    rsi_buy_ceiling: float = _get("indicators.rsi_buy_ceiling", 60.0)
    rsi_sell_floor: float = _get("indicators.rsi_sell_floor", 40.0)

    # --- divergence ---
    divergence_lookback: int = _get("divergence.lookback", 40)
    divergence_tolerance: float = _get("divergence.tolerance", 0.02)
    divergence_strict: bool = _get("divergence.strict", True)

    # --- session filter (UTC hours) ---
    trading_start_hour: int = _get("trading_hours.start_hour_utc", 7)
    trading_end_hour: int = _get("trading_hours.end_hour_utc", 16)

    # --- runtime ---
    loop_interval_seconds: int = _get("runtime.loop_interval_seconds", 60)
    bars_to_fetch: int = _get("runtime.bars_to_fetch", 300)
    close_positions_on_exit: bool = _get("runtime.close_positions_on_exit", False)

    # off by default — flipping this on lets the bot open more than one position on
    # the SAME symbol at once (pyramiding), still bounded by max_positions overall
    allow_stacking: bool = _get("risk.allow_multiple_positions_per_symbol", False)

    # DRY_RUN: an explicit .env value always wins (keeps the old behavior intact),
    # otherwise fall back to config.yaml, otherwise default to safe (True)
    dry_run: bool = (
        os.getenv("DRY_RUN").lower() == "true" if os.getenv("DRY_RUN") is not None
        else _get("runtime.dry_run", True)
    )

    # --- strategy mode: "swing" (default), "scalp", "divergence", "trendline",
    #     "sr", or "combo" (runs swing+divergence+trendline+sr, needs 2+ of 4 to
    #     agree). Same single key that's always driven mode selection — see the
    #     README for why this isn't a second "strategies.mode" key. ---
    strategy_mode: str = _get("strategy.mode", "swing")

    # --- scalp mode only, ignored entirely when strategy_mode == "swing" ---
    scalp_entry_timeframe: str = _get("scalp.entry_timeframe", "M1")
    scalp_trend_timeframe: str = _get("scalp.trend_timeframe", "M15")
    scalp_ema_fast: int = _get("scalp.ema_fast", 9)
    scalp_ema_slow: int = _get("scalp.ema_slow", 21)
    scalp_rsi_period: int = _get("scalp.rsi_period", 7)
    scalp_adx_threshold: float = _get("scalp.adx_threshold", 15.0)
    scalp_atr_sl_multiplier: float = _get("scalp.atr_sl_multiplier", 0.8)
    scalp_atr_tp_multiplier: float = _get("scalp.atr_tp_multiplier", 1.2)
    scalp_cooldown_minutes: int = _get("scalp.cooldown_minutes", 5)
    scalp_loop_interval_seconds: int = _get("scalp.loop_interval_seconds", 5)
    scalp_max_spread_points: float = _get("scalp.max_spread_points", 20.0)

    # --- divergence STRATEGY (a full standalone evaluator — not the same thing as
    #     the divergence_* settings above, which only feed swing mode's built-in
    #     veto check. Prefixed divstrat_ specifically to avoid that confusion) ---
    divstrat_oscillator: str = _get("divergence_strategy.oscillator", "RSI")
    # not part of the original spec list — needed to bound how far back this
    # strategy searches for swings; without a cap it would scan the entire
    # fetched history every call
    divstrat_lookback_bars: int = _get("divergence_strategy.lookback_bars", 60)
    divstrat_min_swing_gap_bars: int = _get("divergence_strategy.min_swing_gap_bars", 5)
    divstrat_confirmation_bars: int = _get("divergence_strategy.require_confirmation_bars", 1)
    divstrat_strength_threshold: int = _get("divergence_strategy.strength_threshold", 1)

    # --- trendline strategy ---
    trendline_swing_lookback: int = _get("trendline_strategy.swing_lookback", 50)
    trendline_min_swings_required: int = _get("trendline_strategy.min_swings_required", 3)
    trendline_min_r_squared: float = _get("trendline_strategy.min_r_squared", 0.75)
    trendline_touch_atr_fraction: float = _get("trendline_strategy.touch_atr_fraction", 0.25)
    trendline_break_atr_fraction: float = _get("trendline_strategy.break_atr_fraction", 0.30)
    trendline_require_slope_direction: bool = _get("trendline_strategy.require_slope_direction", True)

    # --- support/resistance strategy ---
    sr_lookback: int = _get("sr_strategy.lookback", 100)
    sr_cluster_atr_fraction: float = _get("sr_strategy.cluster_atr_fraction", 0.25)
    sr_min_touches: int = _get("sr_strategy.min_touches", 2)
    sr_approach_atr_fraction: float = _get("sr_strategy.approach_atr_fraction", 0.5)
    sr_break_atr_fraction: float = _get("sr_strategy.break_atr_fraction", 0.3)
    sr_max_zones: int = _get("sr_strategy.max_zones", 6)
    # not part of the original spec list — "flip persists for the next N passes"
    # needed a concrete unit, and bar-count is the right one: it's what every
    # other windowing parameter in this file already uses, it's stable across
    # loop_interval_seconds changes, and it doesn't stall when this evaluator
    # goes uncalled for a while (e.g. backtester skips it entirely while a trade
    # is open). How many H1 bars a broken zone keeps acting in its flipped role
    # before the memory of the flip expires.
    sr_flip_persist_bars: int = _get("sr_strategy.flip_persist_bars", 20)


settings = Settings()
