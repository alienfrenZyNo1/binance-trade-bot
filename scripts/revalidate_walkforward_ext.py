#!/usr/bin/env python3
"""Supplementary walk-forward analysis: smaller OOS windows for more samples.

Runs the CORRECTED engine (next-bar-open, slip=0.1%) across as many
disjoint 20-day OOS windows as the data supports, using BOTH:
  (a) per-window optimized params (true walk-forward)
  (b) the fixed deployed params (no peeking, pure out-of-sample)

This gives more statistical data points to judge robustness.
"""

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from revalidate_backtest import (
    build_indices, build_param_grid_sample, run_rotation, buy_hold,
    COINS, DEFAULT_STARTING_COIN, REF_COIN, HOUR_MS, DAY_MS,
    DEFAULT_INITIAL_BALANCE,
)

CACHE = Path(__file__).resolve().parent.parent / "research_results" / "reval_data.json"
with open(CACHE) as fh:
    ohlcv = json.load(fh)["ohlcv"]

close_idx, open_idx, ts_list = build_indices(ohlcv)
print(f"{len(ts_list)} candles: "
      f"{datetime.fromtimestamp(ts_list[0]/1000, tz=timezone.utc).date()} → "
      f"{datetime.fromtimestamp(ts_list[-1]/1000, tz=timezone.utc).date()}")

deployed = {
    "momentum_lookback": 18, "momentum_min_edge": 8.0, "cooldown_hours": 2,
    "anti_churn_hours": 24, "trailing_stop_pct": 15, "use_regime_filter": True,
}

# Build disjoint 20-day OOS windows walking backward from the end.
OOS_DAYS = 20
TRAIN_DAYS = 100
end_ts = ts_list[-1]
windows = []
cur_end = end_ts
while True:
    oos_end = cur_end
    oos_start = oos_end - OOS_DAYS * DAY_MS
    train_end = oos_start
    train_start = train_end - TRAIN_DAYS * DAY_MS
    if train_start < ts_list[0] + 50 * HOUR_MS:  # need 50 bars warmup
        break
    windows.append((train_start, train_end, oos_start, oos_end))
    cur_end = oos_start
print(f"\n{len(windows)} disjoint {OOS_DAYS}-day OOS windows (train={TRAIN_DAYS}d each)\n")

grid = build_param_grid_sample(max_combos=250, seed=7)

print(f"{'#':>2} {'OOS period':>24} {'OptTrain':>9} {'OptOOS':>8} {'FixOOS':>8} "
      f"{'B&H TIA':>8} {'B&H SOL':>8} {'opt>fix':>7} {'oosTr':>6}")
print("-" * 92)

opt_oos = []
fix_oos = []
bh_tia_list = []
beat_tia_opt = 0
beat_tia_fix = 0
details = []

for i, (tr_s, tr_e, oos_s, oos_e) in enumerate(windows):
    # (a) per-window optimized
    best = None
    for combo in grid:
        r = run_rotation(combo, start_ts=tr_s, end_ts=tr_e, ohlcv_by_coin=ohlcv,
                         close_idx=close_idx, open_idx=open_idx, ts_list=ts_list,
                         exec_mode="next_bar_open", fee=0.00075, slip=0.001)
        if best is None or r["pnl"] > best["pnl"]:
            best = {**r, "params": combo}
    oos_opt = run_rotation(best["params"], start_ts=oos_s, end_ts=oos_e,
                           ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
                           ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.001)
    # (b) fixed deployed params
    oos_fix = run_rotation(deployed, start_ts=oos_s, end_ts=oos_e,
                           ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
                           ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.001)
    bh_tia = buy_hold(ohlcv.get(DEFAULT_STARTING_COIN, []), oos_s, oos_e)
    bh_sol = buy_hold(ohlcv.get(REF_COIN, []), oos_s, oos_e)

    opt_oos.append(oos_opt["pnl"])
    fix_oos.append(oos_fix["pnl"])
    bh_tia_list.append(bh_tia)
    if oos_opt["pnl"] > bh_tia:
        beat_tia_opt += 1
    if oos_fix["pnl"] > bh_tia:
        beat_tia_fix += 1

    per = f"{datetime.fromtimestamp(oos_s/1000, tz=timezone.utc).strftime('%m-%d')}→{datetime.fromtimestamp(oos_e/1000, tz=timezone.utc).strftime('%m-%d')}"
    print(f"{i:>2} {per:>24} {best['pnl']:>+8.1f}% {oos_opt['pnl']:>+7.1f}% "
          f"{oos_fix['pnl']:>+7.1f}% {bh_tia:>+7.1f}% {bh_sol:>+7.1f}% "
          f"{str(oos_opt['pnl'] > oos_fix['pnl']):>7} {oos_opt['trades']:>6}")
    details.append({
        "oos_start": datetime.fromtimestamp(oos_s/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "opt_train": best["pnl"], "opt_oos": oos_opt["pnl"],
        "fix_oos": oos_fix["pnl"], "bh_tia": bh_tia, "bh_sol": bh_sol,
        "opt_trades": oos_opt["trades"], "opt_max_dd": oos_opt["max_dd"],
    })

print("-" * 92)
n = len(opt_oos)
print(f"\nAcross {n} windows:")
print(f"  Optimized OOS: mean {statistics.mean(opt_oos):+.2f}% | "
      f"median {statistics.median(opt_oos):+.2f}% | "
      f"positive {sum(1 for p in opt_oos if p>0)}/{n} | "
      f"beat TIA {beat_tia_opt}/{n}")
print(f"  Fixed OOS:     mean {statistics.mean(fix_oos):+.2f}% | "
      f"median {statistics.median(fix_oos):+.2f}% | "
      f"positive {sum(1 for p in fix_oos if p>0)}/{n} | "
      f"beat TIA {beat_tia_fix}/{n}")
print(f"  B&H TIA:       mean {statistics.mean(bh_tia_list):+.2f}%")
# Does optimization help OOS? (if not, edge isn't from the model)
opt_better = sum(1 for o, f in zip(opt_oos, fix_oos) if o > f)
print(f"  Optimization helps OOS: {opt_better}/{n} windows")

out = {
    "oos_days": OOS_DAYS, "train_days": TRAIN_DAYS, "n_windows": n,
    "optimized": {
        "oos_mean": statistics.mean(opt_oos), "oos_median": statistics.median(opt_oos),
        "positive_count": sum(1 for p in opt_oos if p > 0),
        "beat_tia": beat_tia_opt,
        "windows": opt_oos,
    },
    "fixed_deployed": {
        "oos_mean": statistics.mean(fix_oos), "oos_median": statistics.median(fix_oos),
        "positive_count": sum(1 for p in fix_oos if p > 0),
        "beat_tia": beat_tia_fix,
        "windows": fix_oos,
    },
    "bh_tia_mean": statistics.mean(bh_tia_list),
    "optimization_helps_oos": opt_better,
    "details": details,
}
OUT = Path(__file__).resolve().parent.parent / "research_results" / "revalidation_walkforward_ext.json"
OUT.parent.mkdir(exist_ok=True)
with open(OUT, "w") as fh:
    json.dump(out, fh, indent=2)
print(f"\nWritten to {OUT}")
