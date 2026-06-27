"""Research-only: check which RAW v2_smoothed labels the guard blocks (pre-selector).

Shows the direct label-level effect of the guard, independent of selector cascade.
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
    force_refresh=True,
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
    selector_re_engage_confirmation=True, selector_re_engage_breadth_pct=0.60,
    selector_recent_pnl_lookback_windows=0, selector_recent_pnl_stop_pct=0.0,
)

for days in (240, 300):
    out_off = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), momentum_guard=False, **base)
    out_on = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), momentum_guard=True, **base)

    # Count raw v2_smoothed label changes (before selector).
    off_labels = {r["ts"]: r["v2_smoothed"] for r in out_off["records"]}
    on_labels = {r["ts"]: r["v2_smoothed"] for r in out_on["records"]}
    changed = [ts for ts in off_labels if off_labels[ts] != on_labels[ts]]

    # Also v2_route_tuned_smoothed and v2_tuned_smoothed
    off_rt = {r["ts"]: r.get("v2_route_tuned_smoothed", "?") for r in out_off["records"]}
    on_rt = {r["ts"]: r.get("v2_route_tuned_smoothed", "?") for r in out_on["records"]}
    rt_changed = [ts for ts in off_rt if off_rt[ts] != on_rt[ts]]

    print(f"\n=== {days}d ===")
    print(f"  v2_smoothed label changes: {len(changed)}")
    print(f"  v2_route_tuned_smoothed changes: {len(rt_changed)}")

    # For each changed v2_smoothed: show the future return
    for ts in sorted(changed):
        r_on = next(r for r in out_on["records"] if r["ts"] == ts)
        e = r_on.get("exhaustion_features", {})
        gr = r_on.get("momentum_guard_reasons", [])
        print(
            f"    {r_on['time']} {off_labels[ts]:8s}->{on_labels[ts]:8s} "
            f"fbask={r_on['future_basket_ret']:+6.2f} | "
            f"roc24={e.get('basket_roc_24h',0):+5.1f} roc12={e.get('basket_roc_12h',0):+5.1f} "
            f"roc6={e.get('basket_roc_6h',0):+5.1f} rsi={e.get('basket_rsi',0):3.0f} | "
            f"{gr[0][:60] if gr else ''}"
        )

    # Aggregate: if those blocked BULL labels had been traded as BULL, would they
    # have won or lost? (future_basket_ret - fee)
    blocked_bull = [ts for ts in changed if off_labels[ts] == ev.BULL]
    if blocked_bull:
        wins = sum(1 for ts in blocked_bull
                   if next(r for r in out_off["records"] if r["ts"] == ts)["future_basket_ret"] - 0.1 > 0)
        losses = len(blocked_bull) - wins
        sum_fb = sum(next(r for r in out_off["records"] if r["ts"] == ts)["future_basket_ret"]
                     for ts in blocked_bull)
        print(f"  Blocked BULL labels: {len(blocked_bull)} (would-win={wins}, would-lose={losses}, sum_fbask={sum_fb:+.2f}%)")
