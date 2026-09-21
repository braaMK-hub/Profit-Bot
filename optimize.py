import os
import sys
import subprocess
import json
from datetime import datetime

# Test different parameter combinations
optimizations = [
    {"adx": 15, "rsi_buy": 70, "rsi_sell": 30, "risk": 0.01, "label": "very_aggressive"},
    {"adx": 18, "rsi_buy": 65, "rsi_sell": 35, "risk": 0.01, "label": "aggressive"},
    {"adx": 20, "rsi_buy": 60, "rsi_sell": 40, "risk": 0.01, "label": "moderate"},
    {"adx": 25, "rsi_buy": 55, "rsi_sell": 45, "risk": 0.01, "label": "conservative"},
    {"adx": 30, "rsi_buy": 50, "rsi_sell": 50, "risk": 0.01, "label": "very_conservative"},
]

def run_backtest(params):
    """Run backtest with given parameters and return results."""
    # This would need to modify config.yaml dynamically
    # For now, we'll just print what to test
    print(f"Testing: {params['label']}")
    print(f"  ADX: {params['adx']}, RSI Buy: {params['rsi_buy']}, RSI Sell: {params['rsi_sell']}")
    
    # You'd run: python run.py --backtest --symbol EURUSD --start 2023-01-01 --end 2024-06-01
    # Then parse the results

if __name__ == "__main__":
    print("Parameter Optimization Runner")
    print("=" * 50)
    
    for params in optimizations:
        run_backtest(params)
        print("-" * 30)
    
    print("\nDone. Compare results to find the best parameters.")