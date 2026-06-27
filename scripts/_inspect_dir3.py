"""Research-only inspection helper for direction #3 (NOT committed)."""
import importlib.util, json
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


def rr(r):
    return ev.route_window_return(
        str(r.get("selector_smoothed", "sideways")),
        future_basket_ret=float(r.get("future_basket_ret", 0)),
        future_btc_ret=float(r.get("future_btc_ret", 0)),
        fee_bps=10.0,
    )


for days in (240,):
    out = ev.evaluate_regime_v2_history(
        fr._slice_last_days(data, days),
        references=refs,
        breadth_coins=coins,
        step_hours=12,
        warmup_hours=120,
        forward_hours=24,
        confirmation_samples=2,
        min_confidence=0.60,
        tune_scorecard=True,
        tune_route_objective=True,
        train_fraction=0.60,
        selector_lookback=12,
        selector_min_objective=0.0,
        selector_max_trailing_drawdown_pct=15.0,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
        selector_min_trailing_win_rate_pct=50.0,
        selector_trailing_robust_windows=3,
        selector_min_passing_trailing_windows=3,
        selector_trailing_window_max_drawdown_pct=15.0,
        selector_re_engage_confirmation=True,
        selector_re_engage_breadth_pct=0.60,
        selector_recent_pnl_lookback_windows=0,
        selector_recent_pnl_stop_pct=0.0,
        momentum_guard=False,
    )
    recs = out["records"]
    n = len(recs)
    w3 = recs[2 * n // 3:]
    print(f"=== {days}d: total={n}, window3={len(w3)} ===")
    losses = [r for r in w3 if r["selector_route_key"] != "cash" and rr(r) < 0]
    print(f"{len(losses)} active-losing in w3; net w3 route ret={sum(rr(r) for r in w3):+.2f}%")
    for r in sorted(losses, key=rr)[:14]:
        e = r.get("exhaustion_features", {})
        print(
            f"  {r['time']} sel={r['selector_smoothed']:8s} rr={rr(r):+6.2f} "
            f"fbask={r['future_basket_ret']:+6.2f} | rsi={e.get('basket_rsi',0):3.0f} "
            f"overext={e.get('basket_overextended_pct',0):.2f} roc24={e.get('basket_roc_24h',0):+6.1f} | "
            f"btcroc={e.get('btc_roc_24h',0):+6.1f} div={e.get('btc_basket_divergence',0):+6.1f}"
        )
