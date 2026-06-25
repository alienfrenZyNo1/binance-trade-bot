#!/usr/bin/env python3
"""Research-only BEAR futures short backtester.

Models USDC-M/USDT-M short candidates using public Binance futures data. This
script is intentionally research-only: it never places orders or reads private
account endpoints.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.strategy_acceptance_gates import build_research_output

BINANCE_FAPI = "https://fapi.binance.com"
BRIDGE = "USDC"
DEFAULT_SYMBOLS = ("SOLUSDC", "XRPUSDC", "ADAUSDC", "DOGEUSDC", "NEARUSDC", "LINKUSDC", "AAVEUSDC", "AVAXUSDC", "SUIUSDC", "TIAUSDC", "ENAUSDC")
HOUR_MS = 3600 * 1000
DAY_MS = 86400 * 1000


@dataclass
class BacktestConfig:
    initial_balance: float = 1000.0
    leverage: float = 2.0
    max_margin_pct: float = 0.40
    fee_rate: float = 0.00075
    slippage_pct: float = 0.0005
    stop_loss_pct: float = 12.0
    trailing_activation_pct: float = 3.0
    trailing_callback_pct: float = 1.0
    lookback_hours: int = 24
    min_oi_change_pct: float = 0.0
    cooldown_hours: int = 4

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def parse_klines(raw_klines: list[list]) -> list[dict[str, float | int]]:
    return [
        {
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw_klines
    ]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _change_pct(values: list[float]) -> float:
    values = [value for value in values if value > 0]
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return (values[-1] / values[0] - 1.0) * 100.0


def fetch_json(path: str, params: dict[str, Any]) -> Any:
    resp = requests.get(f"{BINANCE_FAPI}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_futures_klines(symbol: str, *, interval: str = "1h", days: int = 30) -> list[dict[str, float | int]]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        data = fetch_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not data:
            break
        rows.extend(data)
        cur = int(data[-1][0]) + 1
        if len(data) < 1000:
            break
        time.sleep(0.12)
    return parse_klines(rows)


def fetch_funding_rates(symbol: str, *, limit: int = 100) -> list[dict[str, Any]]:
    return fetch_json("/fapi/v1/fundingRate", {"symbol": symbol, "limit": min(limit, 1000)})


def fetch_open_interest_hist(symbol: str, *, period: str = "1h", limit: int = 120) -> list[dict[str, Any]]:
    return fetch_json(
        "/futures/data/openInterestHist",
        {"symbol": symbol, "period": period, "limit": min(limit, 500)},
    )


def load_market_data(symbols: Iterable[str], *, days: int = 30, interval: str = "1h") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    funding_limit = max(8, min(1000, days * 3 + 4))
    oi_limit = max(24, min(500, days * 24 + 1))
    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        payload: dict[str, Any] = {"candles": [], "funding": [], "open_interest": []}
        try:
            payload["candles"] = fetch_futures_klines(symbol, interval=interval, days=days)
            time.sleep(0.05)
            payload["funding"] = fetch_funding_rates(symbol, limit=funding_limit)
            time.sleep(0.05)
            payload["open_interest"] = fetch_open_interest_hist(symbol, period=interval, limit=oi_limit)
        except requests.RequestException as exc:
            payload["error"] = str(exc)
        out[symbol] = payload
        time.sleep(0.05)
    return out


def _momentum_pct(candles: list[dict[str, float | int]], lookback_hours: int) -> float | None:
    if len(candles) < lookback_hours + 1:
        return None
    start = float(candles[-lookback_hours - 1]["close"])
    end = float(candles[-1]["close"])
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def _oi_value_change_pct(rows: list[dict[str, Any]]) -> float:
    values = [_safe_float(row.get("sumOpenInterestValue")) for row in rows]
    return _change_pct(values)


def rank_short_candidates(
    market_data: dict[str, dict[str, Any]],
    *,
    lookback_hours: int = 24,
    min_oi_change_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """Rank futures symbols by weakest lookback momentum, with OI context."""
    ranked: list[dict[str, Any]] = []
    for symbol, payload in market_data.items():
        candidate = _candidate_from_window(
            symbol,
            payload.get("candles") or [],
            payload.get("open_interest") or [],
            lookback_hours=lookback_hours,
            min_oi_change_pct=min_oi_change_pct,
        )
        if candidate:
            ranked.append(candidate)
    ranked.sort(key=lambda row: row["momentum_pct"])
    return ranked


def _row_ts(row: dict[str, Any]) -> int:
    return int(row.get("timestamp", row.get("time", row.get("fundingTime", 0))) or 0)


def _rows_through(rows: list[dict[str, Any]], ts: int, limit: int | None = None) -> list[dict[str, Any]]:
    filtered = [row for row in rows if _row_ts(row) <= ts]
    return filtered[-limit:] if limit else filtered


def _candidate_from_window(
    symbol: str,
    candles: list[dict[str, float | int]],
    open_interest: list[dict[str, Any]],
    *,
    lookback_hours: int,
    min_oi_change_pct: float,
) -> dict[str, Any] | None:
    momentum = _momentum_pct(candles, lookback_hours)
    if momentum is None:
        return None
    signal_ts = int(candles[-1]["ts"])
    oi_change = _oi_value_change_pct(_rows_through(open_interest, signal_ts, limit=lookback_hours + 1))
    return {
        "symbol": symbol,
        "signal_ts": signal_ts,
        "momentum_pct": round(momentum, 6),
        "oi_value_change_pct": round(oi_change, 6),
        "eligible": bool(momentum < 0 and oi_change >= min_oi_change_pct),
        "latest_price": float(candles[-1]["close"]),
        "candles": len(candles),
    }


def _next_candle_index_after(candles: list[dict[str, float | int]], ts: int, start: int) -> int:
    for idx in range(start, len(candles)):
        if int(candles[idx]["ts"]) > ts:
            return idx
    return len(candles)


def _funding_pnl_between(funding_rates: list[dict[str, Any]], start_ts: int, end_ts: int, notional: float) -> float:
    pnl = 0.0
    for row in funding_rates:
        ts = int(row.get("fundingTime", row.get("time", 0)) or 0)
        if start_ts < ts <= end_ts:
            # Binance convention: positive funding = longs pay shorts, so shorts receive.
            pnl += notional * _safe_float(row.get("fundingRate"))
    return pnl


def _exit_values(entry_fill: float, exit_ref: float, qty: float, config: BacktestConfig) -> tuple[float, float]:
    exit_fill = exit_ref * (1.0 + config.slippage_pct)
    exit_notional = exit_fill * qty
    return exit_fill, exit_notional


def simulate_short(
    symbol: str,
    candles: list[dict[str, float | int]],
    *,
    funding_rates: list[dict[str, Any]] | None = None,
    config: BacktestConfig | None = None,
) -> dict[str, Any]:
    """Simulate one short from the first candle until stop/trailing/end."""
    if not candles:
        raise ValueError("candles are required")
    config = config or BacktestConfig()
    funding_rates = funding_rates or []

    entry = candles[0]
    entry_ts = int(entry["ts"])
    entry_ref = float(entry["close"])
    entry_fill = entry_ref * (1.0 - config.slippage_pct)
    margin = config.initial_balance * config.max_margin_pct
    notional = margin * config.leverage
    qty = notional / entry_fill if entry_fill > 0 else 0.0
    entry_fee = notional * config.fee_rate
    fees = entry_fee
    funding_pnl = 0.0
    price_pnl = 0.0
    max_drawdown_pct = 0.0
    stop_loss_exits = 0
    trailing_exits = 0
    exit_reason = "end"
    exit_ts = int(candles[-1]["ts"])
    exit_ref = float(candles[-1]["close"])

    stop_price = entry_fill * (1.0 + config.stop_loss_pct / 100.0)
    activation_price = entry_fill * (1.0 - config.trailing_activation_pct / 100.0)
    trailing_active = False
    low_watermark = entry_fill
    liq_price = entry_fill * (1.0 + (1.0 / max(config.leverage, 0.000001)))
    min_liq_buffer = float("inf")
    prev_ts = entry_ts

    for row in candles[1:]:
        ts = int(row["ts"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        funding_pnl += _funding_pnl_between(funding_rates, prev_ts, ts, notional)

        worst_unrealized = (entry_fill - high) * qty
        worst_equity = config.initial_balance + worst_unrealized + funding_pnl - fees
        dd = max(0.0, (config.initial_balance - worst_equity) / config.initial_balance * 100.0)
        max_drawdown_pct = max(max_drawdown_pct, dd)
        min_liq_buffer = min(min_liq_buffer, (liq_price - high) / entry_fill * 100.0)

        if high >= stop_price:
            exit_reason = "stop_loss"
            stop_loss_exits = 1
            exit_ref = stop_price
            exit_ts = ts
            break

        if low <= activation_price:
            trailing_active = True
        if trailing_active:
            low_watermark = min(low_watermark, low)
            trigger = low_watermark * (1.0 + config.trailing_callback_pct / 100.0)
            if trigger < entry_fill and high >= trigger:
                exit_reason = "trailing_stop"
                trailing_exits = 1
                exit_ref = trigger
                exit_ts = ts
                break

        exit_ref = close
        exit_ts = ts
        prev_ts = ts

    exit_fill, exit_notional = _exit_values(entry_fill, exit_ref, qty, config)
    exit_fee = exit_notional * config.fee_rate
    fees += exit_fee
    price_pnl = (entry_fill - exit_fill) * qty
    net_pnl = price_pnl + funding_pnl - fees
    final_balance = config.initial_balance + net_pnl
    min_liq_buffer = 0.0 if min_liq_buffer == float("inf") else min_liq_buffer

    return {
        "name": f"bear_short_{symbol}",
        "strategy": "bear_futures_short",
        "regime": "bear",
        "symbol": symbol,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry": entry_fill,
        "exit": exit_fill,
        "exit_reason": exit_reason,
        "qty": qty,
        "notional": notional,
        "margin": margin,
        "leverage": config.leverage,
        "price_pnl": price_pnl,
        "funding_pnl": funding_pnl,
        "fees": fees,
        "total_fees": fees,
        "fee_pct": fees / config.initial_balance * 100.0 if config.initial_balance else 0.0,
        "net_pnl": net_pnl,
        "final": final_balance,
        "pnl_pct": net_pnl / config.initial_balance * 100.0 if config.initial_balance else 0.0,
        "oos_pnl": net_pnl / config.initial_balance * 100.0 if config.initial_balance else 0.0,
        "baseline_pnl": 0.0,
        "baseline_pnl_pct": 0.0,
        "max_drawdown_pct": max_drawdown_pct,
        "max_drawdown": max_drawdown_pct,
        "trades": 1,
        "trade_count": 1,
        "stop_loss_exits": stop_loss_exits,
        "trailing_exits": trailing_exits,
        "min_liquidation_buffer_pct": min_liq_buffer,
        "account_stop_risk_pct": round(config.max_margin_pct * config.leverage * config.stop_loss_pct, 6),
        "params": config.to_dict(),
        "initial_balance": config.initial_balance,
    }


def run_backtest(market_data: dict[str, dict[str, Any]], config: BacktestConfig) -> dict[str, Any]:
    """Walk forward through each symbol and simulate point-in-time BEAR signals."""
    current_ranked = rank_short_candidates(
        market_data,
        lookback_hours=config.lookback_hours,
        min_oi_change_pct=config.min_oi_change_pct,
    )
    records = []
    cooldown_ms = int(config.cooldown_hours * HOUR_MS)

    for symbol, payload in market_data.items():
        candles = payload.get("candles") or []
        if len(candles) <= config.lookback_hours + 1:
            continue
        open_interest = payload.get("open_interest") or []
        funding = payload.get("funding") or []
        idx = config.lookback_hours
        while idx < len(candles) - 1:
            window = candles[: idx + 1]
            candidate = _candidate_from_window(
                symbol,
                window,
                open_interest,
                lookback_hours=config.lookback_hours,
                min_oi_change_pct=config.min_oi_change_pct,
            )
            if not candidate or not candidate["eligible"]:
                idx += 1
                continue

            result = simulate_short(
                symbol,
                candles[idx:],
                funding_rates=funding,
                config=config,
            )
            result["candidate"] = {k: v for k, v in candidate.items() if k != "candles"}
            records.append(result)
            if result["exit_ts"] >= int(candles[-1]["ts"]):
                break
            cooldown_until = int(result["exit_ts"]) + cooldown_ms
            idx = _next_candle_index_after(candles, cooldown_until, idx + 1)

    records.sort(key=lambda row: (row["entry_ts"], row["symbol"]))
    symbols_for_manifest = {
        symbol.replace(BRIDGE, ""): payload.get("candles") or []
        for symbol, payload in market_data.items()
    }
    return build_research_output(
        records,
        ohlcv_by_coin=symbols_for_manifest,
        interval="1h",
        bridge=BRIDGE,
        assumptions={
            **config.to_dict(),
            "data_source": "Binance public futures endpoints",
            "funding_convention": "positive funding = longs pay shorts = shorts receive",
            "entry_logic": "walk-forward point-in-time negative momentum plus OI filter",
        },
        gates={"min_trades": 1},
    ) | {"candidates": current_ranked}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="History length in days")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated futures symbols")
    parser.add_argument("--output", default="bear_futures_backtest.json", help="JSON output path")
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--max-margin-pct", type=float, default=0.40)
    parser.add_argument("--stop-loss-pct", type=float, default=12.0)
    parser.add_argument("--trailing-activation-pct", type=float, default=3.0)
    parser.add_argument("--trailing-callback-pct", type=float, default=1.0)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--min-oi-change-pct", type=float, default=0.0)
    parser.add_argument("--cooldown-hours", type=int, default=4)
    parser.add_argument("--fee-rate", type=float, default=0.00075)
    parser.add_argument("--slippage-pct", type=float, default=0.0005)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    config = BacktestConfig(
        initial_balance=args.initial_balance,
        leverage=args.leverage,
        max_margin_pct=args.max_margin_pct,
        fee_rate=args.fee_rate,
        slippage_pct=args.slippage_pct,
        stop_loss_pct=args.stop_loss_pct,
        trailing_activation_pct=args.trailing_activation_pct,
        trailing_callback_pct=args.trailing_callback_pct,
        lookback_hours=args.lookback_hours,
        min_oi_change_pct=args.min_oi_change_pct,
        cooldown_hours=args.cooldown_hours,
    )
    print(f"Fetching {args.days}d public futures data for {len(symbols)} symbols...")
    market_data = load_market_data(symbols, days=args.days, interval="1h")
    payload = run_backtest(market_data, config)
    print("\nBEAR futures candidates:")
    for row in payload["candidates"][:10]:
        tag = "ELIG" if row["eligible"] else "WAIT"
        print(
            f"  {row['symbol']:<10} {row['momentum_pct']:+7.2f}% "
            f"OI {row['oi_value_change_pct']:+6.1f}% {tag}"
        )
    print("\nBacktest leaderboard:")
    for row in payload["leaderboard"]["overall"][:10]:
        print(
            f"  {row['name']:<24} pass={row['passed']} "
            f"pnl={row['metrics']['pnl_pct']:+.2f}% dd={row['metrics']['max_drawdown_pct']:.2f}%"
        )
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"\nSaved to {args.output}")
    return payload


if __name__ == "__main__":
    main()
