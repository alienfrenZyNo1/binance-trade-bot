"""Research-only: A/B compare momentum guard ON vs OFF on fresh 240d data.

Shows which BULL windows the guard blocks, whether they were winners or losers,
and the net effect on selector route return.
"""
import importlib.util
from pathlib import Path
from statistics import median

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


def route_ret(r):
    return ev.route_window_return(
        str(r.get("selector_smoothed", "sideways")),
        future_basket_ret=float(r.get("future_basket_ret", 0)),
        future_btc_ret=float(r.get("future_btc_ret", 0)),
        fee_bps=10.0,
    )


for days in (240,):
    out_off = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), momentum_guard=False, **base)
    out_on = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), momentum_guard=True, **base)
    recs_off = {r["ts"]: r for r in out_off["records"]}
    recs_on = {r["ts"]: r for r in out_on["records"]}

    # Windows where the guard changed the selector regime.
    changed = []
    for ts in recs_off:
        r_off = recs_off[ts]
        r_on = recs_on[ts]
        if r_off["selector_smoothed"] != r_on["selector_smoothed"]:
            changed.append((ts, r_off, r_on))

    print(f"=== {days}d: {len(changed)} windows changed by guard ===")
    net_off = sum(route_ret(recs_off[ts]) for ts in recs_off)
    net_on = sum(route_ret(recs_on[ts]) for ts in recs_on)
    print(f"  net route return: OFF={net_off:+.2f}%  ON={net_on:+.2f}%  (delta={net_on - net_off:+.2f}%)")

    print(f"\n  {'time':16} {'off':6} {'on':6} {'rr_off':7} {'rr_on':7} {'fbask':7} | {'roc24':6} {'roc12':6} {'roc6':6} {'rsi':5}")
    for ts, r_off, r_on in sorted(changed, key=lambda x: route_ret(x[1])):
        e = r_on.get("exhaustion_features", {})
        fb = r_on.get("future_basket_ret", 0)
        print(
            f"  {r_off['time']:16} {r_off['selector_smoothed']:6} {r_on['selector_smoothed']:6} "
            f"{route_ret(r_off):+7.2f} {route_ret(r_on):+7.2f} {fb:+7.2f} | "
            f"{e.get('basket_roc_24h', 0):+6.1f} {e.get('basket_roc_12h', 0):+6.1f} "
            f"{e.get('basket_roc_6h', 0):+6.1f} {e.get('basket_rsi', 0):5.0f}"
        )

    # Aggregate: blocked BULL windows that were winners vs losers
    bull_blocked = [(ts, r_off, r_on) for ts, r_off, r_on in changed
                    if r_off["selector_smoothed"] == ev.BULL and r_on["selector_smoothed"] != ev.BULL]
    if bull_blocked:
        winners = sum(1 for _, r_off, _ in bull_blocked if route_ret(r_off) > 0)
        losers = sum(1 for _, r_off, _ in bull_blocked if route_ret(r_off) <= 0)
        sum_rr = sum(route_ret(r_off) for _, r_off, _ in bull_blocked)
        print(f"\n  Blocked BULL: {len(bull_blocked)} (winners={winners}, losers={losers}, sum_rr={sum_rr:+.2f}%)")
