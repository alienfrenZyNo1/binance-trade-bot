"""Research-only: diagnose why the momentum guard is a no-op on 240d real data.

Reports, for each record: the raw regime, the guarded regime, the exhaustion
features, and whether each guard branch's preconditions are met. This surfaces
whether the guard's thresholds are simply too tight for the realized basket
dynamics (e.g. the basket never reaches the roc12 precondition, so the
rollover/stall branches never fire).
"""
import importlib.util
from pathlib import Path
from collections import Counter

spec = importlib.util.spec_from_file_location("ev", "scripts/research_regime_v2_evaluator.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)
spec2 = importlib.util.spec_from_file_location("fr", "scripts/regime_v2_forward_replay.py")
fr = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(fr)

coins = ["SOL", "SUI", "AAVE", "LINK", "AVAX", "JUP", "ENA", "TIA", "APT"]
refs = ["BTC", "ETH", "SOL"]
data, meta = fr.load_or_fetch_market_data(
    cache_dir=Path(".cache/regime_v2_forward_replay"), days=365, coins=coins, references=refs,
    force_refresh=True,
)

for days in (240,):
    out = ev.evaluate_regime_v2_history(
        fr._slice_last_days(data, days), references=refs, breadth_coins=coins,
        step_hours=12, warmup_hours=120, forward_hours=24, confirmation_samples=2,
        min_confidence=0.60, tune_scorecard=True, tune_route_objective=True, train_fraction=0.60,
        selector_lookback=12, selector_min_objective=0.0,
        selector_max_trailing_drawdown_pct=15.0, selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1, selector_min_trailing_win_rate_pct=50.0,
        selector_trailing_robust_windows=3, selector_min_passing_trailing_windows=3,
        selector_trailing_window_max_drawdown_pct=15.0,
        momentum_guard=True,
    )
    recs = out["records"]
    print(f"=== {days}d: {len(recs)} records ===")
    # Distribution of exhaustion features
    bull_recs = [r for r in recs if r["v2_smoothed"] == ev.BULL]
    bear_recs = [r for r in recs if r["v2_smoothed"] == ev.BEAR]
    sw_recs = [r for r in recs if r["v2_smoothed"] == ev.SIDEWAYS]
    print(f"PLAIN label dist: BULL={len(bull_recs)} BEAR={len(bear_recs)} SIDEWAYS={len(sw_recs)}")

    # How many got blocked?
    blocked = [r for r in recs if r.get("momentum_guard_reasons")]
    print(f"Guard blocked {len(blocked)} labels")
    for r in blocked[:10]:
        print(f"  {r['time']}: {r.get('v2_smoothed_plain')} -> {r['v2_smoothed']} :: {'; '.join(r['momentum_guard_reasons'])}")

    # For BULL records, show the exhaustion feature distribution so we can see
    # why the rollover/stall branches don't fire.
    print("\n--- BULL-window exhaustion feature percentiles ---")
    for key in ("basket_roc_24h", "basket_roc_6h", "basket_roc_12h", "basket_deceleration", "basket_rsi", "basket_overextended_pct"):
        vals = sorted(float(r["exhaustion_features"].get(key, 0.0)) for r in bull_recs)
        if vals:
            p10 = vals[len(vals)//10]
            p50 = vals[len(vals)//2]
            p90 = vals[len(vals)*9//10]
            print(f"  {key:28s} p10={p10:+.2f} p50={p50:+.2f} p90={p90:+.2f} min={vals[0]:+.2f} max={vals[-1]:+.2f}")

    # How many BULL windows meet the rollover precondition (roc12 >= 2.0)?
    pre = [r for r in bull_recs if float(r["exhaustion_features"].get("basket_roc_12h", 0)) >= 2.0]
    print(f"\nBULL windows with roc12>=2.0 (rollover precondition): {len(pre)}/{len(bull_recs)}")
    # Of those, how many also have roc24 <= -1.0 (rollover trigger)?
    roll = [r for r in pre if float(r["exhaustion_features"].get("basket_roc_24h", 0)) <= -1.0]
    print(f"  ...and roc24<=-1.0 (rollover fires): {len(roll)}")
    # stall: roc6 <= -1.5 and roc12 >= 2.0
    stall = [r for r in pre if float(r["exhaustion_features"].get("basket_roc_6h", 0)) <= -1.5]
    print(f"  ...and roc6<=-1.5 (stall fires): {len(stall)}")

    print("\n--- BEAR-window exhaustion feature percentiles ---")
    for key in ("basket_roc_24h", "basket_roc_6h", "btc_roc_24h", "btc_basket_divergence"):
        vals = sorted(float(r["exhaustion_features"].get(key, 0.0)) for r in bear_recs)
        if vals:
            p10 = vals[len(vals)//10]
            p50 = vals[len(vals)//2]
            p90 = vals[len(vals)*9//10]
            print(f"  {key:28s} p10={p10:+.2f} p50={p50:+.2f} p90={p90:+.2f} min={vals[0]:+.2f} max={vals[-1]:+.2f}")

    # BEAR mean-revert precondition: roc24 <= -8.0 and roc6 > 1.0
    bear_deep = [r for r in bear_recs if float(r["exhaustion_features"].get("basket_roc_24h", 0)) <= -8.0]
    print(f"\nBEAR windows with roc24<=-8.0 (mean-revert precondition): {len(bear_deep)}/{len(bear_recs)}")
    # BEAR divergence: btc_roc>=2.0 and divergence>=3.0
    bear_div = [r for r in bear_recs if float(r["exhaustion_features"].get("btc_roc_24h", 0)) >= 2.0 and float(r["exhaustion_features"].get("btc_basket_divergence", 0)) >= 3.0]
    print(f"BEAR windows with btc divergence (btc_roc>=2 & div>=3): {len(bear_div)}/{len(bear_recs)}")
