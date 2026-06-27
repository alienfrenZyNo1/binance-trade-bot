#!/usr/bin/env python3
"""Confirm: equity_at_entry stays large (stale) while book equity drops => fake PnL spikes."""
import sys, types
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)
data = loaded["WLDUSDC"]
close = data["close"].astype(np.float64); ts = data["ts"].astype(np.int64)

# Re-run the real trend function but monkeypatch to capture equity_at_entry vs equity at each exit
# Instead, just call it and inspect trades: at exit, pnl is relative to equity_at_entry.
eq, trades, m = r.backtest_trend_following(data, leverage=5.0)
print(f"num trades: {len(trades)}")
print(f"{'side':12s} {'entry':>9s} {'exit':>9s} {'pnl':>10s} {'dur':>7s}")
for t in trades:
    print(f"{t['side']:12s} {t['entry']:9.4f} {t['exit']:9.4f} {t['pnl']:10.2f} {t['duration_hours']:7.1f}")

# The telltale: a single trade's pnl is much bigger than the equity at that point.
# Real check: is final equity plausible given all pnls summing?
print(f"\nsum of all pnls: {sum(t['pnl'] for t in trades):.2f}")
print(f"final equity: {eq[-1]:.2f}")
print(f"INITIAL_CAPITAL: {r.INITIAL_CAPITAL}")
print(f"implied final = INITIAL + sum(pnls) - fees... = {r.INITIAL_CAPITAL + sum(t['pnl'] for t in trades):.2f}")
print(f"(fees are baked into equity during loop, so this won't match exactly, but should be close)")
