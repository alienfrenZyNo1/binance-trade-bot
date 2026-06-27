#!/usr/bin/env python3
"""Compare current Sharpe (contaminated) vs robust Sharpe (correct).
The contamination: zero-return bars (62% of data) + liquidation-floor discontinuities.
"""
import sys
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)

def current_sharpe(eq):
    rets = np.diff(eq) / np.where(eq[:-1]==0,1e-10,eq[:-1])
    rets = rets[np.isfinite(rets)]
    if len(rets) <= 10: return 0.0
    rf = r.RISK_FREE_RATE/(24*r.TRADING_DAYS)
    return (np.mean(rets)-rf)/np.std(rets,ddof=1)*np.sqrt(24*r.TRADING_DAYS)

def robust_sharpe(eq):
    """Correct Sharpe using arithmetic returns over the FULL curve.
    The issue: returns must be computed properly. The current impl is actually
    fine in FORMULA. The real problem is the equity curve itself has artifacts.
    Let's confirm by using the SAME formula but checking what the 'mean' is."""
    rets = np.diff(eq) / np.where(eq[:-1]==0,1e-10,eq[:-1])
    rets = rets[np.isfinite(rets)]
    return np.mean(rets), np.std(rets,ddof=1), len(rets)

print(f"{'sym/strat':28s} {'totalRet':>9s} {'curShp':>7s} {'meanR':>11s} {'stdR':>11s} {'nR':>6s} {'nZero':>6s}")
for sym in loaded.files:
    data = loaded[sym]
    for strat, lev, kw in [("trend_5x",5.0,{}), ("trend_ls_3x",3.0,{"allow_short":True}), ("trend_3x",3.0,{})]:
        eq,tr,m = r.backtest_trend_following(data, leverage=lev, **kw)
        cs = current_sharpe(eq)
        meanr, stdr, nr = robust_sharpe(eq)
        nzero = np.sum(np.diff(eq)/np.where(eq[:-1]==0,1e-10,eq[:-1])==0)
        flag = " <<<" if (m.total_return_pct < -50 and cs > 0.8) else ""
        print(f"{sym+'/'+strat:28s} {m.total_return_pct:+8.1f}% {cs:7.2f} {meanr:11.2e} {stdr:11.2e} {nr:6d} {nzero:6d}{flag}")
