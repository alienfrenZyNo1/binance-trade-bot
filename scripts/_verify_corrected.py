#!/usr/bin/env python3
"""Sanity-check specific suspicious/interesting results in the CORRECTED run."""
import sys, json
import numpy as np
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import research_high_alpha as r

cache = REPO_ROOT / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
loaded = np.load(cache, allow_pickle=True)

# Load corrected JSON
jpath = REPO_ROOT / "docs" / "research" / "high-alpha-data.json"
data_json = json.loads(jpath.read_text())

# 1. DOGE momentum_2x: PF was 13 trillion — degenerate 1-trade case
d = loaded["DOGEUSDC"]
eq,tr,m = r.backtest_momentum_breakout(d, leverage=2.0)
print("DOGE/momentum_2x:")
print(f"  trades={len(tr)} pnls={[round(t['pnl'],2) for t in tr]}")
print(f"  reported PF={m.profit_factor:.2f} winrate={m.win_rate:.0f}%")

# 2. SUI trend_ls_3x: only strongly positive annual return (+78%). Verify.
s = loaded["SUIUSDC"]
eq,tr,m = r.backtest_trend_following(s, leverage=3.0, allow_short=True)
print(f"\nSUI/trend_ls_3x: total={m.total_return_pct:+.1f}% ann={m.annualized_return_pct:+.1f}% "
      f"sharpe={m.sharpe_ratio:.2f} dd={m.max_drawdown_pct:.1f}%")
print(f"  {len(tr)} trades, gross profit/loss check")
gprof = sum(t['pnl'] for t in tr if t['pnl']>0); gloss=abs(sum(t['pnl'] for t in tr if t['pnl']<0))
print(f"  gross_profit={gprof:.2f} gross_loss={gloss:.2f} PF={gprof/gloss:.2f}")
sides = [t['side'] for t in tr]
from collections import Counter
print(f"  side counts: {Counter(sides)}")

# 3. Verify the two grid bright spots (WLD +56.9%, NEAR +46.9%)
for sym in ["WLDUSDC","NEARUSDC"]:
    g = loaded[sym]
    eq,tr,m = r.backtest_grid_trading(g, leverage=1.0)
    print(f"\n{sym}/grid_spot: total={m.total_return_pct:+.1f}% ann={m.annualized_return_pct:+.1f}% "
          f"sharpe={m.sharpe_ratio:.2f} dd={m.max_drawdown_pct:.1f}% trades={len(tr)}")
    gprof = sum(t['pnl'] for t in tr if t['pnl']>0); gloss=abs(sum(t['pnl'] for t in tr if t['pnl']<0))
    wr = sum(1 for t in tr if t['pnl']>0)/len(tr)*100
    print(f"  PF={gprof/max(gloss,1e-9):.2f} winrate={wr:.0f}%")

# 4. Check how many strategy+symbol combos are PROFITABLE at all (ann>0) after fixes
print("\n=== All combos with annualized > 0% (corrected) ===")
profitable = []
for sym in loaded.files:
    for sname in ["trend_1x","trend_3x","trend_5x","trend_ls_3x","momentum_2x","grid_spot","rsi_mr_3x","combined"]:
        m = data_json["results"][sym]["strategies"].get(sname)
        if m and m["annualized_return_pct"] > 0:
            profitable.append((sym, sname, m["annualized_return_pct"], m["sharpe_ratio"], m["max_drawdown_pct"]))
profitable.sort(key=lambda x: -x[2])
for sym,sname,ann,sh,dd in profitable:
    print(f"  {sym:14s}/{sname:12s} ann={ann:+7.1f}%  sharpe={sh:6.2f}  dd={dd:5.1f}%")

# 5. Check the target: Sharpe>1.0 AND DD<15 AND Ann>50 — does ANY meet it?
print("\n=== Target check (Sharpe>1.0 AND DD<15 AND Ann>50) ===")
hits = [(sym,s,m) for sym in loaded.files for s,mv in data_json["results"][sym]["strategies"].items()
        if mv["sharpe_ratio"]>1.0 and mv["max_drawdown_pct"]<15 and mv["annualized_return_pct"]>50]
print(f"  MEETS ALL THREE: {len(hits)}")
# relaxed: Sharpe>1 AND DD<20 AND Ann>50
hits2 = [(sym,s,mv) for sym in loaded.files for s,mv in data_json["results"][sym]["strategies"].items()
         if mv["sharpe_ratio"]>1.0 and mv["max_drawdown_pct"]<20 and mv["annualized_return_pct"]>50]
print(f"  Sharpe>1 AND DD<20 AND Ann>50: {len(hits2)}")
for h in hits2: print(f"    {h}")
