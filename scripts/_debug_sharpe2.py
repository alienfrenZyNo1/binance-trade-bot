#!/usr/bin/env python3
"""Pinpoint the source of impossible single-bar returns (>100%) in trend MTM."""
import sys
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)

# WLD trend_5x
data = loaded["WLDUSDC"]
close = data["close"].astype(np.float64)
ts = data["ts"].astype(np.int64)
eq, trades, m = r.backtest_trend_following(data, leverage=5.0)

rets = np.diff(eq) / np.where(eq[:-1] == 0, 1e-10, eq[:-1])
# Find the biggest single-bar positive return
imax = np.argmax(rets)
imin = np.argmin(rets)
print(f"Max ret bar {imax}: ret={rets[imax]:.4f}  eq[i]={eq[imax]:.1f} eq[i+1]={eq[imax+1]:.1f} close ratio={close[imax+1]/close[imax]:.4f}")
print(f"Min ret bar {imin}: ret={rets[imin]:.4f}  eq[i]={eq[imin]:.1f} eq[i+1]={eq[imin+1]:.1f} close ratio={close[imin+1]/close[imin]:.4f}")

# Show equity curve around the biggest positive jump
print(f"\nContext around max-ret bar ({imax}):")
for i in range(max(0,imax-3), min(len(eq), imax+4)):
    print(f"  i={i:4d} eq={eq[i]:9.2f} close={close[i]:.4f}")

# The MTM formula: eq_now = equity + (close[i]/close[i-1]-1)*leverage*equity_at_entry
# A ret of 3.23 means eq went from X to X*(1+3.23). Let's see if equity_at_entry is stale.
# Re-derive: the gain component is (close[i]/close[i-1]-1)*5*equity_at_entry
# If equity_at_entry is large (e.g. 10000) but equity is now small (e.g. 86),
# then a 6% price move => 0.06*5*10000 = 3000 added to eq=86 => 3086 => ret=34x!

# Find the equity_at_entry vs equity at the max bar
print("\n=> This is a STALE equity_at_entry bug:")
print("   After liquidation, position closes, but a NEW position opens with equity_at_entry")
print("   set to current (tiny) equity. But if position is HELD through the move...")
print()
print("Actually let's check: is equity_at_entry ever LARGER than current equity at a held bar?")

# Re-run manually with instrumentation
def run_with_log(data, leverage, allow_short=False):
    close = data["close"].astype(np.float64); ts = data["ts"].astype(np.int64)
    n = len(close)
    ema_fast = r.ema(close, 50); ema_slow = r.ema(close, 200)
    position = 0.0; entry_price=0.0; entry_ts=0
    equity = r.INITIAL_CAPITAL; equity_curve = np.zeros(n)
    equity_at_entry = r.INITIAL_CAPITAL
    equity_curve[0] = equity
    flagged = 0
    for i in range(1, n):
        if i >= 200:
            golden = ema_fast[i] > ema_slow[i] and ema_fast[i-1] <= ema_slow[i-1]
            death = ema_fast[i] < ema_slow[i-1] and ema_fast[i-1] >= ema_slow[i-1]
        else:
            golden=death=False
        # (simplified) - just track MTM relationship
        if position != 0 and i > 0:
            if position > 0:
                mtm = (close[i]/close[i-1]-1)*leverage*equity_at_entry
            else:
                mtm = (close[i-1]/close[i]-1)*leverage*equity_at_entry
            eq_now = equity + mtm
            # flag impossible bars
            prev = equity_curve[i-1]
            if prev > 0 and eq_now/prev - 1 > 0.5 and eq_now - prev > 1:
                flagged += 1
                if flagged <= 5:
                    print(f"  i={i:4d}: prev_eq={prev:8.1f} equity(book)={equity:8.1f} eq_at_entry={equity_at_entry:8.1f} "
                          f"close_ratio={close[i]/close[i-1]:.4f} mtm={mtm:8.1f} eq_now={eq_now:8.1f} ret={eq_now/prev-1:+.3f}")
        equity_curve[i] = equity if position==0 else eq_now
    return flagged

print("\n=== Trend 5x WLDUSDC impossible-bar flags (first 5) ===")
run_with_log(data, 5.0)
