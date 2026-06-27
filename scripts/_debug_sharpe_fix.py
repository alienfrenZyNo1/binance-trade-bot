#!/usr/bin/env python3
"""Verify candidate Sharpe fixes produce sane numbers for the contaminated cases."""
import sys
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)

def sharpe_arith(eq):  # current
    rets = np.diff(eq)/np.where(eq[:-1]==0,1e-10,eq[:-1]); rets=rets[np.isfinite(rets)]
    if len(rets)<=10: return 0.0
    rf=r.RISK_FREE_RATE/(24*r.TRADING_DAYS)
    return (np.mean(rets)-rf)/np.std(rets,ddof=1)*np.sqrt(24*r.TRADING_DAYS)

def sharpe_log(eq):  # FIX: log returns
    rets = np.diff(eq)/np.where(eq[:-1]==0,1e-10,eq[:-1]); rets=rets[np.isfinite(rets)]
    rets=rets[rets>-0.999]  # avoid log(0)
    if len(rets)<=10: return 0.0
    logr=np.log1p(rets)
    rf=r.RISK_FREE_RATE/(24*r.TRADING_DAYS)
    return (np.mean(logr)-rf)/np.std(logr,ddof=1)*np.sqrt(24*r.TRADING_DAYS)

def sharpe_daily_log(eq, ts):  # FIX: resample to daily then log
    # eq and ts are hourly; take last bar per UTC day
    import numpy as np
    # group by day index
    days = ts // r.DAY_MS
    # last equity per day
    ub, idx = np.unique(days, return_index=False), []
    last_per_day = {}
    for i in range(len(eq)):
        last_per_day[int(days[i])] = eq[i]
    day_eq = np.array([last_per_day[d] for d in sorted(last_per_day)])
    if len(day_eq) < 3: return 0.0
    rets = np.diff(day_eq)/np.where(day_eq[:-1]==0,1e-10,day_eq[:-1])
    rets = rets[np.isfinite(rets) & (rets>-0.999)]
    if len(rets)<=2: return 0.0
    logr=np.log1p(rets)
    rf=r.RISK_FREE_RATE/r.TRADING_DAYS
    return (np.mean(logr)-rf)/np.std(logr,ddof=1)*np.sqrt(r.TRADING_DAYS)

print(f"{'sym/strat':28s} {'totalRet':>9s} | {'arith':>7s} {'logH':>7s} {'logD':>7s}")
print("-"*70)
cases = [("WLDUSDC","trend_5x",5.0,{}), ("ZECUSDC","trend_ls_3x",3.0,{"allow_short":True}),
         ("WLDUSDC","trend_ls_3x",3.0,{"allow_short":True}), ("SUIUSDC","trend_ls_3x",3.0,{"allow_short":True}),
         ("SUIUSDC","trend_3x",3.0,{}), ("BTCUSDC","trend_1x",1.0,{}),
         ("ZECUSDC","momentum_2x",2.0,None), ("WLDUSDC","grid_spot",1.0,None)]
for sym, strat, lev, kw in cases:
    data = loaded[sym]
    if kw is None:
        if strat=="momentum_2x":
            eq,tr,m = r.backtest_momentum_breakout(data, leverage=lev)
        else:
            eq,tr,m = r.backtest_grid_trading(data, leverage=lev)
    else:
        eq,tr,m = r.backtest_trend_following(data, leverage=lev, **kw)
    ts = data["ts"].astype(np.int64)
    print(f"{sym+'/'+strat:28s} {m.total_return_pct:+8.1f}% | {sharpe_arith(eq):7.2f} {sharpe_log(eq):7.2f} {sharpe_daily_log(eq,ts):7.2f}")
