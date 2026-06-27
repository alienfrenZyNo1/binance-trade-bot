#!/usr/bin/env python3
"""Direct instrumentation of backtest_combined to resolve discrepancy."""
import sys
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)
data = loaded["BTCUSDC"]

# Replicate backtest_combined internals EXACTLY but with prints
weights = [0.34, 0.33, 0.33]
w_sum = sum(weights)
weights = [w / w_sum for w in weights]
print("weights:", weights)

eq1, t1, m1 = r.backtest_trend_following(data, leverage=3.0)
eq2, t2, m2 = r.backtest_momentum_breakout(data, leverage=2.0)
eq3, t3, m3 = r.backtest_rsi_mean_reversion(data, leverage=3.0)
print(f"raw last: eq1={eq1[-1]:.2f} eq2={eq2[-1]:.2f} eq3={eq3[-1]:.2f}")
print(f"raw max:  eq1={np.max(eq1):.2f} eq2={np.max(eq2):.2f} eq3={np.max(eq3):.2f}")

def _normalize(eq):
    nonzero = eq[eq > 0]
    if len(nonzero) == 0:
        return eq
    first_val = nonzero[0]
    return np.where(eq > 0, eq / first_val, 1.0)

eq1_n = _normalize(eq1)
eq2_n = _normalize(eq2)
eq3_n = _normalize(eq3)
print(f"norm last: eq1_n={eq1_n[-1]:.6f} eq2_n={eq2_n[-1]:.6f} eq3_n={eq3_n[-1]:.6f}")
print(f"norm max:  eq1_n={np.max(eq1_n):.6f} eq2_n={np.max(eq2_n):.6f} eq3_n={np.max(eq3_n):.6f}")

min_len = min(len(eq1_n), len(eq2_n), len(eq3_n))
print(f"lens: eq1={len(eq1)} eq2={len(eq2)} eq3={len(eq3)} min_len={min_len}")

combined = (weights[0]*eq1_n[:min_len] + weights[1]*eq2_n[:min_len] + weights[2]*eq3_n[:min_len]) * r.INITIAL_CAPITAL
print(f"combined: last={combined[-1]:.2f} first={combined[0]:.2f} max={np.max(combined):.2f}")

# Now call the actual function
eq_c, m_c = r.backtest_combined(data)
print(f"\nactual backtest_combined: last={eq_c[-1]:.2f} first={eq_c[0]:.2f} max={np.max(eq_c):.2f}")
print(f"len(combined)={len(combined)} len(eq_c)={len(eq_c)}")
print(f"max abs diff: {np.max(np.abs(combined[:len(eq_c)] - eq_c)):.4f}")
print(f"eq_c last 5: {eq_c[-5:]}")
print(f"my   last 5: {combined[-5:]}")
