#!/usr/bin/env python3
"""Diagnose why leveraged/liquidated strategies show positive Sharpe with huge losses."""
import sys
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)

for sym, strat, lev, kwargs in [
    ("WLDUSDC", "trend_5x", 5.0, {}),
    ("WLDUSDC", "trend_ls_3x", 3.0, {"allow_short": True}),
    ("ZECUSDC", "trend_ls_3x", 3.0, {"allow_short": True}),
    ("NEARUSDC", "trend_5x", 5.0, {}),
]:
    data = loaded[sym]
    eq, trades, m = r.backtest_trend_following(data, leverage=lev, **kwargs)
    close = data["close"].astype(np.float64)
    rets = np.diff(eq) / np.where(eq[:-1] == 0, 1e-10, eq[:-1])
    rets = rets[np.isfinite(rets)]
    print(f"\n=== {sym} / {strat} ===")
    print(f"  total_ret={m.total_return_pct:+.1f}%  sharpe={m.sharpe_ratio:.3f}  dd={m.max_drawdown_pct:.1f}%")
    print(f"  equity: start={eq[0]:.1f} end={eq[-1]:.1f} min={np.min(eq):.1f} max={np.max(eq):.1f}")
    print(f"  rets: len={len(rets)} mean={np.mean(rets):.6e} median={np.median(rets):.6e} std(ddof1)={np.std(rets,ddof=1):.6e}")
    print(f"  rets: min={np.min(rets):.4f} max={np.max(rets):.4f}")
    print(f"  count(rets==0)={np.sum(rets==0)}  count(rets>0)={np.sum(rets>0)}  count(rets<0)={np.sum(rets<0)}")
    print(f"  count(|rets|<1e-12)={np.sum(np.abs(rets)<1e-12)}  (= {np.sum(np.abs(rets)<1e-12)/len(rets)*100:.1f}% of bars)")
    # Distribution of nonzero returns
    nz = rets[np.abs(rets) > 1e-12]
    print(f"  nonzero rets: len={len(nz)} mean={np.mean(nz):.6e} std(ddof1)={np.std(nz,ddof=1):.6e}")
    print(f"  nonzero sharpe = {np.mean(nz)/np.std(nz,ddof=1)*np.sqrt(24*365):.3f}")
    # Examine the big moves
    big = np.sort(rets)
    print(f"  5 most negative rets: {big[:5]}")
    print(f"  5 most positive rets: {big[-5:]}")
    # Equity curve segments: how many flat runs
    diff = np.diff(eq)
    print(f"  num bars where equity unchanged: {np.sum(diff==0)} / {len(diff)}")
    # Show equity curve sampled
    n = len(eq)
    print(f"  equity at 0%,10%,20%,...,100%: " + ", ".join(f"{eq[int(n*k/10)]:.0f}" for k in range(11)))
