#!/usr/bin/env python3
"""Research-only perpetual futures funding-rate arbitrage analysis.

Uses the PUBLIC Binance FAPI to fetch historical funding rates for major
USDC-M perpetual pairs and evaluates delta-neutral carry trade feasibility.

This script is intentionally research-only: it never places orders, reads
private endpoints, or modifies any live config.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BINANCE_FAPI = "https://fapi.binance.com"
FUNDING_RATE_URL = f"{BINANCE_FAPI}/fapi/v1/fundingRate"
SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC",
    "DOGEUSDC", "ADAUSDC", "AVAXUSDC", "LINKUSDC", "DOTUSDC",
]
LIMIT = 1000  # max records per request (one per 8h slot ≈ 333 days)
FUNDING_PERIODS_PER_DAY = 3  # Binance funds 3× per 8h
DAYS_PER_YEAR = 365
MAKER_FEE = 0.0002  # 0.02 % maker fee per side
ENTRY_EXIT_FEES_PCT = MAKER_FEE * 2  # open + close (0.04 %)
AMORTIZE_OVER_PERIODS = 30  # spread entry/exit fees over 30 funding periods (10 days)
FEES_PER_PERIOD_PCT = ENTRY_EXIT_FEES_PCT / AMORTIZE_OVER_PERIODS
CAPITAL = 500  # simulated capital ($)
REQUEST_TIMEOUT = 15
RATE_LIMIT_PAUSE = 0.35  # seconds between requests


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FundingRecord:
    symbol: str
    funding_rate: float  # e.g. 0.0001 = 0.01 %
    funding_time: datetime  # ms epoch → datetime


@dataclass(slots=True)
class PairStats:
    symbol: str
    n_periods: int
    mean_rate: float
    median_rate: float
    std_rate: float
    pct_positive: float
    pct_negative: float
    max_rate: float
    min_rate: float
    annualized_mean: float  # mean_rate × 3 × 365


@dataclass(slots=True)
class CarrySim:
    symbol: str
    n_periods: int
    total_pnl_pct: float  # cumulative PnL in % of capital
    total_pnl_dollars: float
    annualized_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float  # % of periods where funding > fees


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_funding_rates(symbol: str, limit: int = LIMIT) -> list[FundingRecord]:
    """Fetch historical funding rates from the public FAPI endpoint."""
    params = {"symbol": symbol, "limit": limit}
    try:
        resp = requests.get(
            FUNDING_RATE_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"  [ERROR] Failed to fetch {symbol}: {exc}", file=sys.stderr)
        return []

    records: list[FundingRecord] = []
    for item in data:
        records.append(
            FundingRecord(
                symbol=symbol,
                funding_rate=float(item["fundingRate"]),
                funding_time=datetime.fromtimestamp(int(item["fundingTime"]) / 1000, tz=timezone.utc),
            )
        )
    return records


def fetch_all_symbols(symbols: list[str]) -> dict[str, list[FundingRecord]]:
    """Fetch funding rates for every symbol with rate-limit pacing."""
    all_data: dict[str, list[FundingRecord]] = {}
    for sym in symbols:
        print(f"  Fetching {sym} …")
        all_data[sym] = fetch_funding_rates(sym)
        if sym != symbols[-1]:
            time.sleep(RATE_LIMIT_PAUSE)
    return all_data


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------
def compute_pair_stats(records: list[FundingRecord]) -> PairStats | None:
    if not records:
        return None
    rates = [r.funding_rate for r in records]
    n = len(rates)
    mean_r = statistics.mean(rates)
    median_r = statistics.median(rates)
    std_r = statistics.pstdev(rates) if n > 1 else 0.0
    pct_pos = sum(1 for r in rates if r > 0) / n * 100
    pct_neg = sum(1 for r in rates if r < 0) / n * 100
    return PairStats(
        symbol=records[0].symbol,
        n_periods=n,
        mean_rate=mean_r,
        median_rate=median_r,
        std_rate=std_r,
        pct_positive=pct_pos,
        pct_negative=pct_neg,
        max_rate=max(rates),
        min_rate=min(rates),
        annualized_mean=mean_r * FUNDING_PERIODS_PER_DAY * DAYS_PER_YEAR * 100,  # in %
    )


def simulate_carry(records: list[FundingRecord], capital: float) -> CarrySim | None:
    """Delta-neutral carry: short perp + hold spot when funding > 0.

    PnL per period = |funding_rate| received - fees_per_period.
    Assumes we always take the side that *receives* funding.
    """
    if not records:
        return None

    rates = [r.funding_rate for r in records]
    n = len(rates)

    cumulative_pnl_pct = 0.0
    peak_pnl_pct = 0.0
    max_dd_pct = 0.0
    wins = 0

    for r in rates:
        # We receive |funding_rate| (go short when funding>0, long when funding<0)
        pnl_pct = abs(r) * 100 - FEES_PER_PERIOD_PCT * 100  # convert to %
        cumulative_pnl_pct += pnl_pct

        if cumulative_pnl_pct > peak_pnl_pct:
            peak_pnl_pct = cumulative_pnl_pct
        dd = peak_pnl_pct - cumulative_pnl_pct
        if dd > max_dd_pct:
            max_dd_pct = dd

        if pnl_pct >= 0:
            wins += 1

    total_pnl_dollars = cumulative_pnl_pct / 100 * capital
    # Annualize: periods → days → years
    days_covered = n / FUNDING_PERIODS_PER_DAY
    years_covered = days_covered / DAYS_PER_YEAR if days_covered > 0 else 1
    annualized_return = (cumulative_pnl_pct / years_covered) if years_covered > 0 else 0

    return CarrySim(
        symbol=records[0].symbol,
        n_periods=n,
        total_pnl_pct=round(cumulative_pnl_pct, 4),
        total_pnl_dollars=round(total_pnl_dollars, 2),
        annualized_return_pct=round(annualized_return, 2),
        max_drawdown_pct=round(max_dd_pct, 4),
        win_rate_pct=round(wins / n * 100, 1),
    )


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
def build_markdown_report(
    stats: list[PairStats],
    sims: list[CarrySim],
    capital: float,
) -> str:
    # Sort by annualized mean (absolute value) for ranking
    ranked = sorted(stats, key=lambda s: abs(s.annualized_mean), reverse=True)

    lines: list[str] = []
    lines.append("# Funding Rate Arbitrage Analysis")
    lines.append("")
    lines.append(f"*Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}*")
    lines.append(f"*Data source: Public Binance FAPI — last {LIMIT} funding periods per pair (≈333 days)*")
    lines.append(f"*Simulated capital: ${capital:,.0f} delta-neutral*")
    lines.append("")

    # --- 1. Summary table ---
    lines.append("## 1. Funding Rate Summary by Pair")
    lines.append("")
    lines.append("| Rank | Pair | Periods | Mean Rate (8h) | Median | Std | % Positive | % Negative | Max | Min | Annualized Mean |")
    lines.append("|------|------|---------|----------------|--------|-----|------------|-------------|-----|-----|-----------------|")
    for i, s in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {s.symbol} | {s.n_periods} | {s.mean_rate*100:.5f}% | "
            f"{s.median_rate*100:.5f}% | {s.std_rate*100:.5f}% | "
            f"{s.pct_positive:.1f}% | {s.pct_negative:.1f}% | "
            f"{s.max_rate*100:.5f}% | {s.min_rate*100:.5f}% | "
            f"{s.annualized_mean:.2f}% |"
        )
    lines.append("")

    # --- 2. Carry simulation ---
    sims_sorted = sorted(sims, key=lambda s: s.annualized_return_pct, reverse=True)
    lines.append("## 2. Delta-Neutral Carry Simulation")
    lines.append("")
    lines.append(f"- **Capital**: ${capital:,.0f}")
    lines.append(f"- **Fee model**: {MAKER_FEE*100:.3f}% maker × 2 sides (entry+exit), amortized over {AMORTIZE_OVER_PERIODS} periods → ~{FEES_PER_PERIOD_PCT*100:.4f}% per 8h")
    lines.append(f"- **Strategy**: Short perp + hold spot when funding > 0; long perp + short spot when funding < 0")
    lines.append("")
    lines.append("| Rank | Pair | Total P&L ($) | Total P&L (%) | Annualized Return | Max Drawdown (%) | Win Rate (%) |")
    lines.append("|------|------|---------------|--------------|-------------------|-------------------|--------------|")
    for i, c in enumerate(sims_sorted, 1):
        lines.append(
            f"| {i} | {c.symbol} | ${c.total_pnl_dollars:,.2f} | "
            f"{c.total_pnl_pct:.2f}% | {c.annualized_return_pct:.2f}% | "
            f"{c.max_drawdown_pct:.2f}% | {c.win_rate_pct:.1f}% |"
        )
    lines.append("")

    # --- 3. Best carry pairs ---
    lines.append("## 3. Best Carry Pairs (Ranked by Annualized Return)")
    lines.append("")
    for i, c in enumerate(sims_sorted[:5], 1):
        s = next(st for st in stats if st.symbol == c.symbol)
        lines.append(f"### {i}. {c.symbol}")
        lines.append(f"- **Annualized funding rate**: {s.annualized_mean:.2f}%")
        lines.append(f"- **Simulated annualized return**: {c.annualized_return_pct:.2f}% after fees")
        lines.append(f"- **Win rate**: {c.win_rate_pct:.1f}% of 8h periods profitable after fees")
        lines.append(f"- **Max drawdown**: {c.max_drawdown_pct:.2f}%")
        lines.append(f"- **${capital} P&L over sample**: ${c.total_pnl_dollars:,.2f}")
        lines.append("")

    # --- 4. Feasibility assessment ---
    lines.append("## 4. Feasibility Assessment ($100–$1,000 Scale)")
    lines.append("")
    best = sims_sorted[0] if sims_sorted else None
    worst = sims_sorted[-1] if sims_sorted else None
    avg_ann = statistics.mean(c.annualized_return_pct for c in sims_sorted) if sims_sorted else 0
    lines.append(f"- **Best pair ({best.symbol if best else 'N/A'})**: ~{best.annualized_return_pct:.1f}% annualized after fees")
    lines.append(f"- **Average across all pairs**: ~{avg_ann:.1f}% annualized after fees")
    lines.append(f"- **Worst pair ({worst.symbol if worst else 'N/A'})**: ~{worst.annualized_return_pct:.1f}% annualized")
    lines.append("")
    lines.append("### Scale considerations")
    lines.append("")
    lines.append(f"| Capital | Est. Annual P&L (best pair) | Est. Annual P&L (avg pair) |")
    lines.append("|---------|-----------------------------|---------------------------|")
    for cap in [100, 250, 500, 1000]:
        best_pnl = cap * best.annualized_return_pct / 100 if best else 0
        avg_pnl = cap * avg_ann / 100
        lines.append(f"| ${cap} | ${best_pnl:,.2f} | ${avg_pnl:,.2f} |")
    lines.append("")

    # Caveats
    lines.append("### Caveats & Risks")
    lines.append("")
    lines.append("- Funding rates are **not guaranteed** — they reflect market sentiment and can flip or compress to zero.")
    lines.append("- **Liquidation risk** exists even in delta-neutral positions due to funding-induced mark-price divergence from index.")
    lines.append("- **Exchange risk**: Binance could change funding frequency, rates, or fee structures.")
    lines.append("- **Slippage** is not modeled — small-capital trades may face wider spreads on entry/exit.")
    lines.append("- This analysis uses **historical data only**; past funding patterns ≠ future.")
    lines.append("- **Capital efficiency**: delta-neutral requires ~2× notional (spot + futures), so $500 capital ≈ $250 per side.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 60)
    print("  Funding Rate Arbitrage Research Analysis")
    print("  (Public Binance FAPI — no API keys required)")
    print("=" * 60)

    # 1. Fetch
    print("\n[1/3] Fetching historical funding rates …")
    all_data = fetch_all_symbols(SYMBOLS)
    total_records = sum(len(v) for v in all_data.values())
    print(f"  Fetched {total_records} records across {len(all_data)} pairs.\n")

    if total_records == 0:
        print("[FATAL] No data fetched. Aborting.", file=sys.stderr)
        sys.exit(1)

    # 2. Analyze
    print("[2/3] Computing statistics and carry simulation …")
    stats: list[PairStats] = []
    sims: list[CarrySim] = []
    for sym, records in all_data.items():
        if not records:
            print(f"  {sym}: no data — skipping")
            continue
        pair_stats = compute_pair_stats(records)
        carry_sim = simulate_carry(records, CAPITAL)
        if pair_stats:
            stats.append(pair_stats)
        if carry_sim:
            sims.append(carry_sim)
        if pair_stats and carry_sim:
            print(
                f"  {sym}: mean={pair_stats.mean_rate*100:.5f}%  "
                f"ann={pair_stats.annualized_mean:.2f}%  "
                f"sim_ret={carry_sim.annualized_return_pct:.2f}%  "
                f"dd={carry_sim.max_drawdown_pct:.2f}%  "
                f"win={carry_sim.win_rate_pct:.1f}%"
            )

    # 3. Report
    print("\n[3/3] Generating report …")
    report = build_markdown_report(stats, sims, CAPITAL)

    # Save markdown
    out_dir = REPO_ROOT / "docs" / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "funding-rate-arb-analysis.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"  Report saved: {out_path}")

    # Also save raw JSON for programmatic use
    json_path = out_dir / "funding-rate-arb-data.json"
    json_data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "capital": CAPITAL,
        "stats": [
            {
                "symbol": s.symbol,
                "n_periods": s.n_periods,
                "mean_rate": s.mean_rate,
                "median_rate": s.median_rate,
                "std_rate": s.std_rate,
                "pct_positive": s.pct_positive,
                "pct_negative": s.pct_negative,
                "max_rate": s.max_rate,
                "min_rate": s.min_rate,
                "annualized_mean_pct": s.annualized_mean,
            }
            for s in stats
        ],
        "simulations": [
            {
                "symbol": c.symbol,
                "n_periods": c.n_periods,
                "total_pnl_pct": c.total_pnl_pct,
                "total_pnl_dollars": c.total_pnl_dollars,
                "annualized_return_pct": c.annualized_return_pct,
                "max_drawdown_pct": c.max_drawdown_pct,
                "win_rate_pct": c.win_rate_pct,
            }
            for c in sims
        ],
    }
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"  Raw data saved: {json_path}")

    print("\n" + "=" * 60)
    print("  DONE — Research analysis complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
