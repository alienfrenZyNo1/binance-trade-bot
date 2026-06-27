#!/usr/bin/env python3
"""Compounding portfolio wrapper for the bear-futures trailing-stop scalper.

The raw scalper (scripts/research_bear_futures_backtester.py) logged 808
*parallel* trades per leverage setting, each simulated as if it had the full
$1000 balance to itself (margin = 40% of $1000). Its reported PnL is the
*naive sum* of independent trades -- which is unrealizable because up to 11
trades run concurrently, so they must share one pot of capital.

This wrapper builds a single compounding equity curve from those trade records
under several realistic capital-allocation schemes, then computes true
risk-adjusted metrics (annualized return, Sharpe, MaxDD, Calmar, profit factor)
and applies the alpha gate: Sharpe > 1.0 AND Ann > 50% AND MaxDD < 20%.

RESEARCH ONLY. No live trading, no network, no config changes.

Usage:
    python scripts/research_scalper_compounding.py
    python scripts/research_scalper_compounding.py --leverage 3 --max-concurrent 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "docs" / "research"
HOUR_MS = 3600 * 1000
DAY_MS = 86400 * 1000

# Files keyed by their internal leverage value.
LEVERAGE_FILES = {
    1: DATA_DIR / "bear-futures-short-data-1x-nooi.json",
    2: DATA_DIR / "bear-futures-short-data-2x.json",
    3: DATA_DIR / "bear-futures-short-data-3x-nooi.json",
}

# --- cost / funding assumptions (conservative) -------------------------------
# The per-trade records already include 0.075% taker fee + 0.05% slippage per
# side in net_pnl. Funding is NOT in the records. We conservatively estimate
# 0.010% per 8h on short notional for the hold duration (a short that pays
# funding; conservative because in bear regimes shorts often *receive* funding).
FUNDING_RATE_PER_8H = 0.00010  # 0.010% per 8h funding interval
FUNDING_INTERVAL_MS = 8 * HOUR_MS

# Risk-free rate for Sharpe (annualized). Use 0 to keep numbers clean & because
# the gate is specified in absolute terms; we report both.
RF_ANNUAL = 0.0

# Trading days used for annualization (crypto trades 24/7).
PERIODS_PER_YEAR_8760 = 8760  # hourly
PERIODS_PER_YEAR_365 = 365


# =============================================================================
# Data loading
# =============================================================================
def load_records(path: Path) -> list[dict[str, Any]]:
    with open(path) as fh:
        payload = json.load(fh)
    return payload["records"]


def trade_durations_hours(records: list[dict[str, Any]]) -> list[float]:
    return [(r["exit_ts"] - r["entry_ts"]) / HOUR_MS for r in records]


# =============================================================================
# Core trade representation
# =============================================================================
@dataclass
class Trade:
    """A normalized trade event for the compounding simulator.

    r_margin_full: fractional return on the *margin posted* for this trade at
        the scalper's native sizing (margin = 40% of $1000, notional = margin*lev).
        This is leverage-invariant in *price* terms but scales linearly with the
        leverage embedded in the records. We use it as the per-unit-capital
        return and rescale by the fraction of equity we actually allocate.
    dd_margin_full: worst fractional drawdown on the posted margin.
    """
    entry_ts: int
    exit_ts: int
    symbol: str
    exit_reason: str
    pnl_pct: float          # = net_pnl / 1000 * 100, as recorded
    max_drawdown_pct: float
    leverage: float
    r_margin_full: float    # fractional return on margin (= pnl/net margin)
    dd_margin_full: float
    hold_hours: float


def normalize_trade(rec: dict[str, Any]) -> Trade:
    mmp = rec["params"]["max_margin_pct"]  # 0.40
    margin = rec["margin"]
    net_pnl = rec["net_pnl"]
    # fractional return per unit of margin posted (leverage-invariant price
    # return * leverage factor). r_margin_full scales linearly w/ leverage.
    r_margin_full = net_pnl / margin if margin > 0 else 0.0
    # drawdown as fraction of margin
    dd_margin_full = (rec["max_drawdown_pct"] / 100.0) / mmp if mmp > 0 else 0.0
    return Trade(
        entry_ts=int(rec["entry_ts"]),
        exit_ts=int(rec["exit_ts"]),
        symbol=str(rec["symbol"]),
        exit_reason=str(rec.get("exit_reason", "")),
        pnl_pct=float(rec["pnl_pct"]),
        max_drawdown_pct=float(rec["max_drawdown_pct"]),
        leverage=float(rec["leverage"]),
        r_margin_full=r_margin_full,
        dd_margin_full=dd_margin_full,
        hold_hours=(int(rec["exit_ts"]) - int(rec["entry_ts"])) / HOUR_MS,
    )


# =============================================================================
# Sizing methods
# =============================================================================
def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float,
                   fraction: float = 0.25) -> float:
    """Fractional Kelly: fraction of capital to risk on margin.

    avg_win, avg_loss are fractional returns on margin (avg_loss > 0 magnitude).
    b = avg_win / avg_loss (payoff ratio). Full Kelly f* = (p*b - q) / b.
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    b = avg_win / avg_loss
    p = win_rate
    q = 1.0 - win_rate
    f_star = (p * b - q) / b
    f_star = max(0.0, f_star)
    return f_star * fraction


def estimate_kelly(records: list[Trade], fraction: float = 0.25) -> float:
    rs = [t.r_margin_full for t in records]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    if not wins or not losses:
        return 0.0
    wr = len(wins) / len(rs)
    return kelly_fraction(wr, sum(wins) / len(wins), sum(losses) / len(losses), fraction)


# =============================================================================
# Compounding simulator (single equity curve)
# =============================================================================
@dataclass
class SimConfig:
    method: str = "fixed_frac"      # fixed_frac | kelly | equal_notional
    risk_per_trade: float = 0.02    # fraction of equity posted as margin per slot
    max_concurrent: int = 3         # cap on simultaneously open positions
    initial_equity: float = 1000.0
    funding_on: bool = False
    kelly_fraction_cap: float = 0.25  # quarter-Kelly by default


@dataclass
class SimResult:
    config: SimConfig
    trades_taken: int
    trades_skipped: int             # skipped due to concurrency cap
    final_equity: float
    total_return: float             # fraction
    annualized_return: float        # fraction
    sharpe: float                   # annualized, rf=0
    max_drawdown: float             # fraction
    calmar: float
    profit_factor: float
    win_rate: float
    equity_curve: list[tuple[int, float]]   # (ts_ms, equity)
    realized_trades: list[dict[str, Any]]   # per-trade realized pnl on equity
    peak_concurrency: int
    avg_concurrency: float


def simulate_compounding(
    trades: list[Trade],
    cfg: SimConfig,
) -> SimResult:
    """Walk through trades chronologically, sizing each off CURRENT equity.

    Sizing (fraction of current equity posted as margin):
      fixed_frac     : risk_per_trade            (e.g. 2%)
      kelly          : kelly_sizing (capped at risk_per_trade as a hard ceiling)
      equal_notional : (1 / max_concurrent)      (equal-weight slot)

    Per-trade realized fractional equity return:
        r_eq = size_frac * r_margin_full
    which already embeds the leverage baked into the records. Funding cost
    (if cfg.funding_on) is subtracted as size_frac * funding_cost_margin.
    """
    sorted_trades = sorted(trades, key=lambda t: (t.entry_ts, t.symbol))

    # Pre-compute kelly size fraction once.
    kelly_size_frac = 0.0
    if cfg.method == "kelly":
        kelly_size_frac = estimate_kelly(sorted_trades, cfg.kelly_fraction_cap)

    equity = cfg.initial_equity
    eq_curve: list[tuple[int, float]] = []
    realized: list[dict[str, Any]] = []
    peak_conc = 0
    open_slots: list[tuple[int, Trade, float, float]] = []  # (exit_ts, trade, size_frac, entry_eq)
    taken = 0
    skipped = 0
    conc_samples: list[int] = []

    # Generate ordered event timeline for an accurate equity mark.
    ts0 = sorted_trades[0].entry_ts if sorted_trades else 0
    ts1 = max((t.exit_ts for t in sorted_trades), default=ts0)
    # sample hourly
    n_hours = (ts1 - ts0) // HOUR_MS + 1

    # We process trades in entry order; a trade is admitted only if fewer than
    # max_concurrent positions are currently open (exit-respecting).
    # Mark equity at each hour using mark-to-close of open positions' mid-life.
    # For an honest *realized* equity curve, we realize PnL at exit_ts and
    # carry a conservative unrealized drawdown between entry and exit.
    pending = list(sorted_trades)
    # event queue: (ts, kind, trade)
    events: list[tuple[int, int, Trade]] = []
    for t in sorted_trades:
        events.append((t.entry_ts, 1, t))
        events.append((t.exit_ts, 0, t))
    # exits before entries at same ts
    events.sort(key=lambda e: (e[0], e[1]))

    # Track open positions with their entry equity and size_frac.
    open_positions: dict[int, list[tuple[Trade, float, float]]] = {}
    # For marking: approximate unrealized PnL linearly across hold by storing
    # each trade's realized r_margin_full and its max adverse excursion.
    marks: list[tuple[int, float]] = []
    cur_equity = equity
    # running tally of realized equity at each event
    for ts, kind, trade in events:
        if kind == 1:  # entry
            n_open = sum(len(v) for v in open_positions.values())
            conc_samples.append(n_open)
            if n_open < cfg.max_concurrent:
                # size off current realized equity
                if cfg.method == "fixed_frac":
                    size_frac = cfg.risk_per_trade
                elif cfg.method == "kelly":
                    size_frac = min(kelly_size_frac, cfg.risk_per_trade)
                elif cfg.method == "equal_notional":
                    size_frac = 1.0 / cfg.max_concurrent
                else:
                    size_frac = cfg.risk_per_trade
                open_positions.setdefault(ts, []).append((trade, size_frac, equity))
                taken += 1
            else:
                skipped += 1
        else:  # exit
            # realize pnl
            for pos_list in list(open_positions.values()):
                remaining = []
                for (tr, sf, entry_eq) in pos_list:
                    if tr is trade:
                        r_eq = sf * tr.r_margin_full
                        if cfg.funding_on:
                            funding = funding_cost_for_trade(tr, sf)
                            r_eq -= funding
                        equity += entry_eq * r_eq  # realize on the equity at entry
                        realized.append({
                            "entry_ts": tr.entry_ts,
                            "exit_ts": tr.exit_ts,
                            "symbol": tr.symbol,
                            "exit_reason": tr.exit_reason,
                            "size_frac": sf,
                            "r_margin_full": tr.r_margin_full,
                            "r_equity": r_eq,
                            "pnl_dollars": entry_eq * r_eq,
                            "equity_after": equity,
                        })
                    else:
                        remaining.append((tr, sf, entry_eq))
                if pos_list:
                    pos_list[:] = remaining
            open_positions = {k: v for k, v in open_positions.items() if v}
        # mark equity at this event (realized only; unrealized shown via DD sim below)
        marks.append((ts, equity))
        peak_conc = max(peak_conc, sum(len(v) for v in open_positions.values()))

    # Build hourly equity curve with UNREALIZED drawdown modeling for honest MaxDD.
    # We mark-to-market open positions by linearly interpolating their path:
    # assume the trade's max_drawdown_pct (on margin) is hit at mid-hold, then
    # recovers to the realized r_margin_full at exit. This gives a conservative
    # intratrade equity dip for MaxDD.
    eq_hourly, dd_hourly = build_hourly_equity_with_unrealized(
        sorted_trades, cfg, marks, realized, ts0, ts1
    )

    final_equity = equity
    total_return = final_equity / cfg.initial_equity - 1.0

    # Metrics from hourly curve
    eq_arr = np.array([e for _, e in eq_hourly], dtype=float)
    ts_arr = np.array([t for t, _ in eq_hourly], dtype=float)
    ann_return, sharpe, max_dd, calmar = compute_metrics(eq_arr, ts_arr, cfg.initial_equity)

    # profit factor & win rate from realized trades
    pf, wr = profit_factor_winrate(realized)

    return SimResult(
        config=cfg,
        trades_taken=taken,
        trades_skipped=skipped,
        final_equity=final_equity,
        total_return=total_return,
        annualized_return=ann_return,
        sharpe=sharpe,
        max_drawdown=max_dd,
        calmar=calmar,
        profit_factor=pf,
        win_rate=wr,
        equity_curve=eq_hourly,
        realized_trades=realized,
        peak_concurrency=peak_conc,
        avg_concurrency=float(np.mean(conc_samples)) if conc_samples else 0.0,
    )


def funding_cost_for_trade(trade: Trade, size_frac: float) -> float:
    """Conservative funding cost as a fraction of equity allocated.

    Short pays ~0.010%/8h on notional. Notional = margin*lev. As a fraction of
    the *equity* posted (size_frac * entry_equity), the funding drag is:
        funding/eq = (notional * rate * n_intervals) / eq
                   = (size_frac*eq * lev * rate * n_intervals) / (size_frac*eq)
                   = lev * rate * n_intervals
    i.e. independent of size_frac, scales w/ leverage & hold time.
    """
    n_intervals = max(1.0, trade.hold_hours / 8.0)
    return trade.leverage * FUNDING_RATE_PER_8H * n_intervals


def build_hourly_equity_with_unrealized(
    trades: list[Trade],
    cfg: SimConfig,
    realized_marks: list[tuple[int, float]],
    realized: list[dict[str, Any]],
    ts0: int,
    ts1: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Hourly equity curve: realized equity between events + unrealized MTM dip.

    Conservative model: for each open trade, assume its worst drawdown (on
    margin) is reached at the midpoint of the hold, recovering linearly to the
    realized return at exit. The equity dip = entry_equity * size_frac *
    (fraction of margin DD at that hour).
    """
    HOUR = HOUR_MS
    n_hours = (ts1 - ts0) // HOUR + 1
    # Realized equity at each hour: step from realized_marks
    realized_by_ts = {ts: eq for ts, eq in realized_marks}
    # build a step function of realized equity
    sorted_marks = sorted(realized_marks)
    eq_hourly: list[tuple[int, float]] = []
    dd_series: list[tuple[int, float]] = []

    # map: trade -> (size_frac, entry_equity) by matching entry_ts+symbol
    trade_meta: dict[tuple[int, str], tuple[float, float]] = {}
    for rz in realized:
        trade_meta[(rz["entry_ts"], rz["symbol"])] = (rz["size_frac"], rz["pnl_dollars"] / max(rz["r_equity"], 1e-12) if rz["r_equity"] != 0 else 0.0)
    # simpler: recompute size_frac & entry_eq from realized list directly
    meta2: dict[tuple[int, str], tuple[float, float]] = {}
    for rz in realized:
        # entry_equity = pnl_dollars / r_equity (if r_equity!=0)
        sf = rz["size_frac"]
        ee = rz["pnl_dollars"] / rz["r_equity"] if rz["r_equity"] != 0 else cfg.initial_equity
        meta2[(rz["entry_ts"], rz["symbol"])] = (sf, ee)

    # Build hourly marks
    for i in range(n_hours):
        ts = ts0 + i * HOUR
        # realized equity = last mark <= ts
        eq_real = cfg.initial_equity
        for mts, meq in sorted_marks:
            if mts <= ts:
                eq_real = meq
            else:
                break
        # add unrealized MTM of currently-open trades
        unreal = 0.0
        for tr in trades:
            if tr.entry_ts <= ts < tr.exit_ts:
                key = (tr.entry_ts, tr.symbol)
                if key not in meta2:
                    continue
                sf, ee = meta2[key]
                # fraction of hold elapsed
                frac = (ts - tr.entry_ts) / max(tr.exit_ts - tr.entry_ts, 1)
                # DD model: triangle peaking at mid
                if frac <= 0.5:
                    dd_frac = frac / 0.5
                else:
                    dd_frac = (1.0 - frac) / 0.5
                # max adverse on margin, scaled to equity
                adverse_eq = ee * sf * tr.dd_margin_full * dd_frac
                # also accrue linearly toward realized return
                lin_ret = ee * sf * tr.r_margin_full * frac
                unreal += lin_ret - adverse_eq
        eq_mark = eq_real + unreal
        eq_hourly.append((ts, eq_mark))
    return eq_hourly, dd_series


def compute_metrics(
    eq_arr: np.ndarray,
    ts_arr: np.ndarray,
    initial_equity: float,
) -> tuple[float, float, float, float]:
    """Annualized return, Sharpe, MaxDD, Calmar from an hourly equity curve."""
    if len(eq_arr) < 2:
        return 0.0, 0.0, 0.0, 0.0
    # hourly returns
    rets = np.diff(eq_arr) / eq_arr[:-1]
    rets = rets[np.isfinite(rets)]
    n_hours = len(eq_arr)
    years = n_hours / PERIODS_PER_YEAR_8760
    if years <= 0:
        return 0.0, 0.0, 0.0, 0.0
    total_return = eq_arr[-1] / initial_equity - 1.0
    if total_return <= -1.0:
        ann_return = -1.0
    else:
        ann_return = (eq_arr[-1] / initial_equity) ** (1.0 / years) - 1.0
    # Sharpe (hourly, rf=0, annualized by sqrt(8760))
    if rets.std() > 0:
        sharpe = (rets.mean() / rets.std()) * math.sqrt(PERIODS_PER_YEAR_8760)
    else:
        sharpe = 0.0
    # MaxDD
    peak = np.maximum.accumulate(eq_arr)
    dd = (eq_arr - peak) / peak
    max_dd = float(-np.min(dd)) if len(dd) else 0.0
    # Calmar
    calmar = ann_return / max_dd if max_dd > 0 else 0.0
    return float(ann_return), float(sharpe), max_dd, float(calmar)


def profit_factor_winrate(realized: list[dict[str, Any]]) -> tuple[float, float]:
    if not realized:
        return 0.0, 0.0
    gains = sum(r["pnl_dollars"] for r in realized if r["pnl_dollars"] > 0)
    losses = -sum(r["pnl_dollars"] for r in realized if r["pnl_dollars"] <= 0)
    pf = gains / losses if losses > 0 else float("inf")
    wins = sum(1 for r in realized if r["pnl_dollars"] > 0)
    wr = wins / len(realized)
    return float(pf), float(wr)


def passes_gate(sharpe: float, ann: float, max_dd: float) -> bool:
    return sharpe > 1.0 and ann > 0.50 and max_dd < 0.20


# =============================================================================
# Reporting
# =============================================================================
def fmt_pct(x: float) -> str:
    if x == float("inf"):
        return "inf"
    return f"{x * 100:.1f}%"


def result_summary(r: SimResult) -> dict[str, Any]:
    gate = passes_gate(r.sharpe, r.annualized_return, r.max_drawdown)
    return {
        "method": r.config.method,
        "leverage_embedded": None,  # filled by caller
        "max_concurrent": r.config.max_concurrent,
        "risk_per_trade": r.config.risk_per_trade,
        "funding_on": r.config.funding_on,
        "trades_taken": r.trades_taken,
        "trades_skipped": r.trades_skipped,
        "final_equity": round(r.final_equity, 2),
        "total_return": round(r.total_return * 100, 2),
        "annualized_return_pct": round(r.annualized_return * 100, 2),
        "sharpe": round(r.sharpe, 3),
        "max_drawdown_pct": round(r.max_drawdown * 100, 2),
        "calmar": round(r.calmar, 3),
        "profit_factor": round(r.profit_factor, 3),
        "win_rate_pct": round(r.win_rate * 100, 2),
        "peak_concurrency": r.peak_concurrency,
        "avg_concurrency": round(r.avg_concurrency, 2),
        "passes_gate": gate,
    }


def print_table(rows: list[dict[str, Any]], title: str = "") -> None:
    if title:
        print(f"\n=== {title} ===")
    if not rows:
        print("  (no rows)")
        return
    cols = ["method", "max_concurrent", "risk_per_trade", "annualized_return_pct",
            "sharpe", "max_drawdown_pct", "calmar", "profit_factor", "win_rate_pct",
            "trades_taken", "passes_gate"]
    headers = {
        "method": "method",
        "max_concurrent": "conc",
        "risk_per_trade": "risk%",
        "annualized_return_pct": "Ann%",
        "sharpe": "Sharpe",
        "max_drawdown_pct": "MaxDD%",
        "calmar": "Calmar",
        "profit_factor": "PF",
        "win_rate_pct": "Win%",
        "trades_taken": "n",
        "passes_gate": "GATE",
    }
    widths = {c: max(len(headers[c]), max(len(str(row.get(c, ""))) for row in rows)) for c in cols}
    line = "  ".join(f"{headers[c]:>{widths[c]}}" for c in cols)
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(f"{str(row.get(c, '')):>{widths[c]}}" for c in cols))


# =============================================================================
# Main
# =============================================================================
def run_grid(leverage: int, trades: list[Trade], *, funding_on: bool = False) -> list[SimResult]:
    results: list[SimResult] = []
    methods = ["fixed_frac", "kelly", "equal_notional"]
    for method in methods:
        for mc in (2, 3, 5, 8):
            for rpt in (0.01, 0.02, 0.05):
                cfg = SimConfig(
                    method=method,
                    risk_per_trade=rpt,
                    max_concurrent=mc,
                    funding_on=funding_on,
                )
                res = simulate_compounding(trades, cfg)
                results.append(res)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--leverage", type=int, default=0, help="1/2/3, 0=all")
    ap.add_argument("--max-concurrent", type=int, default=0, help="override grid")
    ap.add_argument("--risk-per-trade", type=float, default=0.0, help="override grid")
    ap.add_argument("--method", default="", help="override method")
    ap.add_argument("--funding", action="store_true", help="include funding cost")
    ap.add_argument("--json-out", default="", help="write results JSON here")
    args = ap.parse_args(argv)

    leps = [args.leverage] if args.leverage in (1, 2, 3) else [1, 2, 3]
    all_summaries: list[dict[str, Any]] = []

    for lev in leps:
        path = LEVERAGE_FILES[lev]
        if not path.exists():
            print(f"!! missing {path}, skipping leverage {lev}")
            continue
        recs = load_records(path)
        trades = [normalize_trade(r) for r in recs]
        print(f"\n################## LEVERAGE {lev}x ##################")
        print(f"loaded {len(trades)} trades from {path.name}")
        print(f"naive sum (unrealizable): {sum(r['net_pnl'] for r in recs):.0f} "
              f"= {sum(r['net_pnl'] for r in recs)/10:.1f}% of $1000 (assumes ~10x capital)")

        # Kelly estimate
        kf = estimate_kelly(trades, 0.25)
        print(f"quarter-Kelly size fraction (on margin): {kf*100:.2f}%")

        # Override grid if requested
        if args.max_concurrent or args.risk_per_trade or args.method:
            cfg = SimConfig(
                method=args.method or "fixed_frac",
                risk_per_trade=args.risk_per_trade or 0.02,
                max_concurrent=args.max_concurrent or 3,
                funding_on=args.funding,
            )
            res = simulate_compounding(trades, cfg)
            s = result_summary(res)
            s["leverage_embedded"] = lev
            print(f"\n--- single run ---")
            for k, v in s.items():
                print(f"  {k}: {v}")
            all_summaries.append(s)
            continue

        results = run_grid(lev, trades, funding_on=args.funding)
        rows = [result_summary(r) | {"leverage_embedded": lev} for r in results]
        all_summaries.extend(rows)

        # Method comparison at canonical (mc=3, risk=2%)
        canonical = [r for r in rows if r["max_concurrent"] == 3 and abs(r["risk_per_trade"] - 0.02) < 1e-9]
        print_table(canonical, f"Method comparison @ lev={lev}x, conc=3, risk=2%")

        # Sensitivity: risk x concurrent for fixed_frac
        ff = [r for r in rows if r["method"] == "fixed_frac"]
        print_table(ff, f"Fixed-frac sensitivity @ lev={lev}x")

        # Best passing config
        passing = [r for r in rows if r["passes_gate"]]
        if passing:
            best = max(passing, key=lambda r: r["sharpe"])
            print(f"\n>>> BEST GATE-PASSING config @ {lev}x: "
                  f"{best['method']} conc={best['max_concurrent']} risk={best['risk_per_trade']} "
                  f"-> Ann={best['annualized_return_pct']}% Sharpe={best['sharpe']} "
                  f"MaxDD={best['max_drawdown_pct']}%")
        else:
            # best by Sharpe anyway
            best_any = max(rows, key=lambda r: r["sharpe"])
            print(f"\n>>> NO gate-passing config @ {lev}x. Best-by-Sharpe: "
                  f"{best_any['method']} conc={best_any['max_concurrent']} risk={best_any['risk_per_trade']} "
                  f"-> Ann={best_any['annualized_return_pct']}% Sharpe={best_any['sharpe']} "
                  f"MaxDD={best_any['max_drawdown_pct']}%")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(all_summaries, indent=2) + "\n")
        print(f"\nwrote {len(all_summaries)} summaries -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
