"""Research-only: inspect short-horizon deceleration on losing windows."""
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


def rr(r):
    return ev.route_window_return(
        str(r.get("selector_smoothed", "sideways")),
        future_basket_ret=float(r.get("future_basket_ret", 0)),
        future_btc_ret=float(r.get("future_btc_ret", 0)),
        fee_bps=10.0,
    )


# Add a short-horizon ROC feature check directly on the raw data at each record ts.
def basket_roc_at(ts, periods):
    rets = []
    for c in coins:
        rows = data.get(c, [])
        closes = [float(r["close"]) for r in rows if int(r["ts"]) <= ts]
        if len(closes) > periods and closes[-periods - 1] > 0:
            rets.append((closes[-1] / closes[-periods - 1] - 1.0) * 100.0)
    rets = [x for x in rets]
    from statistics import median
    return median(rets) if rets else 0.0


def btc_roc_at(ts, periods):
    rows = data.get("BTC", [])
    closes = [float(r["close"]) for r in rows if int(r["ts"]) <= ts]
    if len(closes) <= periods or closes[-periods - 1] <= 0:
        return 0.0
    return (closes[-1] / closes[-periods - 1] - 1.0) * 100.0


out = ev.evaluate_regime_v2_history(
    fr._slice_last_days(data, 240),
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
losses = [r for r in w3 if r["selector_route_key"] != "cash" and rr(r) < 0]
print(f"=== false-BULL / false-BEAR losing windows: deceleration signature ===")
print(f"{'time':16} {'sel':6} {'rr':7} {'fbask':7} | {'roc6':6} {'roc12':6} {'roc24':6} | {'btc6':6} {'btc12':6} {'btc24':6}")
for r in sorted(losses, key=rr)[:14]:
    ts = r["ts"]
    roc6 = basket_roc_at(ts, 6)
    roc12 = basket_roc_at(ts, 12)
    roc24 = basket_roc_at(ts, 24)
    btc6 = btc_roc_at(ts, 6)
    btc12 = btc_roc_at(ts, 12)
    btc24 = btc_roc_at(ts, 24)
    print(
        f"{r['time']:16} {r['selector_smoothed']:6} {rr(r):+7.2f} {r['future_basket_ret']:+7.2f} | "
        f"{roc6:+6.1f} {roc12:+6.1f} {roc24:+6.1f} | {btc6:+6.1f} {btc12:+6.1f} {btc24:+6.1f}"
    )
