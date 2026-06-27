#!/usr/bin/env python3
"""Fetch 2+ years of hourly klines for extended-history trend backtests.

IMPORTANT SUBSTITUTION (documented in the report):
  The task asked for USDC-M perps (BTCUSDC, ETHUSDC, ...). However, USDC-M
  perpetuals on Binance only list from 2024-01-04 onward (~18 months), which
  is STILL almost entirely the bear regime we are trying to escape — and
  INJUSDC does not exist at all. USDT-M perps list back to 2019-2022 and
  therefore are the ONLY way to obtain 2+ years covering a bull market.

  USDC and USDT trade within <0.1% of each other 24/7 (both USD-pegged
  stables), so trend signals (Donchian breakout, Supertrend) computed on one
  are numerically equivalent to the other. We therefore fetch USDT-M pairs:
    BTCUSDT, ETHUSDT, SOLUSDT, LINKUSDT, NEARUSDT, INJUSDT

Caches to scripts/_cache_klines_extended/<SYMBOL>.pkl. Paginated, deduped,
resumable. Public API, no key needed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
CACHE_DIR = Path(__file__).resolve().parent / "_cache_klines_extended"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# (symbol, earliest_start_ms). Start each a bit before its listing for safety;
# API returns the first available bar if startTime pre-dates listing.
# Window target: from 2023-01-01 (captures 2023 recovery bull + 2024 ATH bull
# + 2025 sideways + 2026 bear). INJ lists 2022-08.
START_MS = {
    "BTCUSDT":  1_672_531_200_000,  # 2023-01-01
    "ETHUSDT":  1_672_531_200_000,  # 2023-01-01
    "SOLUSDT":  1_672_531_200_000,  # 2023-01-01
    "LINKUSDT": 1_672_531_200_000,  # 2023-01-01
    "NEARUSDT": 1_672_531_200_000,  # 2023-01-01
    "INJUSDT":  1_660_924_800_000,  # 2022-08-20
}
SYMBOLS = list(START_MS.keys())
LIMIT = 1500  # Binance fapi max


def fetch_symbol(symbol: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    if end_ms is None:
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows: list[list] = []
    cur = start_ms
    n_req = 0
    while cur < end_ms:
        params = {"symbol": symbol, "interval": "1h", "startTime": cur,
                  "endTime": end_ms, "limit": LIMIT}
        data = None
        for attempt in range(5):
            try:
                r = requests.get(f"{FAPI}/fapi/v1/klines", params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 4:
                    print(f"    [WARN] {symbol} fetch failed at {cur}: {exc}")
                    data = []
                    break
                time.sleep(0.6 * (attempt + 1))
        if not data:
            break
        rows.extend(data)
        n_req += 1
        last_ts = data[-1][0]
        if last_ts <= cur:  # no progress guard
            break
        cur = last_ts + HOUR_MS
        time.sleep(0.10)  # be polite (~10 req/s)
        if len(data) < LIMIT:
            break
    # dedup by ts
    seen: set[int] = set()
    uniq = []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            uniq.append(row)
    df = pd.DataFrame(uniq, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_time", "qv", "trades", "tbv", "tbqv", "ign"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["ts"] = pd.to_numeric(df["ts"]).astype("int64")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df[["ts", "open", "high", "low", "close", "volume"]], n_req


def main() -> None:
    print("=" * 70)
    print(" Fetching extended-history hourly klines (USDT-M, 2+ years)")
    print("=" * 70)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for sym in SYMBOLS:
        cache_file = CACHE_DIR / f"{sym}.pkl"
        start_ms = START_MS[sym]
        # resumable: if cache has enough recent data, skip
        if cache_file.exists():
            try:
                cached = pd.read_pickle(cache_file)
                # require data within last 2 days of now and starting before start+7d
                fresh = cached["ts"].iloc[-1] > end_ms - 2 * 86_400_000
                old = cached["ts"].iloc[0] <= start_ms + 7 * 86_400_000
                if fresh and old and len(cached) > 15000:
                    print(f"  {sym}: cached ({len(cached)} bars) "
                          f"{datetime.fromtimestamp(cached['ts'].iloc[0]/1000, tz=timezone.utc).date()} "
                          f"-> {datetime.fromtimestamp(cached['ts'].iloc[-1]/1000, tz=timezone.utc).date()}  [skip]")
                    continue
            except Exception:  # noqa: BLE001
                pass
        print(f"  Fetching {sym} from "
              f"{datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).date()} ...")
        df, n_req = fetch_symbol(sym, start_ms, end_ms)
        df.to_pickle(cache_file)
        s = datetime.fromtimestamp(df["ts"].iloc[0] / 1000, tz=timezone.utc).date()
        e = datetime.fromtimestamp(df["ts"].iloc[-1] / 1000, tz=timezone.utc).date()
        days = len(df) / 24
        print(f"    {sym}: {len(df)} bars ({days:.0f} days, {n_req} reqs)  {s} -> {e}")
        print(f"       first close={df['close'].iloc[0]:.4f}  last close={df['close'].iloc[-1]:.4f}")
    print("\nDone. Cache at", CACHE_DIR)


if __name__ == "__main__":
    main()
