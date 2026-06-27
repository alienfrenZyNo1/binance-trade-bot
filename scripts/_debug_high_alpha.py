#!/usr/bin/env python3
"""Debug script to inspect equity curves and Sharpe computation."""
import sys
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import research_high_alpha as r

# Load cached data
cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)
symbols = list(loaded.files)
print("Cached symbols:", symbols)

sym = "BTCUSDC"
data = loaded[sym]
close = data["close"].astype(np.float64)
ts = data["ts"].astype(np.int64)
n = len(close)
print(f"\n{sym}: {n} candles, close[0]={close[0]:.2f} close[-1]={close[-1]:.2f}")

# ── RSI MR 3x ──
print("\n=== RSI MR 3x ===")
eq_rsi, trades_rsi, m_rsi = r.backtest_rsi_mean_reversion(data, leverage=3.0)
print(f"  equity_curve: first={eq_rsi[0]:.2f} last={eq_rsi[-1]:.2f}")
print(f"  min={np.min(eq_rsi):.4f} max={np.max(eq_rsi):.4f}")
print(f"  num zeros={np.sum(eq_rsi==0)} num negative={np.sum(eq_rsi<0)} num nan={np.sum(np.isnan(eq_rsi))}")
print(f"  metrics: total_ret={m_rsi.total_return_pct:.2f}% sharpe={m_rsi.sharpe_ratio:.4f} dd={m_rsi.max_drawdown_pct:.2f}%")

# Manual Sharpe
rets = np.diff(eq_rsi) / np.where(eq_rsi[:-1] == 0, 1e-10, eq_rsi[:-1])
rets = rets[np.isfinite(rets)]
print(f"  rets: len={len(rets)} mean={np.mean(rets):.8e} std(ddof1)={np.std(rets,ddof=1):.8e} std(ddof0)={np.std(rets):.8e}")
print(f"  manual sharpe (ddof1) = {(np.mean(rets)/np.std(rets,ddof=1))*np.sqrt(24*365):.6f}")
print(f"  min ret={np.min(rets):.6e} max ret={np.max(rets):.6e}")
# Check if returns are constant
print(f"  unique rets count={len(np.unique(np.round(rets,12)))}")

# ── Trend 3x ──
print("\n=== Trend 3x ===")
eq_t, trades_t, m_t = r.backtest_trend_following(data, leverage=3.0)
print(f"  equity_curve: first={eq_t[0]:.2f} last={eq_t[-1]:.2f}")
print(f"  min={np.min(eq_t):.4f} max={np.max(eq_t):.4f}")
print(f"  num zeros={np.sum(eq_t==0)} num negative={np.sum(eq_t<0)}")
print(f"  metrics: total_ret={m_t.total_return_pct:.2f}% sharpe={m_t.sharpe_ratio:.4f} dd={m_t.max_drawdown_pct:.2f}%")

# ── Momentum 2x ──
print("\n=== Momentum 2x ===")
eq_m, trades_m, m_mom = r.backtest_momentum_breakout(data, leverage=2.0)
print(f"  equity_curve: first={eq_m[0]:.2f} last={eq_m[-1]:.2f}")
print(f"  min={np.min(eq_m):.4f} max={np.max(eq_m):.4f}")
print(f"  num zeros={np.sum(eq_m==0)} num negative={np.sum(eq_m<0)}")
print(f"  metrics: total_ret={m_mom.total_return_pct:.2f}% sharpe={m_mom.sharpe_ratio:.4f} dd={m_mom.max_drawdown_pct:.2f}%")

# ── Combined ──
print("\n=== Combined ===")
eq_c, m_c = r.backtest_combined(data)
print(f"  combined equity: first={eq_c[0]:.2f} last={eq_c[-1]:.2f}")
print(f"  min={np.min(eq_c):.4f} max={np.max(eq_c):.4f}")
print(f"  num zeros={np.sum(eq_c==0)} num negative={np.sum(eq_c<0)}")
print(f"  metrics: total_ret={m_c.total_return_pct:.2f}% sharpe={m_c.sharpe_ratio:.4f} dd={m_c.max_drawdown_pct:.2f}%")

# Inspect _normalize behavior
print("\n=== _normalize inspection ===")
for name, eq in [("trend3x", eq_t), ("mom2x", eq_m), ("rsi3x", eq_rsi)]:
    nz = eq[eq > 0]
    first = nz[0] if len(nz) else 0
    normed = r.backtest_combined.__code__  # just for ref
    print(f"  {name}: raw first_nonzero={first:.2f} last={eq[-1]:.2f} min={np.min(eq):.4f} max={np.max(eq):.4f}")

# Show the normalized curves' last values
def _normalize(eq):
    nonzero = eq[eq > 0]
    if len(nonzero) == 0:
        return eq
    first_val = nonzero[0]
    return np.where(eq > 0, eq / first_val, 1.0)

eq1_n = _normalize(eq_t)
eq2_n = _normalize(eq_m)
eq3_n = _normalize(eq_rsi)
print(f"\n  normalized LAST values: trend3x={eq1_n[-1]:.4f} mom2x={eq2_n[-1]:.4f} rsi3x={eq3_n[-1]:.4f}")
print(f"  normalized MAX values:  trend3x={np.max(eq1_n):.4f} mom2x={np.max(eq2_n):.4f} rsi3x={np.max(eq3_n):.4f}")
min_len = min(len(eq1_n), len(eq2_n), len(eq3_n))
combined = (0.34*eq1_n[:min_len] + 0.33*eq2_n[:min_len] + 0.33*eq3_n[:min_len]) * r.INITIAL_CAPITAL
print(f"  manual combined: last={combined[-1]:.2f} (return={combined[-1]/r.INITIAL_CAPITAL*100-100:.1f}%)")
print(f"  combined max={np.max(combined):.2f}")
