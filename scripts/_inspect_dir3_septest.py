"""Research-only: check deceleration signal on winners vs losers (window 3)."""
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


def rr(r):
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
n = len(recs)
w3 = recs[2 * n // 3:]
active = [r for r in w3 if r["selector_route_key"] != "cash"]

# Deceleration guard proposal:
# BULL block when roc6 < roc12 - delta  (decelerating) AND roc24 > 0 (extended)
# BEAR block when roc6 > roc12 + delta  (decelerating decline) AND roc24 < 0
DELTA = 1.5


def signals(r):
    ts = r["ts"]
    roc6 = basket_roc_at(ts, 6)
    roc12 = basket_roc_at(ts, 12)
    roc24 = basket_roc_at(ts, 24)
    sel = r["selector_smoothed"]
    block_bull = (sel == ev.BULL) and (roc6 < roc12 - DELTA) and (roc24 > 0.0)
    block_bear = (sel == ev.BEAR) and (roc6 > roc12 + DELTA) and (roc24 < 0.0)
    return roc6, roc12, roc24, block_bull, block_bear


bull = [r for r in active if r["selector_smoothed"] == ev.BULL]
bear = [r for r in active if r["selector_smoothed"] == ev.BEAR]
print(f"w3 active: {len(active)} (bull={len(bull)}, bear={len(bear)})")

# For BULL windows: how many blocked, and what is the avg route return of blocked vs unblocked?
b_win, b_lose, u_win, u_lose = 0, 0, 0, 0
b_ret, u_ret = 0.0, 0.0
for r in bull:
    _, _, _, blk, _ = signals(r)
    if blk:
        b_ret += rr(r)
        if rr(r) > 0:
            b_win += 1
        else:
            b_lose += 1
    else:
        u_ret += rr(r)
        if rr(r) > 0:
            u_win += 1
        else:
            u_lose += 1
print(f"\nBULL guard: blocked={b_win+b_lose} (win/lose={b_win}/{b_lose}, avg rr={b_ret/max(1,b_win+b_lose):+.2f}); unblocked={u_win+u_lose} (win/lose={u_win}/{u_lose}, avg rr={u_ret/max(1,u_win+u_lose):+.2f})")

b_win, b_lose, u_win, u_lose = 0, 0, 0, 0
b_ret, u_ret = 0.0, 0.0
for r in bear:
    _, _, _, _, blk = signals(r)
    if blk:
        b_ret += rr(r)
        if rr(r) > 0:
            b_win += 1
        else:
            b_lose += 1
    else:
        u_ret += rr(r)
        if rr(r) > 0:
            u_win += 1
        else:
            u_lose += 1
print(f"BEAR guard: blocked={b_win+b_lose} (win/lose={b_win}/{b_lose}, avg rr={b_ret/max(1,b_win+b_lose):+.2f}); unblocked={u_win+u_lose} (win/lose={u_win}/{u_lose}, avg rr={u_ret/max(1,u_win+u_lose):+.2f})")
