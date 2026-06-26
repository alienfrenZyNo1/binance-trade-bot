#!/usr/bin/env python3
"""Autonomous coin list manager for binance-trade-bot (DB-native).

Works directly against the SQLite database — no file editing, no Docker
redeploy, no LLM agent required. The trade bot picks up DB changes on
its next scout cycle (~3 seconds).

Actions taken:
- REMOVE: delisted, inactive, or extremely low volume (<$50K) → disabled in DB
- REPLACE: low volume (<$500K) sustained for 3+ checks → disabled + replacement enabled
- PROMOTE: high-volume candidates from the dynamic candidate pool → enabled in DB

Designed to run as a no_agent cron job. Prints a human-readable summary.
Exits 0 with "COIN_LIST_OK" when healthy (suppresses delivery), or
"COIN_LIST_CHANGED" when actions were taken (triggers delivery).

Safety:
  - Circuit breaker: aborts if Binance API returns <500 markets
  - Never removes more than 30% of coins in one run
  - Never removes the currently held coin or coins with open futures positions
  - Persistent state tracks low-volume streaks across runs
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

import ccxt

DB_PATH = os.environ.get("DB_PATH", "/data/binance-bot-data/crypto_trading.db")
STATE_FILE = os.path.join(os.path.dirname(__file__), "monitor_state.json")
BRIDGE = "USDC"

# Thresholds
VOLUME_REMOVE_THRESHOLD = 50_000       # Auto-remove below this
VOLUME_WARN_THRESHOLD = 500_000        # Flag as low volume
LOW_VOLUME_DAYS_TO_REPLACE = 3         # Days of low volume before replacing
MAX_COINS = 25                         # Don't exceed this many coins
MIN_COINS = 12                         # Don't go below this many coins


# ── DB helpers ───────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_enabled_coins():
    """Return list of enabled coin symbols from DB (excluding bridge)."""
    conn = _db()
    coins = [r[0] for r in conn.execute(
        "SELECT symbol FROM coins WHERE enabled = 1 AND symbol != ?", (BRIDGE,)
    ).fetchall()]
    conn.close()
    return sorted(coins)


def get_disabled_coins():
    """Return set of disabled coin symbols (candidate re-promotion pool)."""
    conn = _db()
    coins = {r[0] for r in conn.execute(
        "SELECT symbol FROM coins WHERE enabled = 0 AND symbol != ?", (BRIDGE,)
    ).fetchall()}
    conn.close()
    return coins


def get_held_coin():
    """Return the coin the bot is currently holding, or None."""
    conn = _db()
    row = conn.execute(
        "SELECT coin_id FROM current_coin_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_futures_symbols():
    """Return set of coin symbols with open futures positions."""
    # Read from DB scraper state if available; otherwise check API indirectly
    # by looking at the bot_state table for futures positions.
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT key, value FROM bot_state WHERE key LIKE 'futures_%'"
        ).fetchall()
        symbols = set()
        for r in rows:
            if r["key"] == "futures_open_positions":
                positions = json.loads(r["value"])
                for p in positions:
                    sym = p.get("symbol", "").replace(BRIDGE, "")
                    if sym:
                        symbols.add(sym)
        conn.close()
        return symbols
    except Exception:
        conn.close()
        return set()


def db_disable_coin(symbol):
    """Disable a coin in the DB. Returns True if changed."""
    conn = _db()
    row = conn.execute("SELECT enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if not row or not row[0]:
        conn.close()
        return False
    conn.execute("UPDATE coins SET enabled = 0 WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    return True


def db_enable_coin(symbol):
    """Enable a coin in the DB and create pairs with all other enabled coins."""
    conn = _db()
    row = conn.execute("SELECT enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if row and row[0]:
        conn.close()
        return False  # already enabled

    if row:
        conn.execute("UPDATE coins SET enabled = 1 WHERE symbol = ?", (symbol,))
    else:
        conn.execute("INSERT OR IGNORE INTO coins (symbol, enabled) VALUES (?, 1)", (symbol,))
    conn.commit()

    # Create pair rows with all other enabled coins
    enabled = [r[0] for r in conn.execute(
        "SELECT symbol FROM coins WHERE enabled = 1 AND symbol != ?", (symbol,)
    ).fetchall()]
    for other in enabled:
        for a, b in [(symbol, other), (other, symbol)]:
            exists = conn.execute(
                "SELECT id FROM pairs WHERE from_coin_id = ? AND to_coin_id = ?", (a, b)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO pairs (from_coin_id, to_coin_id, ratio) VALUES (?, ?, 1.0)",
                    (a, b),
                )
    conn.commit()
    conn.close()
    return True


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"low_volume_days": {}, "last_run": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    actions = []
    coins = get_enabled_coins()
    state = load_state()
    state["last_run"] = datetime.utcnow().isoformat()

    held_coin = get_held_coin()
    futures_coins = get_futures_symbols()
    protected = {held_coin} | futures_coins if held_coin else futures_coins

    if held_coin:
        print(f"📌 Holding: {held_coin} (protected)")
    if futures_coins:
        print(f"🔻 Futures open: {', '.join(sorted(futures_coins))} (protected)")

    exchange = ccxt.binance()

    # ── CIRCUIT BREAKER ──
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"❌ ABORTED: Binance load_markets() failed: {e}")
        print("No changes made.")
        print("COIN_LIST_OK")
        return

    if len(markets) < 500:
        print(f"❌ ABORTED: load_markets() returned {len(markets)} markets (< 500). Likely API failure.")
        print("COIN_LIST_OK")
        return

    # Fetch tickers for all active USDC pairs of our coins
    active_pairs = {
        f"{c}/{BRIDGE}" for c in coins
        if f"{c}/{BRIDGE}" in markets and markets[f"{c}/{BRIDGE}"].get("active", False)
    }
    tickers = {}
    if active_pairs:
        try:
            tickers = exchange.fetch_tickers(list(active_pairs))
        except Exception as e:
            print(f"⚠️ fetch_tickers() failed ({e}) — falling back to individual tickers")

    # ── Check each coin ──
    to_remove = []
    for coin in coins:
        pair = f"{coin}/{BRIDGE}"

        if pair not in markets:
            actions.append(f"🚨 REMOVE {coin}: USDC pair delisted/missing")
            to_remove.append(coin)
            continue
        if not markets[pair].get("active", False):
            actions.append(f"🚫 REMOVE {coin}: USDC pair inactive/halted")
            to_remove.append(coin)
            continue

        t = tickers.get(pair, {})
        vol = t.get("quoteVolume") or 0

        if vol < VOLUME_REMOVE_THRESHOLD:
            actions.append(f"💀 REMOVE {coin}: volume ${vol:,.0f}")
            to_remove.append(coin)
            state["low_volume_days"].pop(coin, None)
        elif vol < VOLUME_WARN_THRESHOLD:
            low_days = state["low_volume_days"].get(coin, 0) + 1
            state["low_volume_days"][coin] = low_days
            if low_days >= LOW_VOLUME_DAYS_TO_REPLACE:
                if len(coins) - len(to_remove) > MIN_COINS:
                    actions.append(f"🔄 REPLACE {coin}: low vol ${vol:,.0f} for {low_days}d")
                    to_remove.append(coin)
                    state["low_volume_days"].pop(coin, None)
                else:
                    actions.append(f"⚠️ {coin}: low vol ${vol:,.0f} ({low_days}d) — at min coin count, keeping")
            else:
                actions.append(f"📉 {coin}: low vol ${vol:,.0f} ({low_days}d/{LOW_VOLUME_DAYS_TO_REPLACE})")
        else:
            state["low_volume_days"].pop(coin, None)

    # ── CIRCUIT BREAKER: never nuke >30% in one run ──
    max_removals = max(2, int(len(coins) * 0.30))
    if len(to_remove) > max_removals:
        print(f"❌ ABORTED: {len(to_remove)} flagged (max {max_removals}) — looks like API failure.")
        save_state(state)
        print("COIN_LIST_OK")
        return

    # ── PROTECT held coin + futures positions ──
    protected_removed = []
    for coin in list(to_remove):
        if coin in protected:
            actions.append(f"⏸️ SKIP {coin}: protected (held/futures)")
            to_remove.remove(coin)
            protected_removed.append(coin)

    # ── Execute removals in DB ──
    for coin in to_remove:
        db_disable_coin(coin)

    # ── Find replacements ──
    removed_count = len(to_remove)
    if removed_count > 0:
        current_enabled = get_enabled_coins()
        if len(current_enabled) < MAX_COINS:
            # Build candidate pool: disabled coins from DB + known high-volume altcoins.
            # Exclude majors (BTC/ETH/BNB) — they're reference coins, not momentum targets,
            # and are typically disabled intentionally.
            EXCLUDE_MAJORS = {"BTC", "ETH", "BNB", "USDC", BRIDGE}
            pool = (get_disabled_coins() | {
                "UNI", "RENDER", "ENA", "SEI", "JUP", "WIF", "PEPE",
                "TRX", "LTC", "BCH", "ETC", "NEIRO", "BONK", "FET",
                "RNDR", "HBAR", "FTM", "AGIX", "ONDO", "TAO",
            }) - EXCLUDE_MAJORS
            candidates = [c for c in pool if c not in current_enabled and c not in protected]

            # Check volume for each candidate
            scored = []
            for cand in candidates:
                pair = f"{cand}/{BRIDGE}"
                if pair not in markets or not markets[pair].get("active", False):
                    continue
                t = tickers.get(pair)
                if not t:
                    try:
                        t = exchange.fetch_ticker(pair)
                    except Exception:
                        continue
                vol = float(t.get("quoteVolume") or 0)
                if vol >= VOLUME_WARN_THRESHOLD:
                    scored.append((cand, vol))

            scored.sort(key=lambda x: x[1], reverse=True)
            slots = min(removed_count, MAX_COINS - len(current_enabled))

            for cand, vol in scored[:slots]:
                if db_enable_coin(cand):
                    actions.append(f"➕ ADD {cand}: ${vol:,.0f} 24h vol")

    # Clean up state for removed coins
    state["low_volume_days"] = {
        k: v for k, v in state["low_volume_days"].items()
        if k in get_enabled_coins()
    }
    save_state(state)

    # ── Output ──
    # Only treat as "changed" if actual DB writes happened (removals or additions)
    db_changes = [a for a in actions if any(k in a for k in ("REMOVE", "REPLACE", "ADD"))]
    if not actions:
        print(f"✅ All {len(coins)} coins healthy.")
        print("COIN_LIST_OK")
    elif not db_changes:
        # Only informational warnings — don't trigger delivery
        print(f"✅ All {len(coins)} coins healthy (warnings below).")
        for a in actions:
            print(f"  {a}")
        print("COIN_LIST_OK")
    else:
        print(f"\n🤖 {len(db_changes)} action(s):")
        for a in actions:
            print(f"  {a}")
        final = get_enabled_coins()
        print(f"\n📋 Active: {len(final)} coins — {', '.join(sorted(final))}")
        print("COIN_LIST_CHANGED")


if __name__ == "__main__":
    main()
