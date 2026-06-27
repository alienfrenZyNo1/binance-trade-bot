#!/usr/bin/env python3
"""Research-only regime-switching long/short backtester.

Uses the CACHED hourly kline data in data/kline_cache/ (no network). Logic:
  - EMA(fast) / EMA(slow) regime filter on price.
  - BEAR regime (price < ema_slow, or fast < slow): hold a SHORT.
  - BULL regime (price > ema_slow, or fast > slow): hold a LONG (or CASH).
  - ATR-based position sizing; leverage configurable.

Cost model:
  - taker fee 0.04% per side, slippage 0.03% per side -> round-trip ~0.14% on flips.
  - funding 0.010% per 8h (3x/day) applied to notional while position open; shorts
    are credited positive funding (longs pay shorts) at the modeled average.

Reports per-symbol and portfolio metrics: annualized return, Sharpe, MaxDD,
Calmar, win rate, trade count. Gate: Sharpe > 1.0, Ann > 50%, MaxDD < 20%.

Research only. No live trading.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / "data" / "kline_cache"
HOURS_PER_YEAR = 24 * 365

TAKER_FEE = 0.0004       # 0.04% per side
SLIPPAGE = 0.0003        # 0.03% per side
FUNDING_8H = 0.00010     # 0.010% per 8h interval, 3x/day
FUNDING_PER_H = FUNDING_8H * 3.0 / 24.0  # blended hourly funding cost


@dataclass
class RegimeConfig:
    ema_slow: int = 200          # regime filter EMA
    ema_fast: int = 50           # confirmation EMA
    bull_mode: str = "long"      # "long" or "cash"
    leverage: float = 1.0        # applied to equity at risk
    cap_per_symbol: float = 0.20  # fraction of portfolio equity per symbol
    trade_fee: float = TAKER_FEE + SLIPPAGE   # per side
    funding_per_h: float = FUNDING_PER_H
    funding_sign_short: float = -0.3  # shorts receive 30% of modeled funding on avg (net tailwind)
    use_slope_filter: bool = False    # add ema_slow slope confirmation (fewer flips)


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def load_symbol(symbol: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_1h_4320.json"
    raw = json.loads(path.read_text())
    df = pd.DataFrame(raw)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.sort_values("dt").reset_index(drop=True)
    return df


def backtest_symbol(df: pd.DataFrame, cfg: RegimeConfig) -> dict:
    """Per-symbol bar-by-bar long/short backtest on 1h bars.

    Position is +1 (long), -1 (short), or 0 (cash). Rebalanced at each bar close
    on next-bar open with slippage+fee on flips. Funding charged hourly on notional.
    Returns metrics + a normalized 'strategy_ret' per-bar return series (key for
    correct portfolio combination). Equity curve indexed to 1.0 start.
    """
    close = df["close"].astype(float).to_numpy()
    n = len(close)
    if n < cfg.ema_slow + 5:
        return {"error": "insufficient data", "n": n}

    ema_slow = ema(df["close"], cfg.ema_slow).to_numpy()
    ema_fast = ema(df["close"], cfg.ema_fast).to_numpy()

    # Regime: 1 = bull, -1 = bear. Use ONLY the EMA cross (clean, low-churn).
    # The raw `close < ema` condition is far too noisy (constant whipsaw) and is
    # deliberately NOT used here. Optional confirmation: ema_slow slope.
    if cfg.use_slope_filter:
        slope = np.gradient(ema_slow)
        regime = np.where(
            (ema_fast < ema_slow) | (slope < 0), -1, 1
        )
    else:
        regime = np.where(ema_fast < ema_slow, -1, 1)

    # Target position from regime
    bull_pos = 1.0 if cfg.bull_mode == "long" else 0.0
    target = np.where(regime == 1, bull_pos, -1.0)

    # Walk forward. equity starts at 1.0 (normalized). Position in units of equity.
    equity = 1.0
    pos = 0.0          # current position direction in [-1,1]
    equity_curve = np.empty(n)
    strat_ret = np.empty(n)   # strategy return realized AT this bar (for portfolio)
    trades = 0
    wins = 0
    gross_pnl = 0.0
    entry_equity_for_trade = 1.0

    for i in range(n):
        # funding on currently held position over this hour (charged on notional = |pos|*equity*lev)
        notional = abs(pos) * equity * cfg.leverage
        # longs pay funding; shorts receive a fraction (net funding tailwind for shorts)
        fund_rate_h = cfg.funding_per_h
        if pos > 0:
            funding_cost = notional * fund_rate_h
        elif pos < 0:
            funding_cost = notional * fund_rate_h * cfg.funding_sign_short  # negative => credit
        else:
            funding_cost = 0.0

        # price move this bar (close[i]/close[i-1] - 1); apply at bar open of i using prev close
        if i > 0:
            ret = close[i] / close[i - 1] - 1.0
        else:
            ret = 0.0
        price_pnl = pos * equity * cfg.leverage * ret

        # flip cost if target changes
        new_pos = target[i] if i < n else pos
        if new_pos != pos:
            # turnover = |new_pos - pos| * equity * leverage
            turnover = abs(new_pos - pos) * equity * cfg.leverage
            flip_cost = turnover * cfg.trade_fee
            # count a trade if we actually change direction
            if pos != 0 or new_pos != 0:
                trades += 1
                # settle the just-closed leg's win/loss vs its entry equity
                if equity > entry_equity_for_trade:
                    wins += 1
                entry_equity_for_trade = equity
            pos = new_pos
        else:
            flip_cost = 0.0

        prev_equity = equity
        equity = equity + price_pnl - funding_cost - flip_cost
        if equity <= 0:  # liquidation guard
            equity = 0.0
            pos = 0.0
        equity_curve[i] = equity
        strat_ret[i] = equity / prev_equity - 1.0 if prev_equity > 0 else 0.0

    df_out = df.copy()
    df_out["equity"] = equity_curve
    metrics = _metrics(df_out, cfg, trades, wins)
    metrics["equity_curve"] = equity_curve
    metrics["strategy_ret"] = strat_ret
    return metrics


def _metrics(df: pd.DataFrame, cfg: RegimeConfig, trades: int, wins: int) -> dict:
    eq = df["equity"].astype(float).to_numpy()
    n = len(eq)
    total_ret = eq[-1] / 1.0 - 1.0
    hours = n
    years = hours / HOURS_PER_YEAR
    ann = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 and (1.0 + total_ret) > 0 else -1.0

    # hourly returns of equity curve
    rets = np.diff(eq) / np.where(eq[:-1] > 0, eq[:-1], np.nan)
    rets = rets[~np.isnan(rets)]
    if len(rets) > 2:
        mu = rets.mean()
        sd = rets.std(ddof=1)
        sharpe = (mu / sd) * math.sqrt(HOURS_PER_YEAR) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    # max drawdown
    running = np.maximum.accumulate(eq)
    dd = (eq - running) / np.where(running > 0, running, np.nan)
    max_dd = float(np.nanmin(dd)) * 100.0  # negative percent
    max_dd_abs = abs(max_dd)
    calmar = ann / max_dd_abs * 100.0 if max_dd_abs > 0 else float("inf")

    win_rate = wins / trades * 100.0 if trades > 0 else 0.0

    ema_fast_arr = ema(df["close"], cfg.ema_fast).to_numpy()
    ema_slow_arr = ema(df["close"], cfg.ema_slow).to_numpy()
    bear_hours = int((ema_fast_arr < ema_slow_arr).sum())
    return {
        "total_return_pct": total_ret * 100.0,
        "annualized_return_pct": ann * 100.0,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "max_drawdown_abs_pct": max_dd_abs,
        "calmar": calmar,
        "trade_count": trades,
        "win_rate_pct": win_rate,
        "bars": n,
        "years": years,
        "bear_hours_pct": bear_hours / n * 100.0,
        "leverage": cfg.leverage,
        "final_equity": eq[-1],
    }


def portfolio_backtest(symbols: list[str], cfg: RegimeConfig) -> tuple[dict, pd.DataFrame]:
    """Run each symbol with EQUAL-WEIGHT REBALANCED allocation, combine into a
    portfolio curve. Combines per-bar strategy returns (not equity levels), which
    is the correct way to capture diversification: each bar the portfolio earns
    the average of the per-symbol strategy returns that bar.
    """
    curves = []
    per_symbol = {}
    total_trades = 0
    for sym in symbols:
        df = load_symbol(sym)
        res = backtest_symbol(df, cfg)
        if "error" in res:
            per_symbol[sym] = res
            continue
        eq = res.pop("equity_curve")
        sret = res.pop("strategy_ret")
        total_trades += res.get("trade_count", 0)
        per_symbol[sym] = res
        curves.append((sym, df["dt"].to_numpy(), sret))

    if not curves:
        return per_symbol, pd.DataFrame()
    # equal-weight rebalanced: portfolio return each bar = mean of symbol returns
    idx_dt = curves[0][1]
    mat = np.vstack([np.full(len(idx_dt), np.nan) for _ in curves])
    for r, (sym, dt, sret) in enumerate(curves):
        L = min(len(sret), len(idx_dt))
        mat[r, :L] = sret[:L]
    port_ret = np.nanmean(mat, axis=0)
    port_ret = np.nan_to_num(port_ret, nan=0.0)
    # compound to equity curve starting at 1.0
    port_eq = np.cumprod(1.0 + port_ret)
    port_df = pd.DataFrame({"dt": idx_dt, "equity": port_eq, "ret": port_ret})
    port_df.attrs["total_trades"] = total_trades
    return per_symbol, port_df


def portfolio_metrics(port_df: pd.DataFrame, cfg: RegimeConfig) -> dict:
    if port_df.empty:
        return {}
    eq = port_df["equity"].astype(float).to_numpy()
    n = len(eq)
    total_ret = eq[-1] / 1.0 - 1.0
    years = n / HOURS_PER_YEAR
    ann = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 and (1.0 + total_ret) > 0 else -1.0
    rets = np.diff(eq) / np.where(eq[:-1] > 0, eq[:-1], np.nan)
    rets = rets[~np.isnan(rets)]
    mu, sd = (rets.mean(), rets.std(ddof=1)) if len(rets) > 2 else (0.0, 0.0)
    sharpe = (mu / sd) * math.sqrt(HOURS_PER_YEAR) if sd > 0 else 0.0
    running = np.maximum.accumulate(eq)
    dd = (eq - running) / np.where(running > 0, running, np.nan)
    max_dd_abs = abs(float(np.nanmin(dd)) * 100.0)
    calmar = ann / max_dd_abs * 100.0 if max_dd_abs > 0 else float("inf")
    return {
        "total_return_pct": total_ret * 100.0,
        "annualized_return_pct": ann * 100.0,
        "sharpe": sharpe,
        "max_drawdown_pct": float(np.nanmin(dd) * 100.0),
        "max_drawdown_abs_pct": max_dd_abs,
        "calmar": calmar,
        "trade_count": int(port_df.attrs.get("total_trades", 0)),
        "win_rate_pct": 0.0,
        "bars": n,
        "years": years,
        "leverage": cfg.leverage,
        "final_equity": eq[-1],
    }


def passes_gate(m: dict, *, min_sharpe=1.0, min_ann=50.0, max_dd=20.0) -> bool:
    return (
        m.get("sharpe", 0) > min_sharpe
        and m.get("annualized_return_pct", -999) > min_ann
        and m.get("max_drawdown_abs_pct", 999) < max_dd
    )


def run_all(symbols, leverages=(1.0, 2.0, 3.0), bull_modes=("long", "cash"),
            ema_pairs=((50, 200), (20, 100), (30, 150))):
    results = []
    for lev in leverages:
        for bmode in bull_modes:
            for fast, slow in ema_pairs:
                cfg = RegimeConfig(ema_fast=fast, ema_slow=slow, bull_mode=bmode, leverage=lev)
                per_sym, port_df = portfolio_backtest(symbols, cfg)
                pm = portfolio_metrics(port_df, cfg)
                passed = passes_gate(pm)
                results.append({
                    "config": {
                        "leverage": lev, "bull_mode": bmode,
                        "ema_fast": fast, "ema_slow": slow,
                    },
                    "portfolio": pm,
                    "per_symbol": {k: {kk: vv for kk, vv in v.items() if kk != "equity"}
                                   for k, v in per_sym.items() if "error" not in v},
                    "passes_gate": passed,
                })
    return results


def fmt(m: dict) -> str:
    return (
        f"Ann {m.get('annualized_return_pct', 0):+7.1f}%  "
        f"Sharpe {m.get('sharpe', 0):5.2f}  "
        f"MaxDD {m.get('max_drawdown_abs_pct', 0):5.1f}%  "
        f"Calmar {m.get('calmar', 0):6.2f}  "
        f"Trades {m.get('trade_count', 0):4d}  "
        f"WR {m.get('win_rate_pct', 0):4.1f}%"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="BTCUSDC,ETHUSDC,SOLUSDC,XRPUSDC,LINKUSDC")
    ap.add_argument("--output", default="docs/research/regime-switch-results.json")
    ap.add_argument("--single", action="store_true", help="Run only the headline 2x long/short config")
    args = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"Regime-switching backtest on {symbols} using cached 1h data\n")

    if args.single:
        cfg = RegimeConfig(leverage=2.0, bull_mode="long")
        per_sym, port_df = portfolio_backtest(symbols, cfg)
        pm = portfolio_metrics(port_df, cfg)
        print("=== PORTFOLIO (2x, long/short, EMA50/200) ===")
        print(fmt(pm), "| GATE:", "PASS" if passes_gate(pm) else "FAIL")
        print("\n=== PER SYMBOL ===")
        for s, m in per_sym.items():
            if "error" in m:
                continue
            print(f"  {s:<10} {fmt(m)}  bear%={m.get('bear_hours_pct',0):.0f}")
        return

    results = run_all(symbols)
    # print summary sorted by sharpe desc
    results.sort(key=lambda r: r["portfolio"].get("sharpe", -999), reverse=True)
    print(f"{'Lev':>4} {'Mode':<5} {'EMA':<8} | {'PORTFOLIO METRICS':<60} | Gate")
    print("-" * 100)
    passing = []
    for r in results:
        c = r["config"]
        m = r["portfolio"]
        ema_str = f"{c['ema_fast']}/{c['ema_slow']}"
        gate = "PASS" if r["passes_gate"] else ""
        print(f"{c['leverage']:>4} {c['bull_mode']:<5} {ema_str:<8} | {fmt(m)} | {gate}")
        if r["passes_gate"]:
            passing.append(r)

    print(f"\n>>> {len(passing)}/{len(results)} configs clear the gate (Sharpe>1.0, Ann>50%, MaxDD<20%)")
    if passing:
        best = passing[0]
        print("Best passing config:", best["config"])
        print("  ", fmt(best["portfolio"]))
        print("Per-symbol breakdown:")
        for s, m in best["per_symbol"].items():
            print(f"    {s:<10} {fmt(m)}  bear%={m.get('bear_hours_pct',0):.0f}")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(results, indent=2, default=str) + "\n")
    print(f"\nSaved full results to {args.output}")
    return results


if __name__ == "__main__":
    main()
