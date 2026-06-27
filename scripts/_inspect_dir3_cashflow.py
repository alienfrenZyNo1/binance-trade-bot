"""Research-only: what does the selector actually DO on this window?

The selector win rate is ~3%, suggesting it's in cash almost the entire time.
This traces the selector route composition and WHY it's in cash so often
(block reasons), to understand whether a label-level guard can even help.
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
    momentum_guard=False,
)

for days in (240,):
    out = ev.evaluate_regime_v2_history(fr._slice_last_days(data, days), **base)
    recs = out["records"]
    print(f"=== {days}d: {len(recs)} records ===")

    # Selector route composition
    sel_routes = Counter(r.get("selector_smoothed", "?") for r in recs)
    print(f"Selector route dist: {dict(sel_routes)}")

    # What route_key (source) does the selector pick?
    sel_keys = Counter(r.get("selector_route_key", "?") for r in recs)
    print(f"Selector route_key dist: {dict(sel_keys)}")

    # Block reasons
    reasons = Counter()
    for r in recs:
        br = r.get("selector_block_reason") or ""
        if br:
            # Bucket the reasons
            if "win rate" in br: reasons["win_rate_gate"] += 1
            elif "drawdown" in br.lower(): reasons["drawdown_gate"] += 1
            elif "equity" in br.lower(): reasons["equity_stop"] += 1
            elif "cooldown" in br.lower(): reasons["cooldown"] += 1
            elif "objective" in br.lower(): reasons["objective_gate"] += 1
            elif "confirmation" in br.lower(): reasons["confirmation_gate"] += 1
            elif "robust" in br.lower(): reasons["robust_gate"] += 1
            else: reasons[f"other: {br[:40]}"] += 1
    print(f"Block reason buckets: {dict(reasons)}")

    # When the selector is NOT cash, what regime is it and what's the return?
    active = [r for r in recs if r.get("selector_route_key") not in ("cash", "")]
    print(f"\nActive (non-cash) selector windows: {len(active)}")
    for r in active[:20]:
        ret = ev.route_window_return(str(r["selector_smoothed"]),
            float(r["future_basket_ret"]), float(r["future_btc_ret"]), fee_bps=10.0)
        print(f"  {r['time']}: route={r['selector_route_key']:20s} regime={r['selector_smoothed']:8s} ret={ret:+.2f}%")

    # The selector is mostly cash. Where does the maxDD come from?
    # Compute the equity curve
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    max_dd_idx = -1
    for i, r in enumerate(recs):
        ret = ev.route_window_return(str(r.get("selector_smoothed","sideways")),
            float(r["future_basket_ret"]), float(r["future_btc_ret"]), fee_bps=10.0)
        equity *= max(0.0, 1.0 + ret/100.0)
        peak = max(peak, equity)
        dd = (peak - equity)/peak*100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_idx = i
    print(f"\nMaxDD = {max_dd:.2f}% at record {max_dd_idx} ({recs[max_dd_idx]['time'] if max_dd_idx>=0 else '?'})")

    # Trace the equity curve around the maxDD point
    print(f"\n--- equity curve around maxDD (idx {max_dd_idx}) ---")
    equity = 1.0
    peak = 1.0
    curve = []
    for i, r in enumerate(recs):
        ret = ev.route_window_return(str(r.get("selector_smoothed","sideways")),
            float(r["future_basket_ret"]), float(r["future_btc_ret"]), fee_bps=10.0)
        equity *= max(0.0, 1.0 + ret/100.0)
        peak = max(peak, equity)
        dd = (peak - equity)/peak*100.0 if peak > 0 else 0.0
        curve.append((i, r["time"], r.get("selector_smoothed","?"), ret, equity, dd))
    start = max(0, max_dd_idx - 15)
    end = min(len(curve), max_dd_idx + 5)
    for i, t, reg, ret, eq, dd in curve[start:end]:
        marker = " <<< MAXDD" if i == max_dd_idx else ""
        print(f"  [{i:3d}] {t} regime={reg:8s} ret={ret:+7.2f}% eq={eq:.4f} dd={dd:6.2f}%{marker}")
