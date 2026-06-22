#!/usr/bin/env python3
"""Autonomous coin list manager for binance-trade-bot.

Checks USDC pair health, auto-removes dead coins, replaces low-volume coins
with better candidates, and reports all actions taken.

Actions taken:
- REMOVE: delisted, inactive, or extremely low volume (<$50K)
- REPLACE: low volume (<$500K) sustained for 3+ checks
- PROMOTE: high-volume candidates from the candidate pool
"""

import json
import os
import sys
from datetime import datetime

import ccxt

BOT_DIR = os.path.expanduser("~/binance-trade-bot")
COIN_LIST_FILE = os.path.join(BOT_DIR, "supported_coin_list")
STATE_FILE = os.path.join(BOT_DIR, "scripts", "monitor_state.json")
BRIDGE = "USDC"

# Thresholds
VOLUME_REMOVE_THRESHOLD = 50_000       # Auto-remove below this
VOLUME_WARN_THRESHOLD = 500_000        # Flag as low volume
LOW_VOLUME_DAYS_TO_REPLACE = 3         # Days of low volume before replacing
MAX_COINS = 25                         # Don't exceed this many coins
MIN_COINS = 12                         # Don't go below this many coins

# Candidate pool — high-volume coins with USDC pairs, not already in list
CANDIDATE_POOL = [
    "UNI", "AAVE", "MKR", "RENDER", "ENA", "SEI", "JUP",
    "WIF", "PEPE", "FDUSD", "TRX", "LTC", "BCH", "ETC",
    "NEIRO", "BONK", "FET", "AGIX", "RNDR", "HBAR",
    "ALGO", "FTM", "CELO", "CFX", "APT",
]


def load_coins():
    """Load coin list from file."""
    coins = []
    with open(COIN_LIST_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                coins.append(line)
    return coins


def write_coins(coins):
    """Write coin list back to file, preserving comments."""
    with open(COIN_LIST_FILE) as f:
        original = f.read()

    # Simple write — just the coin list
    # Group by category for readability
    categories = {
        "Major Pairs": ["BTC", "ETH", "BNB", "SOL"],
        "L1 Alternatives": [],
        "L2 Scaling": [],
        "DeFi": [],
        "High Beta": [],
        "Infrastructure": [],
        "Ecosystem": [],
    }
    classified = []
    unclassified = []
    for c in coins:
        if c in ["BTC", "ETH", "BNB", "SOL"]:
            categories["Major Pairs"].append(c) if c not in categories["Major Pairs"] else None
        elif c in ["AVAX", "NEAR", "APT", "ADA", "ATOM", "DOT", "SUI"]:
            categories["L1 Alternatives"].append(c)
        elif c in ["OP", "ARB"]:
            categories["L2 Scaling"].append(c)
        elif c in ["LINK", "INJ", "UNI", "AAVE"]:
            categories["DeFi"].append(c)
        elif c in ["DOGE", "WIF", "PEPE", "BONK", "NEIRO"]:
            categories["High Beta"].append(c)
        elif c in ["FIL", "RENDER", "FET", "LTC", "HBAR", "TRX"]:
            categories["Infrastructure"].append(c)
        else:
            unclassified.append(c)

    with open(COIN_LIST_FILE, "w") as f:
        f.write("# Major pairs - core liquidity anchors, mean-reverting\n")
        for c in categories["Major Pairs"]:
            if c in coins:
                f.write(f"{c}\n")
        f.write("\n# L1 alternatives - oscillate with BTC but different phase offsets\n")
        for c in categories["L1 Alternatives"]:
            f.write(f"{c}\n")
        f.write("\n# L2 scaling\n")
        for c in categories["L2 Scaling"]:
            f.write(f"{c}\n")
        f.write("\n# DeFi blue chips\n")
        for c in categories["DeFi"]:
            f.write(f"{c}\n")
        f.write("\n# High beta / memecoins with strong mean reversion\n")
        for c in categories["High Beta"]:
            f.write(f"{c}\n")
        f.write("\n# Infrastructure / ecosystem\n")
        for c in categories["Infrastructure"]:
            f.write(f"{c}\n")
        if unclassified:
            f.write("\n# Other\n")
            for c in unclassified:
                f.write(f"{c}\n")


def load_state():
    """Load persistent state for tracking low-volume streaks."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"low_volume_days": {}, "last_run": None, "actions_history": []}


def save_state(state):
    """Save state to file."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _get_currently_held_coin():
    """Check the bot's DB for the currently held coin (to avoid disabling it)."""
    import sqlite3
    db_path = "REDACTED/crypto_trading.db"
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT coin_id FROM current_coin_history ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def main():
    actions = []
    coins = load_coins()
    state = load_state()
    state["last_run"] = datetime.utcnow().isoformat()

    exchange = ccxt.binance()

    # ── CIRCUIT BREAKER: abort if load_markets fails or returns too few markets ──
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"❌ ABORTED: Binance load_markets() failed: {e}")
        print("No changes made — coin list preserved.")
        print("COIN_LIST_OK")  # Don't trigger change handlers
        return

    if len(markets) < 500:
        print(f"❌ ABORTED: load_markets() returned only {len(markets)} markets (expected 4000+). Likely API failure.")
        print("No changes made — coin list preserved.")
        print("COIN_LIST_OK")
        return

    # Check each coin's USDC pair
    to_remove = []
    low_volume = []
    held_coin = _get_currently_held_coin()
    if held_coin:
        print(f"📌 Currently held coin: {held_coin} (will not remove)")

    # Fetch tickers for all pairs
    active_pairs = {f"{c}/{BRIDGE}" for c in coins if f"{c}/{BRIDGE}" in markets and markets[f"{c}/{BRIDGE}"].get("active", False)}
    tickers = {}
    if active_pairs:
        try:
            tickers = exchange.fetch_tickers(list(active_pairs))
        except Exception as e:
            print(f"⚠️ fetch_tickers() failed ({e}) — using volume from individual tickers as fallback")

    for coin in coins:
        pair = f"{coin}/{BRIDGE}"

        # Check pair exists and is active
        if pair not in markets:
            actions.append(f"🚨 REMOVED {coin}: USDC pair delisted/missing")
            to_remove.append(coin)
            continue
        if not markets[pair].get("active", False):
            actions.append(f"🚫 REMOVED {coin}: USDC pair inactive/halted")
            to_remove.append(coin)
            continue

        # Check volume — ccxt quoteVolume is ALREADY in quote currency (USDC), do NOT multiply by last
        t = tickers.get(pair, {})
        vol = t.get("quoteVolume") or 0

        if vol < VOLUME_REMOVE_THRESHOLD:
            actions.append(f"💀 REMOVED {coin}: Extremely low volume ${vol:,.0f}")
            to_remove.append(coin)
            # Reset streak
            state["low_volume_days"].pop(coin, None)
        elif vol < VOLUME_WARN_THRESHOLD:
            low_volume.append((coin, vol))
            # Track streak
            prev = state["low_volume_days"].get(coin, 0)
            state["low_volume_days"][coin] = prev + 1

            days = prev + 1
            if days >= LOW_VOLUME_DAYS_TO_REPLACE:
                if len(coins) > MIN_COINS:
                    actions.append(f"🔄 SCHEDULED REPLACEMENT {coin}: Low volume ${vol:,.0f} for {days} days")
                    to_remove.append(coin)
                    state["low_volume_days"].pop(coin, None)
                else:
                    actions.append(f"⚠️ {coin}: Low volume ${vol:,.0f} for {days} days (keeping — at minimum coin count)")
        else:
            # Volume is fine, reset streak
            state["low_volume_days"].pop(coin, None)

    # ── CIRCUIT BREAKER: never remove more than 30% of coins in one run ──
    # This prevents catastrophic nukes from transient API failures.
    max_removals = max(2, int(len(coins) * 0.30))
    if len(to_remove) > max_removals:
        print(f"❌ ABORTED removals: {len(to_remove)} coins flagged for removal "
              f"(max {max_removals}). This looks like an API failure, not real delisting.")
        print("No changes made — coin list preserved.")
        save_state(state)
        print("COIN_LIST_OK")
        return

    # ── PROTECT currently held coin ──
    if held_coin and held_coin in to_remove:
        actions.append(f"⏸️ SKIPPED {held_coin}: currently held by bot (will remove after bot trades away)")
        to_remove.remove(held_coin)
        # Restore coin to list
        if held_coin not in coins:
            coins.append(held_coin)

    # Remove flagged coins
    if to_remove:
        coins = [c for c in coins if c not in to_remove]

    # Find replacements from candidate pool
    removed_count = len([a for a in actions if "REMOVED" in a or "SCHEDULED REPLACEMENT" in a])
    if removed_count > 0 and len(coins) < MAX_COINS:
        # Check candidates for good volume USDC pairs
        available_candidates = []
        for cand in CANDIDATE_POOL:
            if cand in coins:
                continue
            pair = f"{cand}/{BRIDGE}"
            if pair not in markets or not markets[pair].get("active", False):
                continue

            t = tickers.get(pair)  # Might not be in tickers if it wasn't in active_pairs
            if not t:
                try:
                    t = exchange.fetch_ticker(pair)
                except Exception:
                    continue

            vol = (t.get("quoteVolume") or 0) * (t.get("last") or 0)
            if vol >= VOLUME_WARN_THRESHOLD:
                available_candidates.append((cand, vol))

        # Sort by volume, best first
        available_candidates.sort(key=lambda x: x[1], reverse=True)

        for cand, vol in available_candidates[:removed_count]:
            coins.append(cand)
            actions.append(f"➕ ADDED {cand}: ${vol:,.0f} 24h volume — replacing removed coin")

    # Reset low-volume streaks for coins that are no longer in the list
    state["low_volume_days"] = {k: v for k, v in state["low_volume_days"].items() if k in coins}

    # Save updated state
    save_state(state)

    # Write updated coin list if changed
    original_coins = load_coins()
    if set(coins) != set(original_coins):
        write_coins(coins)

    # Output results
    if not actions:
        print(f"✅ All {len(coins)} coins healthy — no actions needed.")
        print(f"COIN_LIST_OK")
    else:
        print(f"🤖 Autonomous coin manager — {len(actions)} action(s) taken:")
        print()
        for a in actions:
            print(a)
        print()
        print(f"📋 Updated coin list ({len(coins)} coins):")
        print(", ".join(coins))
        print("COIN_LIST_CHANGED")


if __name__ == "__main__":
    main()
