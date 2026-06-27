"""Research-only: robustness sub-windows + maxDD location for 240d/300d."""
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
    cache_dir=Path(".cache/regime_v2_forward_replay"), days=365, coins=coins, references=refs
)


def route_ret(r):
    return ev.route_window_return(
        str(r.get("selector_smoothed", "sideways")),
        future_basket_ret=float(r.get("future_basket_ret", 0)),
        future_btc_ret=float(r.get("future_btc_ret", 0)),
        fee_bps=10.0,
    )


for days in (240, 300):
    out = ev.evaluate_regime_v2_history(
        fr._slice_last_days(data, days), references=refs, breadth_coins=coins,
        step_hours=12, warmup_hours=120, forward_hours=24, confirmation_samples=2,
        min_confidence=0.60, tune_scorecard=True, tune_route_objective=True, train_fraction=0.60,
        selector_lookback=12, selector_min_objective=0.0,
        selector_max_trailing_drawdown_pct=15.0, selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1, selector_min_trailing_win_rate_pct=50.0,
        selector_trailing_robust_windows=3, selector_min_passing_trailing_windows=3,
        selector_trailing_window_max_drawdown_pct=15.0,
        selector_re_engage_confirmation=True, selector_re_engage_breadth_pct=0.60,
        selector_recent_pnl_lookback_windows=0, selector_recent_pnl_stop_pct=0.0,
        momentum_guard=False,
    )
    recs = out["records"]
    rb = out["route_robustness"]["regime_v2_selector"]
    sel_out = out["route_outcomes"]["regime_v2_selector"]
    print(f"\n===== {days}d: sel return={sel_out['total_return_pct']:+.2f}% maxDD={sel_out['max_drawdown_pct']:.2f}% =====")
    for w in rb["windows"]:
        print(f"  w{w['window_index']}: {w['start_time']} -> {w['end_time']} | ret={w['total_return_pct']:+.2f}% maxDD={w['max_drawdown_pct']:.2f}% {'PASS' if w['passed'] else 'FAIL'}")

    # Find the maxDD trough location: compound returns, find peak-to-trough window.
    eq = 1.0
    peak = 1.0
    peak_idx = 0
    max_dd = 0.0
    max_dd_idx = 0
    max_dd_peak_idx = 0
    curve = []
    for i, r in enumerate(recs):
        eq *= max(0.0, 1.0 + route_ret(r) / 100.0)
        if eq > peak:
            peak = eq
            peak_idx = i
        dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_idx = i
            max_dd_peak_idx = peak_idx
        curve.append(eq)
    print(f"  maxDD {max_dd:.2f}% peaked at idx {max_dd_peak_idx} ({recs[max_dd_peak_idx]['time']}) trough at idx {max_dd_idx} ({recs[max_dd_idx]['time']})")
    # Show the losers in the peak->trough span
    span = recs[max_dd_peak_idx:max_dd_idx + 1]
    losers = [r for r in span if route_ret(r) < 0]
    print(f"  peak->trough span {len(span)} windows, {len(losers)} negative. Top losers:")
    for r in sorted(losers, key=route_ret)[:8]:
        e = r.get("exhaustion_features", {})
        print(f"    {r['time']} sel={r['selector_smoothed']:6s} rr={route_ret(r):+6.2f} fbask={r['future_basket_ret']:+6.2f} rsi={e.get('basket_rsi',0):.0f} roc24={e.get('basket_roc_24h',0):+.1f} div={e.get('btc_basket_divergence',0):+.1f}")
