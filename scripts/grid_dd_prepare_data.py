#!/usr/bin/env python3
"""
Prepare combined kline cache for the 10 requested coins.
Uses existing high_alpha_klines.npz for 6 coins and fetches
the 4 missing (LINK, INJ, APT, OP) from Binance public API.

Writes scripts/_cache_klines/grid_dd_klines.npz
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO = Path(__file__).resolve().parents[1]
EXISTING = REPO / "scripts" / "_cache_klines" / "high_alpha_klines.npz"
OUT = REPO / "scripts" / "_cache_klines" / "grid_dd_klines.npz"

BINANCE_API = "https://api.binance.com/api/v3/klines"
HOURS = 4320  # 180 days hourly, matching existing cache
DTYPE = np.dtype([("ts", "<i8"), ("open", "<f8"), ("high", "<f8"),
                  ("low", "<f8"), ("close", "<f8"), ("volume", "<f8")])

WANTED = ["BTCUSDC", "ETHUSDC", "SOLUSDC", "LINKUSDC", "AVAXUSDC",
          "DOGEUSDC", "XRPUSDC", "INJUSDC", "APTUSDC", "OPUSDC"]


def fetch(symbol: str) -> np.ndarray:
    all_rows = []
    remaining = HOURS
    end_time = None
    while remaining > 0:
        batch = min(remaining, 1000)
        params = {"symbol": symbol, "interval": "1h", "limit": batch}
        if all_rows:
            params["endTime"] = all_rows[0][0] - 1
        r = requests.get(BINANCE_API, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_rows = data + all_rows
        remaining -= len(data)
        if len(data) < 1000:
            break
        time.sleep(0.2)
    arr = np.empty(len(all_rows), dtype=DTYPE)
    for i, row in enumerate(all_rows):
        arr[i] = (int(row[0]), float(row[1]), float(row[2]),
                  float(row[3]), float(row[4]), float(row[5]))
    return arr


def main():
    existing = np.load(EXISTING, allow_pickle=True)
    out = {}
    for sym in WANTED:
        if sym in existing.files:
            out[sym] = existing[sym]
            a = out[sym]
            print(f"  {sym}: cached {len(a)} candles {pd.Timestamp(a['ts'][0],unit='ms',tz='UTC').date()} -> {pd.Timestamp(a['ts'][-1],unit='ms',tz='UTC').date()}")
        else:
            print(f"  {sym}: fetching from Binance...")
            try:
                out[sym] = fetch(sym)
                a = out[sym]
                print(f"    OK {len(a)} candles {pd.Timestamp(a['ts'][0],unit='ms',tz='UTC').date()} -> {pd.Timestamp(a['ts'][-1],unit='ms',tz='UTC').date()}")
            except Exception as e:
                print(f"    FAIL: {e}")
    np.savez(OUT, **out)
    print(f"\nSaved {len(out)} symbols -> {OUT}")
    print(f"Symbols: {list(out.keys())}")


if __name__ == "__main__":
    main()
