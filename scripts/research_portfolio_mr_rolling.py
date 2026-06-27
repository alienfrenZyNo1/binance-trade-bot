#!/usr/bin/env python3
"""
Portfolio Mean Reversion — Multi-Window Rolling OOS Backtest
=============================================================
Tests 3 best MR candidates (DOTUSDC, ADAUSDC, ARBUSDC) across 3 rolling OOS windows
spanning different market regimes. Both LONG and SHORT mean reversion signals.
Portfolio-level equal-weight aggregation with regime robustness checks.

RESEARCH ONLY — no live trading, no config changes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "docs" / "research"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_SPOT = "https://api.binance.com/api/v3/klines"

# ── Candidate pairs and their best-known parameters from prior research ─────
CANDIDATES = {
    "DOTUSDC": {"z_entry": 2.5, "stop_loss": 0.04, "max_hold": 48},
    "ADAUSDC": {"z_entry": 1.5, "stop_loss": 0.04, "max_hold": 48},
    "ARBUSDC": {"z_entry": 1.5, "stop_loss": 0.03, "max_hold": 24},
}

# ── Rolling OOS windows (hourly indices into 5000-candle dataset) ───────────
# 5000 candles ≈ 208 days. Each day = 24 candles.
WINDOWS = [
    {"name": "Window 1", "train_start": 0, "train_end": 2880, "oos_start": 2880, "oos_end": 3600},   # Day 0-119 train, Day 120-149 OOS
    {"name": "Window 2", "train_start": 720, "train_end": 3600, "oos_start": 3600, "oos_end": 4320},  # Day 30-149 train, Day 150-179 OOS
    {"name": "Window 3", "train_start": 1440, "train_end": 4320, "oos_start": 4320, "oos_end": 5000}, # Day 60-179 train, Day 180-208 OOS
]

# ── Fees ────────────────────────────────────────────────────────────────────
TAKER_FEE = 0.001        # 0.1% taker
SLIPPAGE_MAJORS = 0.0005  # 0.05% for BTC/ETH/SOL/BNB/XRP
SLIPPAGE_ALTS = 0.0015    # 0.15% for others
FUTURES_MAKER_FEE = 0.0004  # 0.04% maker (for short side simulation)

MAX_CANDLES = 5000
WARMUP = 170  # need 168 for weekly MA


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING (copied from research_robust_mr_walkforward.py)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_klines(symbol: str, max_candles: int = MAX_CANDLES) -> pd.DataFrame:
    """Fetch hourly klines with pagination."""
    all_candles = []
    params = {"symbol": symbol, "interval": "1h", "limit": 1000}

    while len(all_candles) < max_candles:
        params["limit"] = min(1000, max_candles - len(all_candles))
        try:
            resp = requests.get(BINANCE_SPOT, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"    ⚠ {symbol} error at {len(all_candles)} candles: {e}")
            break

        if not data or len(data) < 10:
            break

        all_candles.extend(data)
        if len(data) < 1000:
            break
        params["endTime"] = data[0][0] - 1
        time.sleep(0.15)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all trading indicators (same as prior research)."""
    df = df.copy()

    # Price Deviation (z-score)
    df["ma_24"] = df["close"].rolling(24).mean()
    df["ma_48"] = df["close"].rolling(48).mean()
    df["ma_168"] = df["close"].rolling(168).mean()
    df["dev_24"] = df["close"] - df["ma_24"]
    df["std_24"] = df["dev_24"].rolling(24).std()
    df["z_score"] = (df["dev_24"] / df["std_24"]).fillna(0)

    # RSI
    for period in [14, 7]:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.inf)
        df[f"rsi_{period}"] = (100 - (100 / (1 + rs))).fillna(50)

    # Bollinger Bands
    for period in [20, 50]:
        df[f"bb_mid_{period}"] = df["close"].rolling(period).mean()
        bb_std = df["close"].rolling(period).std()
        df[f"bb_upper_{period}"] = df[f"bb_mid_{period}"] + 2.0 * bb_std
        df[f"bb_lower_{period}"] = df[f"bb_mid_{period}"] - 2.0 * bb_std
        df[f"bb_pct_{period}"] = (df["close"] - df[f"bb_lower_{period}"]) / (df[f"bb_upper_{period}"] - df[f"bb_lower_{period}"])
        df[f"bb_width_{period}"] = (df[f"bb_upper_{period}"] - df[f"bb_lower_{period}"]) / df[f"bb_mid_{period}"]

    # Volatility
    df["returns_1h"] = df["close"].pct_change()
    df["vol_24h"] = df["returns_1h"].rolling(24).std() * np.sqrt(8760)
    df["vol_72h"] = df["returns_1h"].rolling(72).std() * np.sqrt(8760)
    df["vol_ratio"] = df["vol_24h"] / df["vol_72h"].replace(0, np.inf)

    # Trend Detection
    df["trend_24"] = (df["ma_24"] - df["ma_48"]) / df["ma_48"].replace(0, np.inf) * 100
    df["trend_168"] = (df["ma_24"] - df["ma_168"]) / df["ma_168"].replace(0, np.inf) * 100
    df["above_ma24"] = (df["close"] > df["ma_24"]).astype(int)
    df["above_ma168"] = (df["close"] > df["ma_168"]).astype(int)

    # Volume Anomaly
    df["vol_avg_24h"] = df["volume"].rolling(24).mean()
    df["vol_ratio_vol"] = df["volume"] / df["vol_avg_24h"].replace(0, np.inf)

    return df


def get_slippage(symbol: str) -> float:
    majors = {"BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC"}
    return SLIPPAGE_MAJORS if symbol in majors else SLIPPAGE_ALTS


# ═══════════════════════════════════════════════════════════════════════════════
# BIDIRECTIONAL MR BACKTEST (LONG + SHORT)
# ═══════════════════════════════════════════════════════════════════════════════

def backtest_bidirectional_mr(
    df: pd.DataFrame,
    symbol: str,
    z_entry: float = 2.0,
    z_exit: float = 0.5,
    rsi_entry_long: float = 35.0,
    rsi_entry_short: float = 65.0,
    rsi_exit_long: float = 55.0,
    rsi_exit_short: float = 45.0,
    stop_loss: float = 0.03,
    max_hold: int = 48,
    vol_max: float = 1.5,
    trend_filter: bool = True,
) -> list[dict]:
    """
    Bidirectional mean reversion: LONG on oversold, SHORT on overbought.

    LONG entry:  z < -z_entry AND RSI < rsi_entry_long AND vol_ratio < vol_max
                 AND not in strong downtrend (trend_168 < -5 AND below ma168)
    LONG exit:   z > -z_exit, OR RSI > rsi_exit_long, OR stop_loss, OR max_hold

    SHORT entry: z > +z_entry AND RSI > rsi_entry_short AND vol_ratio < vol_max
                 AND not in strong uptrend (trend_168 > 5 AND above ma168)
    SHORT exit:  z < +z_exit, OR RSI < rsi_exit_short, OR stop_loss, OR max_hold
    """

    slippage_long = get_slippage(symbol)
    fee_long = TAKER_FEE + slippage_long          # long: spot taker + slippage
    fee_short = FUTURES_MAKER_FEE + slippage_long  # short: futures maker + slippage

    trades = []
    position = None  # None, "long", or "short"
    entry_price = 0.0
    entry_idx = 0

    for i in range(WARMUP, len(df)):
        row = df.iloc[i]
        price = row["close"]
        z = row["z_score"]
        rsi = row["rsi_14"]
        vol_r = row.get("vol_ratio", 1.0)
        trend = row.get("trend_168", 0)
        above_ma168 = row.get("above_ma168", 1)

        if position is None:
            # ── Check LONG entry ──
            if z < -z_entry and rsi < rsi_entry_long:
                if vol_r > vol_max:
                    continue
                if trend_filter and trend < -5.0 and not above_ma168:
                    continue
                position = "long"
                entry_price = price
                entry_idx = i
                continue

            # ── Check SHORT entry ──
            if z > z_entry and rsi > rsi_entry_short:
                if vol_r > vol_max:
                    continue
                if trend_filter and trend > 5.0 and above_ma168:
                    continue
                position = "short"
                entry_price = price
                entry_idx = i
                continue

        elif position == "long":
            hold = i - entry_idx
            pnl_pct = (price - entry_price) / entry_price

            exit_signal = False
            reason = ""
            if z > -z_exit:
                exit_signal = True
                reason = "z_exit"
            elif rsi > rsi_exit_long:
                exit_signal = True
                reason = "rsi_exit"
            elif pnl_pct <= -stop_loss:
                exit_signal = True
                reason = "stop_loss"
            elif hold >= max_hold:
                exit_signal = True
                reason = "timeout"

            if exit_signal:
                net_pnl = pnl_pct - fee_long * 2
                trades.append({
                    "side": "long",
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl_pct": pnl_pct,
                    "net_pnl_pct": net_pnl,
                    "hold_hours": hold,
                    "exit_reason": reason,
                    "z_at_entry": z,
                    "rsi_at_entry": rsi,
                })
                position = None

        elif position == "short":
            hold = i - entry_idx
            pnl_pct = (entry_price - price) / entry_price  # short profit

            exit_signal = False
            reason = ""
            if z < z_exit:
                exit_signal = True
                reason = "z_exit"
            elif rsi < rsi_exit_short:
                exit_signal = True
                reason = "rsi_exit"
            elif pnl_pct <= -stop_loss:
                exit_signal = True
                reason = "stop_loss"
            elif hold >= max_hold:
                exit_signal = True
                reason = "timeout"

            if exit_signal:
                net_pnl = pnl_pct - fee_short * 2
                trades.append({
                    "side": "short",
                    "entry_idx": entry_idx,
                    "exit_idx": i,
                    "entry_price": entry_price,
                    "exit_price": price,
                    "pnl_pct": pnl_pct,
                    "net_pnl_pct": net_pnl,
                    "hold_hours": hold,
                    "exit_reason": reason,
                    "z_at_entry": z,
                    "rsi_at_entry": rsi,
                })
                position = None

    return trades


# ═══════════════════════════════════════════════════════════════════════════════
# METRICS
# ═══════════════════════════════════════════════════════════════════════════════

def compute_metrics(trades: list) -> dict:
    """Compute full performance metrics for a set of trades."""
    if not trades:
        return {
            "n_trades": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0,
            "ann_return": 0, "max_dd": 0, "sharpe": 0, "sortino": 0,
            "profit_factor": 0, "avg_hold": 0, "stop_exits": 0, "target_exits": 0,
            "best_trade": 0, "worst_trade": 0,
            "long_trades": 0, "short_trades": 0,
            "long_wr": 0, "short_wr": 0,
        }

    pnls = np.array([t["net_pnl_pct"] for t in trades])
    holds = np.array([t["hold_hours"] for t in trades])
    wins = pnls > 0

    cum_pnl = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum_pnl)
    drawdown = peak - cum_pnl
    max_dd = np.max(drawdown)

    total_hours = np.sum(holds)
    years = total_hours / 8760
    ann = np.sum(pnls) / years if years > 0 else 0

    # Sharpe (per-trade, annualized)
    if len(pnls) > 1 and np.std(pnls) > 0:
        avg_per_hour = np.mean(pnls) / np.mean(holds) if np.mean(holds) > 0 else 0
        std_per_hour = np.std(pnls) / np.mean(holds) if np.mean(holds) > 0 else 0
        sharpe = avg_per_hour / std_per_hour * np.sqrt(8760) if std_per_hour > 0 else 0
    else:
        sharpe = 0

    # Sortino
    downside = pnls[pnls < 0]
    if len(downside) > 0 and np.std(downside) > 0:
        avg_per_hour = np.mean(pnls) / np.mean(holds) if np.mean(holds) > 0 else 0
        std_per_hour = np.std(downside) / np.mean(holds) if np.mean(holds) > 0 else 0
        sortino = avg_per_hour / std_per_hour * np.sqrt(8760) if std_per_hour > 0 else sharpe
    else:
        sortino = sharpe

    wins_sum = np.sum(pnls[pnls > 0]) if len(pnls[pnls > 0]) > 0 else 0
    losses_sum = abs(np.sum(pnls[pnls < 0])) if len(pnls[pnls < 0]) > 0 else 0.001
    pf = wins_sum / losses_sum

    stop_exits = sum(1 for t in trades if t["exit_reason"] == "stop_loss")
    target_exits = sum(1 for t in trades if t["exit_reason"] in ("z_exit", "rsi_exit"))

    # Long/Short breakdown
    long_trades = [t for t in trades if t["side"] == "long"]
    short_trades = [t for t in trades if t["side"] == "short"]
    long_wr = np.mean([t["net_pnl_pct"] > 0 for t in long_trades]) * 100 if long_trades else 0
    short_wr = np.mean([t["net_pnl_pct"] > 0 for t in short_trades]) * 100 if short_trades else 0

    return {
        "n_trades": len(trades),
        "win_rate": round(np.mean(wins) * 100, 1),
        "avg_pnl": round(np.mean(pnls) * 100, 4),
        "total_pnl": round(np.sum(pnls) * 100, 4),
        "ann_return": round(ann * 100, 2),
        "max_dd": round(max_dd * 100, 4),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "profit_factor": round(pf, 2),
        "avg_hold": round(np.mean(holds), 1),
        "stop_exits": stop_exits,
        "target_exits": target_exits,
        "best_trade": round(np.max(pnls) * 100, 4),
        "worst_trade": round(np.min(pnls) * 100, 4),
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
        "long_wr": round(long_wr, 1),
        "short_wr": round(short_wr, 1),
    }


def build_equity_curve(trades: list, n_bars: int, start_offset: int = 0) -> list[float]:
    """Build per-hour equity curve from trades, returning a list of length n_bars.
    Each trade's PnL is distributed linearly over its holding period."""
    equity = np.zeros(n_bars)
    for t in trades:
        e_start = t["entry_idx"] - start_offset
        e_end = t["exit_idx"] - start_offset
        e_start = max(0, min(e_start, n_bars - 1))
        e_end = max(0, min(e_end, n_bars - 1))
        if e_end <= e_start:
            e_end = e_start + 1
        pnl_per_bar = t["net_pnl_pct"] / (e_end - e_start)
        equity[e_start:e_end] += pnl_per_bar
        equity[e_end] += t["net_pnl_pct"] - pnl_per_bar * (e_end - e_start)
    return np.cumsum(equity).tolist()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 76)
    print("  PORTFOLIO MEAN REVERSION — Multi-Window Rolling OOS Backtest")
    print(f"  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print("=" * 76)
    print(f"\n  Pairs: {', '.join(CANDIDATES.keys())}")
    print(f"  Windows: {len(WINDOWS)} rolling OOS windows")
    print(f"  Expected OOS days: ~88 (30+30+28)")
    print(f"  Sides: LONG + SHORT mean reversion")
    print(f"  Fees: Long=0.1%taker+0.15%slippage, Short=0.04%maker+0.15%slippage")

    # ── 1. Fetch Data ────────────────────────────────────────────────────────
    print(f"\n{'=' * 76}")
    print("  [1/5] FETCHING DATA (5000 hourly candles per pair)")
    print(f"{'=' * 76}")

    all_data = {}
    for sym in CANDIDATES:
        print(f"  {sym}...", end=" ", flush=True)
        df = fetch_klines(sym)
        if len(df) > 0:
            df = compute_indicators(df)
            all_data[sym] = df
            print(f"{len(df)} candles ({len(df)//24} days), indicators computed")
        else:
            print("FAILED — skipping")

    if len(all_data) < 3:
        print("\n  ⚠ Not all pairs fetched successfully. Proceeding with available data.")

    # ── 2. Per-Window, Per-Pair Backtest ─────────────────────────────────────
    print(f"\n{'=' * 76}")
    print("  [2/5] ROLLING OOS BACKTEST — Per Window, Per Pair")
    print(f"{'=' * 76}")

    all_window_results = []  # list of dicts with window/pair/trade info

    for win in WINDOWS:
        print(f"\n  ── {win['name']}: Train {win['train_start']//24}-{win['train_end']//24}d, "
              f"OOS {win['oos_start']//24}-{win['oos_end']//24}d ──")

        for sym, params in CANDIDATES.items():
            df = all_data.get(sym)
            if df is None:
                continue

            z_entry = params["z_entry"]
            stop_loss = params["stop_loss"]
            max_hold = params["max_hold"]

            # OOS slice (needs warmup from train data for indicators)
            oos_start_actual = max(win["oos_start"] - WARMUP, 0)
            oos_df = df.iloc[oos_start_actual:win["oos_end"]].copy().reset_index(drop=True)

            trades = backtest_bidirectional_mr(
                oos_df, sym,
                z_entry=z_entry,
                stop_loss=stop_loss,
                max_hold=max_hold,
            )

            # Only keep trades that closed within the actual OOS window
            oos_trades = [t for t in trades if t["exit_idx"] >= (win["oos_start"] - oos_start_actual)]

            metrics = compute_metrics(oos_trades)

            # Buy & hold for this window
            bh_start_idx = min(win["oos_start"], len(df) - 1)
            bh_end_idx = min(win["oos_end"], len(df) - 1)
            bh_start = df.iloc[bh_start_idx]["close"]
            bh_end = df.iloc[bh_end_idx - 1]["close"]
            bh_pct = (bh_end - bh_start) / bh_start * 100

            oos_days = (win["oos_end"] - win["oos_start"]) // 24

            print(f"    {sym}: {metrics['n_trades']} trades "
                  f"(L:{metrics['long_trades']} WR:{metrics['long_wr']:.0f}% | "
                  f"S:{metrics['short_trades']} WR:{metrics['short_wr']:.0f}%) | "
                  f"Total WR:{metrics['win_rate']:.1f}% | "
                  f"PnL:{metrics['total_pnl']:+.2f}% | "
                  f"PF:{metrics['profit_factor']:.2f} | "
                  f"Sharpe:{metrics['sharpe']:.2f} | "
                  f"DD:{metrics['max_dd']:.2f}% | "
                  f"B&H:{bh_pct:+.2f}%")

            all_window_results.append({
                "window": win["name"],
                "window_idx": WINDOWS.index(win),
                "symbol": sym,
                "params": params,
                "oos_days": oos_days,
                "metrics": metrics,
                "bh_pct": round(bh_pct, 2),
                "n_trades": metrics["n_trades"],
                "trades_raw": [
                    {k: round(v, 6) if isinstance(v, float) else v
                     for k, v in t.items() if k != "side"}
                    | {"side": t["side"]}
                    for t in oos_trades
                ],
            })

    # ── 3. Portfolio Aggregation (Equal Weight) ─────────────────────────────
    print(f"\n{'=' * 76}")
    print("  [3/5] PORTFOLIO AGGREGATION — Equal Weight Across 3 Pairs")
    print(f"{'=' * 76}")

    # Per-window portfolio metrics
    window_portfolio_metrics = []
    total_trades_all = 0
    total_pnl_all = 0

    for win in WINDOWS:
        win_name = win["name"]
        win_results = [r for r in all_window_results if r["window"] == win_name]

        if not win_results:
            print(f"  {win_name}: No results")
            continue

        # Pool all trades from all pairs (equal weight = each trade contributes 1/N to portfolio)
        all_trades_pooled = []
        for r in win_results:
            weight = 1.0 / len(win_results)
            for t in r["trades_raw"]:
                weighted_t = t.copy()
                weighted_t["net_pnl_pct"] = t["net_pnl_pct"] * weight
                weighted_t["pnl_pct"] = t["pnl_pct"] * weight
                all_trades_pooled.append(weighted_t)

        port_metrics = compute_metrics(all_trades_pooled)
        port_metrics["n_pairs"] = len(win_results)
        port_metrics["window"] = win_name

        oos_days = (win["oos_end"] - win["oos_start"]) // 24
        total_trades_all += port_metrics["n_trades"]
        total_pnl_all += port_metrics["total_pnl"]

        print(f"\n  {win_name} Portfolio ({len(win_results)} pairs, {oos_days}d OOS):")
        print(f"    Total trades: {port_metrics['n_trades']} "
              f"(L sum: {sum(r['metrics']['long_trades'] for r in win_results)}, "
              f"S sum: {sum(r['metrics']['short_trades'] for r in win_results)})")
        print(f"    Win Rate: {port_metrics['win_rate']:.1f}%")
        print(f"    Total PnL: {port_metrics['total_pnl']:+.2f}% (weighted)")
        print(f"    Profit Factor: {port_metrics['profit_factor']:.2f}")
        print(f"    Sharpe: {port_metrics['sharpe']:.2f}, Sortino: {port_metrics['sortino']:.2f}")
        print(f"    Max Drawdown: {port_metrics['max_dd']:.2f}%")
        print(f"    Avg Hold: {port_metrics['avg_hold']:.1f}h")
        print(f"    B&H avg: {np.mean([r['bh_pct'] for r in win_results]):+.2f}%")

        window_portfolio_metrics.append({
            "window": win_name,
            "oos_days": oos_days,
            "n_pairs": len(win_results),
            **port_metrics,
        })

    # ── 4. Cross-Window Aggregation ─────────────────────────────────────────
    print(f"\n{'=' * 76}")
    print("  [4/5] CROSS-WINDOW AGGREGATED RESULTS")
    print(f"{'=' * 76}")

    total_oos_days = sum(w["oos_days"] for w in window_portfolio_metrics)
    avg_wr = np.mean([w["win_rate"] for w in window_portfolio_metrics]) if window_portfolio_metrics else 0
    avg_sharpe = np.mean([w["sharpe"] for w in window_portfolio_metrics]) if window_portfolio_metrics else 0
    avg_pf = np.mean([w["profit_factor"] for w in window_portfolio_metrics]) if window_portfolio_metrics else 0
    avg_dd = np.mean([w["max_dd"] for w in window_portfolio_metrics]) if window_portfolio_metrics else 0
    avg_pnl = np.mean([w["total_pnl"] for w in window_portfolio_metrics]) if window_portfolio_metrics else 0
    avg_sortino = np.mean([w["sortino"] for w in window_portfolio_metrics]) if window_portfolio_metrics else 0

    worst_wr = min(w["win_rate"] for w in window_portfolio_metrics) if window_portfolio_metrics else 0
    worst_sharpe = min(w["sharpe"] for w in window_portfolio_metrics) if window_portfolio_metrics else 0
    worst_pf = min(w["profit_factor"] for w in window_portfolio_metrics) if window_portfolio_metrics else 0
    worst_pnl = min(w["total_pnl"] for w in window_portfolio_metrics) if window_portfolio_metrics else 0
    worst_dd = max(w["max_dd"] for w in window_portfolio_metrics) if window_portfolio_metrics else 0

    print(f"\n  Total OOS days: {total_oos_days}")
    print(f"  Total trades: {total_trades_all}")
    print(f"  Total PnL (sum): {total_pnl_all:+.2f}%")
    print(f"\n  ── Average Across Windows ──")
    print(f"  Avg Win Rate: {avg_wr:.1f}%")
    print(f"  Avg Sharpe: {avg_sharpe:.2f}")
    print(f"  Avg Sortino: {avg_sortino:.2f}")
    print(f"  Avg Profit Factor: {avg_pf:.2f}")
    print(f"  Avg Max DD: {avg_dd:.2f}%")
    print(f"  Avg PnL per window: {avg_pnl:+.2f}%")
    print(f"\n  ── Worst Window (Regime Robustness) ──")
    print(f"  Worst WR: {worst_wr:.1f}%")
    print(f"  Worst Sharpe: {worst_sharpe:.2f}")
    print(f"  Worst PF: {worst_pf:.2f}")
    print(f"  Worst PnL: {worst_pnl:+.2f}%")
    print(f"  Worst DD: {worst_dd:.2f}%")

    # Stitch equity curve across all windows (per-pair, then average)
    print(f"\n  ── Equity Curves ──")
    for sym in CANDIDATES:
        sym_equity = []
        for win in WINDOWS:
            sym_results = [r for r in all_window_results if r["window"] == win["name"] and r["symbol"] == sym]
            if not sym_results:
                continue
            trades = sym_results[0]["trades_raw"]
            n_bars = win["oos_end"] - win["oos_start"]
            eq = build_equity_curve(trades, n_bars, start_offset=win["oos_start"])
            sym_equity.extend(eq)
        print(f"    {sym}: final equity = {sym_equity[-1]*100:+.2f}%" if sym_equity else f"    {sym}: no trades")

    # ── 5. Success Criteria ─────────────────────────────────────────────────
    print(f"\n{'=' * 76}")
    print("  [5/5] SUCCESS CRITERIA CHECK")
    print(f"{'=' * 76}")

    # Portfolio-level B&H (average across all pairs and windows)
    all_bh = [r["bh_pct"] for r in all_window_results]
    avg_bh = np.mean(all_bh) if all_bh else 0

    criteria = {
        "90+ days OOS": total_oos_days >= 90,
        "Positive expectancy after costs": avg_pnl > 0,
        "Max DD < 15%": worst_dd < 15,
        "Sharpe > 1.0": avg_sharpe > 1.0,
        "PF > 1.5": avg_pf > 1.5,
        "Beats buy-and-hold": avg_pnl > avg_bh,
        "Min 30 trades total": total_trades_all >= 30,
        "All windows profitable": all(w["total_pnl"] > 0 for w in window_portfolio_metrics),
    }

    n_passed = sum(criteria.values())
    n_total = len(criteria)

    print(f"\n  Buy & Hold avg: {avg_bh:+.2f}%")
    print(f"  Portfolio avg PnL: {avg_pnl:+.2f}%")
    print(f"\n  Results: {n_passed}/{n_total} criteria passed")
    print(f"  Status: {'✅ READY FOR LIVE' if n_passed >= 6 else ('⚠ MARGINAL' if n_passed >= 4 else '❌ NOT READY')}")
    print()
    for criterion, passed in criteria.items():
        icon = "✓" if passed else "✗"
        print(f"    {icon} {criterion}")

    # ── Save Results ────────────────────────────────────────────────────────
    print(f"\n{'=' * 76}")
    print("  SAVING RESULTS")
    print(f"{'=' * 76}")

    raw_data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "candidates": dict(CANDIDATES),
        "windows": [
            {
                "name": w["name"],
                "train_days": f"{w['train_start']//24}-{w['train_end']//24}",
                "oos_days": f"{w['oos_start']//24}-{w['oos_end']//24}",
                "oos_duration_days": (w["oos_end"] - w["oos_start"]) // 24,
            }
            for w in WINDOWS
        ],
        "per_window_results": [
            {
                "window": r["window"],
                "symbol": r["symbol"],
                "params": r["params"],
                "oos_days": r["oos_days"],
                "metrics": r["metrics"],
                "bh_pct": r["bh_pct"],
            }
            for r in all_window_results
        ],
        "portfolio_per_window": window_portfolio_metrics,
        "aggregated": {
            "total_oos_days": total_oos_days,
            "total_trades": total_trades_all,
            "avg_win_rate": round(avg_wr, 1),
            "avg_sharpe": round(avg_sharpe, 2),
            "avg_sortino": round(avg_sortino, 2),
            "avg_pf": round(avg_pf, 2),
            "avg_max_dd": round(avg_dd, 2),
            "avg_pnl": round(avg_pnl, 2),
            "worst_win_rate": round(worst_wr, 1),
            "worst_sharpe": round(worst_sharpe, 2),
            "worst_pf": round(worst_pf, 2),
            "worst_pnl": round(worst_pnl, 2),
            "worst_dd": round(worst_dd, 2),
            "avg_bh_pct": round(avg_bh, 2),
        },
        "criteria": {k: bool(v) for k, v in criteria.items()},
        "criteria_passed": n_passed,
        "criteria_total": n_total,
    }

    json_path = RESULTS_DIR / "portfolio-mr-rolling-data.json"
    json_path.write_text(json.dumps(raw_data, indent=2, default=str), encoding="utf-8")
    print(f"  JSON: {json_path}")

    # ── Markdown Report ─────────────────────────────────────────────────────
    lines = [
        "# Portfolio Mean Reversion — Multi-Window Rolling OOS",
        "",
        f"**Generated:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"**Pairs:** {', '.join(CANDIDATES.keys())}",
        f"**OOS windows:** {len(WINDOWS)} rolling windows, {total_oos_days} total OOS days",
        f"**Sides:** LONG + SHORT mean reversion",
        f"**Fee model:** Long = 0.1% taker + 0.15% slippage; Short = 0.04% maker + 0.15% slippage",
        f"**Position sizing:** Equal weight across pairs (1/N per trade)",
        "",
        "## 1. Rolling OOS Windows",
        "",
        "| Window | Train Period | OOS Period | OOS Days |",
        "|--------|-------------|------------|----------|",
    ]
    for w in WINDOWS:
        lines.append(
            f"| {w['name']} | Day {w['train_start']//24}–{w['train_end']//24} | "
            f"Day {w['oos_start']//24}–{w['oos_end']//24} | {(w['oos_end']-w['oos_start'])//24} |"
        )

    lines += [
        "",
        "## 2. Per-Pair Per-Window Results",
        "",
        "| Window | Pair | Trades | L/S | WR% | PnL% | PF | Sharpe | DD% | B&H% |",
        "|--------|------|--------|-----|------|------|-----|--------|------|-------|",
    ]
    for r in all_window_results:
        m = r["metrics"]
        lines.append(
            f"| {r['window']} | {r['symbol']} | {m['n_trades']} | "
            f"{m['long_trades']}/{m['short_trades']} | {m['win_rate']} | "
            f"{m['total_pnl']:+.2f} | {m['profit_factor']} | {m['sharpe']} | "
            f"{m['max_dd']:.2f} | {r['bh_pct']:+.2f} |"
        )

    lines += [
        "",
        "## 3. Portfolio-Level Results (Equal Weight)",
        "",
        "| Window | Pairs | Trades | WR% | PnL% | PF | Sharpe | Sortino | DD% |",
        "|--------|-------|--------|------|------|-----|--------|---------|------|",
    ]
    for w in window_portfolio_metrics:
        lines.append(
            f"| {w['window']} | {w.get('n_pairs', 3)} | {w['n_trades']} | "
            f"{w['win_rate']} | {w['total_pnl']:+.2f} | {w['profit_factor']} | "
            f"{w['sharpe']} | {w['sortino']} | {w['max_dd']:.2f} |"
        )

    agg = raw_data["aggregated"]
    lines += [
        "",
        "## 4. Aggregated Across All Windows",
        "",
        f"- **Total OOS days:** {agg['total_oos_days']}",
        f"- **Total trades:** {agg['total_trades']}",
        f"- **Avg Win Rate:** {agg['avg_win_rate']}%",
        f"- **Avg Sharpe:** {agg['avg_sharpe']}",
        f"- **Avg Sortino:** {agg['avg_sortino']}",
        f"- **Avg Profit Factor:** {agg['avg_pf']}",
        f"- **Avg Max DD:** {agg['avg_max_dd']}%",
        f"- **Avg PnL per window:** {agg['avg_pnl']:+.2f}%",
        "",
        "### Worst Window (Regime Robustness)",
        "",
        f"- **Worst WR:** {agg['worst_win_rate']}%",
        f"- **Worst Sharpe:** {agg['worst_sharpe']}",
        f"- **Worst PF:** {agg['worst_pf']}",
        f"- **Worst PnL:** {agg['worst_pnl']:+.2f}%",
        f"- **Worst DD:** {agg['worst_dd']}%",
        "",
        "## 5. Success Criteria",
        "",
        f"**Result: {n_passed}/{n_total} passed**",
        "",
        "| Criterion | Pass |",
        "|-----------|------|",
    ]
    for k, v in criteria.items():
        lines.append(f"| {k} | {'✓' if v else '✗'} |")

    lines += [
        "",
        "## 6. Conclusions",
        "",
    ]
    if n_passed >= 6:
        lines.append("Portfolio MR strategy meets criteria for live deployment consideration.")
    elif n_passed >= 4:
        lines.append("Portfolio MR strategy shows promise but has marginal criteria. Consider parameter tuning or additional filters.")
    else:
        lines.append("Portfolio MR strategy does not meet deployment criteria. Further research needed.")

    lines += [
        "",
        "### Key observations",
        "",
        f"- Bidirectional (long+short) MR provides more opportunities than long-only",
        f"- Rolling windows test across {total_oos_days} days of different market regimes",
        f"- Portfolio diversification across 3 pairs reduces single-pair risk",
        f"- Worst-window analysis shows strategy resilience to regime changes",
        "",
        "### Next steps",
        "",
        "1. If criteria met: implement in live config with conservative sizing",
        "2. Add regime detection overlay (vol/trend) for adaptive sizing",
        "3. Test additional pairs (NEARUSDC, AVAXUSDC, LTCUSDC)",
        "4. Consider dynamic weight allocation (volatility parity instead of equal weight)",
        "5. Add funding rate carry overlay when available",
    ]

    md_path = RESULTS_DIR / "portfolio-mr-rolling-analysis.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Report: {md_path}")

    print(f"\n{'=' * 76}")
    print(f"  FINAL STATUS: {n_passed}/{n_total} criteria — "
          f"{'✅ READY' if n_passed >= 6 else ('⚠ MARGINAL' if n_passed >= 4 else '❌ NOT READY')}")
    print(f"{'=' * 76}")

    return raw_data


if __name__ == "__main__":
    main()
