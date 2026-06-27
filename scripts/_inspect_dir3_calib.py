"""Research-only: calibrate momentum-guard thresholds against realized losses.

For each BULL and BEAR window, compute the route return and the exhaustion
features. Then sweep candidate threshold settings and report, for each, how many
LOSING windows it blocks vs how many WINNING windows it kills (selectivity),
plus the net return impact. The goal is a threshold set that blocks mostly
losers and spares winners.
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
)

FEE = 10.0

def route_ret(regime, basket, btc):
    return ev.route_window_return(str(regime), future_basket_ret=float(basket), future_btc_ret=float(btc), fee_bps=FEE)

for days in (240,):
    out = ev.evaluate_regime_v2_history(
        fr._slice_last_days(data, days), references=refs, breadth_coins=coins,
        step_hours=12, warmup_hours=120, forward_hours=24, confirmation_samples=2,
        min_confidence=0.60, tune_scorecard=True, tune_route_objective=True, train_fraction=0.60,
        momentum_guard=False,  # PLAIN labels for calibration
    )
    recs = out["records"]
    print(f"=== {days}d: {len(recs)} records ===")

    bull = [r for r in recs if r["v2_smoothed"] == ev.BULL]
    bear = [r for r in recs if r["v2_smoothed"] == ev.BEAR]

    def ret_of(r, regime):
        return route_ret(regime, r["future_basket_ret"], r["future_btc_ret"])

    # BULL: net return if we block a subset to SIDEWAYS (0 ret). Blocking a BULL
    # window changes its return from route_ret(BULL) to 0.0 (minus no fee since
    # sideways = cash). Actually SIDEWAYS route return: let's use ev.
    # The selector picks the best route, but for label-level calib, blocking
    # BULL->SIDEWAYS changes that window from BULL-return to cash (0).
    bull_rets = [(r, ret_of(r, ev.BULL)) for r in bull]
    bull_losers = [(r, ret) for r, ret in bull_rets if ret < 0]
    bull_winners = [(r, ret) for r, ret in bull_rets if ret >= 0]
    print(f"\nBULL: {len(bull)} total, {len(bull_losers)} losers, {len(bull_winners)} winners")
    print(f"  total BULL return if all traded: {sum(ret for _,ret in bull_rets):+.2f}%")
    print(f"  loser return sum: {sum(ret for _,ret in bull_losers):+.2f}% (n={len(bull_losers)})")
    print(f"  winner return sum: {sum(ret for _,ret in bull_winners):+.2f}% (n={len(bull_winners)})")

    # Sweep BULL-blocking triggers. For each, count losers blocked, winners
    # blocked, and net return delta (blocking adds -ret to total, i.e. removes
    # the loss/gain).
    print("\n--- BULL-blocking trigger sweep (block -> cash) ---")
    print(f"{'trigger':<55s} {'blk':>4s} {'los':>4s} {'win':>4s} {'netΔ':>8s}")
    def bull_sweep(name, cond):
        blocked_loser = 0
        blocked_winner = 0
        net = 0.0
        for r, ret in bull_rets:
            ex = r["exhaustion_features"]
            if cond(ex):
                if ret < 0:
                    blocked_loser += 1
                else:
                    blocked_winner += 1
                net += -ret  # removing this window's return
        print(f"{name:<55s} {blocked_loser+blocked_winner:>4d} {blocked_loser:>4d} {blocked_winner:>4d} {net:>+8.2f}")

    roc24 = lambda ex: float(ex.get("basket_roc_24h", 0))
    roc6 = lambda ex: float(ex.get("basket_roc_6h", 0))
    roc12 = lambda ex: float(ex.get("basket_roc_12h", 0))
    decel = lambda ex: float(ex.get("basket_deceleration", 0))

    # Current defaults
    bull_sweep("rollover: roc24<=-1 & roc12>=2 [CURRENT]", lambda ex: roc24(ex)<=-1 and roc12(ex)>=2)
    bull_sweep("stall: roc6<=-1.5 & roc12>=2 [CURRENT]", lambda ex: roc6(ex)<=-1.5 and roc12(ex)>=2)
    # Relax precondition
    bull_sweep("rollover: roc24<=-1 & roc12>=1", lambda ex: roc24(ex)<=-1 and roc12(ex)>=1)
    bull_sweep("rollover: roc24<=-1 & roc12>=0.5", lambda ex: roc24(ex)<=-1 and roc12(ex)>=0.5)
    bull_sweep("rollover: roc24<=0 & roc12>=1", lambda ex: roc24(ex)<=0 and roc12(ex)>=1)
    # Deceleration-based (roc6 < roc12 by a margin, i.e. decel negative)
    bull_sweep("decel<=-1.5 & roc12>=1", lambda ex: decel(ex)<=-1.5 and roc12(ex)>=1)
    bull_sweep("decel<=-1.0 & roc12>=1", lambda ex: decel(ex)<=-1.0 and roc12(ex)>=1)
    bull_sweep("decel<=-0.5 & roc12>=1", lambda ex: decel(ex)<=-0.5 and roc12(ex)>=1)
    bull_sweep("decel<=-1.0 & roc12>=0.5", lambda ex: decel(ex)<=-1.0 and roc12(ex)>=0.5)
    bull_sweep("decel<=-1.5 & roc12>=0.5", lambda ex: decel(ex)<=-1.5 and roc12(ex)>=0.5)
    bull_sweep("decel<=-2.0", lambda ex: decel(ex)<=-2.0)
    bull_sweep("roc6<=0 & roc12>=1", lambda ex: roc6(ex)<=0 and roc12(ex)>=1)
    bull_sweep("roc6<=-0.5 & roc12>=1", lambda ex: roc6(ex)<=-0.5 and roc12(ex)>=1)
    bull_sweep("roc6<=-1.0 & roc12>=1", lambda ex: roc6(ex)<=-1.0 and roc12(ex)>=1)
    # Combined: stall OR rollover (union of two relaxed)
    bull_sweep("union: (roc24<=0&roc12>=1) | (roc6<=-1&roc12>=1)", lambda ex: (roc24(ex)<=0 and roc12(ex)>=1) or (roc6(ex)<=-1 and roc12(ex)>=1))

    # BEAR blocking sweep
    bear_rets = [(r, ret_of(r, ev.BEAR)) for r in bear]
    bear_losers = [(r, ret) for r, ret in bear_rets if ret < 0]
    print(f"\nBEAR: {len(bear)} total, {len(bear_losers)} losers")
    print(f"  total BEAR return: {sum(ret for _,ret in bear_rets):+.2f}%")
    print(f"  loser return sum: {sum(ret for _,ret in bear_losers):+.2f}%")

    print("\n--- BEAR-blocking trigger sweep ---")
    def bear_sweep(name, cond):
        bl = bw = 0
        net = 0.0
        for r, ret in bear_rets:
            ex = r["exhaustion_features"]
            if cond(ex):
                if ret < 0: bl += 1
                else: bw += 1
                net += -ret
        print(f"{name:<55s} {bl+bw:>4d} {bl:>4d} {bw:>4d} {net:>+8.2f}")

    btcroc = lambda ex: float(ex.get("btc_roc_24h", 0))
    div = lambda ex: float(ex.get("btc_basket_divergence", 0))
    bear_sweep("divergence: btcroc>=2 & div>=3 [CURRENT]", lambda ex: btcroc(ex)>=2 and div(ex)>=3)
    bear_sweep("divergence: btcroc>=1 & div>=2", lambda ex: btcroc(ex)>=1 and div(ex)>=2)
    bear_sweep("divergence: btcroc>=1 & div>=1.5", lambda ex: btcroc(ex)>=1 and div(ex)>=1.5)
    bear_sweep("divergence: div>=2", lambda ex: div(ex)>=2)
    bear_sweep("meanrev: roc24<=-8 & roc6>1 [CURRENT]", lambda ex: roc24(ex)<=-8 and roc6(ex)>1)
    bear_sweep("meanrev: roc24<=-5 & roc6>0.5", lambda ex: roc24(ex)<=-5 and roc6(ex)>0.5)
