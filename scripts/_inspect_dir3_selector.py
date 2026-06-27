"""Research-only: diagnose selector-level effect of momentum guard ON vs OFF.

The label-level selectivity (blocking losers) did NOT translate to selector
improvement. This script traces WHY: it compares the selector route outcomes
with guard ON vs OFF and shows which windows changed route, and the cascade
effect (blocking a BULL can change which route the selector picks, defer trips,
etc.).
"""
import importlib.util
from pathlib import Path

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
)

base = dict(
    references=refs, breadth_coins=coins,
    step_hours=12, warmup_hours=120, forward_hours=24, confirmation_samples=2,
    min_confidence=0.60, tune_scorecard=True, tune_route_objective=True, train_fraction=0.60,
    selector_lookback=12, selector_min_objective=0.0,
    selector_max_trailing_drawdown_pct=15.0, selector_equity_stop_drawdown_pct=15.0,
    selector_equity_stop_cooldown_windows=1, selector_min_trailing_win_rate_pct=50.0,
    selector_trailing_robust_windows=3, selector_min_passing_trailing_windows=3,
    selector_trailing_window_max_drawdown_pct=15.0,
)

def route_ret(r):
    return ev.route_window_return(
        str(r.get("selector_smoothed", "sideways")),
        future_basket_ret=float(r.get("future_basket_ret", 0)),
        future_btc_ret=float(r.get("future_btc_ret", 0)),
        fee_bps=10.0,
    )

for days in (240, 300):
    print(f"\n{'='*70}\n=== {days}d ===\n{'='*70}")
    out_off = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), momentum_guard=False, **base)
    out_on = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), momentum_guard=True, **base)
    recs_off = out_off["records"]
    recs_on = out_on["records"]

    # Route outcomes
    for label, out in (("OFF", out_off), ("ON", out_on)):
        ro = out["leaderboard"]["by_metric"]["route_outcomes"]
        sel = next((r for r in ro if r["name"] == "regime_v2_selector"), None)
        if sel:
            print(f"  [{label}] selector: ret={sel['total_return_pct']:+.2f}% maxDD={sel['max_drawdown_pct']:.2f}% win={sel['win_rate_pct']:.1f}%")
        # robustness
        rob = out.get("route_robustness", {}).get("regime_v2_selector", {})
        print(f"  [{label}] robustness: {rob.get('passing_windows',0)}/{rob.get('total_windows',0)} passed={rob.get('passed')}")

    # How many labels changed?
    n = min(len(recs_off), len(recs_on))
    changed = [(i, recs_off[i], recs_on[i]) for i in range(n) if recs_off[i]["v2_smoothed"] != recs_on[i]["v2_smoothed"]]
    print(f"\n  Labels changed by guard: {len(changed)}/{n}")
    # Of those, how did the selector route change?
    route_changed = [(i, o, on) for i, o, on in changed if o.get("selector_smoothed") != on.get("selector_smoothed")]
    print(f"  Of changed labels, selector route also changed: {len(route_changed)}")

    # Show the changed windows with their returns
    print(f"\n  --- windows where selector route changed ({len(route_changed)}) ---")
    net = 0.0
    for i, o, on in route_changed[:30]:
        ro = route_ret(o)
        rn = route_ret(on)
        delta = rn - ro
        net += delta
        blk = "; ".join(on.get("momentum_guard_reasons") or [])
        print(f"    [{i}] {o['time']}: off={o['selector_smoothed']:8s}({ro:+.2f}%) on={on['selector_smoothed']:8s}({rn:+.2f}%) Δ={delta:+.2f}% | {blk[:70]}")
    if len(route_changed) > 30:
        for i, o, on in route_changed[30:]:
            net += route_ret(on) - route_ret(o)
    print(f"  NET selector return delta from route changes: {net:+.2f}%")

    # Also: windows where label changed but selector route did NOT change
    route_same = [(i, o, on) for i, o, on in changed if o.get("selector_smoothed") == on.get("selector_smoothed")]
    print(f"\n  --- windows where label changed but route STAYED same ({len(route_same)}) ---")
    for i, o, on in route_same[:10]:
        print(f"    [{i}] {o['time']}: label {o['v2_smoothed']}->{on['v2_smoothed']} route={o['selector_smoothed']} (selector picked a different candidate)")
