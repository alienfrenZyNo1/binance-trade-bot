"""Research-only: selectivity of candidate guard signals across FULL 240d record.

We compute, for every record, the candidate guard trigger and compare the route
return of TRIGGERED vs NOT-TRIGGERED windows, split by regime. A good guard
fires mostly on losers (negative avg route return) and spares winners.
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
    cache_dir=Path(".cache/regime_v2_forward_replay"), days=365, coins=coins, references=refs
)


def route_ret(r):
    return ev.route_window_return(
        str(r.get("selector_smoothed", "sideways")),
        future_basket_ret=float(r.get("future_basket_ret", 0)),
        future_btc_ret=float(r.get("future_btc_ret", 0)),
        fee_bps=10.0,
    )


def basket_roc_at(ts, periods):
    rets = []
    for c in coins:
        rows = data.get(c, [])
        closes = [float(r["close"]) for r in rows if int(r["ts"]) <= ts]
        if len(closes) > periods and closes[-periods - 1] > 0:
            rets.append((closes[-1] / closes[-periods - 1] - 1.0) * 100.0)
    return median(rets) if rets else 0.0


def btc_roc_at(ts, periods):
    rows = data.get("BTC", [])
    closes = [float(r["close"]) for r in rows if int(r["ts"]) <= ts]
    if len(closes) <= periods or closes[-periods - 1] <= 0:
        return 0.0
    return (closes[-1] / closes[-periods - 1] - 1.0) * 100.0


out = ev.evaluate_regime_v2_history(
    fr._slice_last_days(data, 240), references=refs, breadth_coins=coins,
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


def summarize(trigger_bull, trigger_bear):
    """trigger_bull/trigger_bear are dicts ts->bool computed from raw-data ROC."""
    for regime, trig, name in ((ev.BULL, trigger_bull, "BULL"), (ev.BEAR, trigger_bear, "BEAR")):
        rs = [r for r in recs if r["selector_smoothed"] == regime]
        blk = [r for r in rs if trig.get(r["ts"], False)]
        unb = [r for r in rs if not trig.get(r["ts"], False)]
        blk_ret = sum(route_ret(r) for r in blk)
        unb_ret = sum(route_ret(r) for r in unb)
        blk_neg = sum(1 for r in blk if route_ret(r) < 0)
        unb_neg = sum(1 for r in unb if route_ret(r) < 0)
        print(
            f"  {name}: blocked={len(blk)} (neg={blk_neg}, sumRR={blk_ret:+.2f}%, avg={blk_ret/max(1,len(blk)):+.2f}) | "
            f"unblocked={len(unb)} (neg={unb_neg}, sumRR={unb_ret:+.2f}%, avg={unb_ret/max(1,len(unb)):+.2f})"
        )


print("=== Candidate A: block BULL if basket roc24 <= 0 ; block BEAR if basket roc6 > roc12 + 1.5 AND roc24 < 0 ===")
tb, tbe = {}, {}
for r in recs:
    ts = r["ts"]
    roc24 = basket_roc_at(ts, 24)
    roc6 = basket_roc_at(ts, 6)
    roc12 = basket_roc_at(ts, 12)
    tb[ts] = roc24 <= 0.0
    tbe[ts] = (roc6 > roc12 + 1.5) and (roc24 < 0.0)
summarize(tb, tbe)

print("\n=== Candidate B: block BULL if basket roc24 <= 1.0 (mildly rolling) ; BEAR if roc24<=-8 AND roc6>0 (V-bounce) ===")
tb, tbe = {}, {}
for r in recs:
    ts = r["ts"]
    roc24 = basket_roc_at(ts, 24)
    roc6 = basket_roc_at(ts, 6)
    tb[ts] = roc24 <= 1.0
    tbe[ts] = (roc24 <= -8.0) and (roc6 > 0.0)
summarize(tb, tbe)

print("\n=== Candidate C: block BULL if roc24<=0 OR (rsi>=72 & roc24<3) ; BEAR if roc24<=-6 & roc6>-1 (mean revert) ===")
tb, tbe = {}, {}
for r in recs:
    ts = r["ts"]
    roc24 = basket_roc_at(ts, 24)
    roc6 = basket_roc_at(ts, 6)
    e = r.get("exhaustion_features", {})
    rsi = e.get("basket_rsi", 50.0)
    tb[ts] = (roc24 <= 0.0) or (rsi >= 72.0 and roc24 < 3.0)
    tbe[ts] = (roc24 <= -6.0) and (roc6 > -1.0)
summarize(tb, tbe)
