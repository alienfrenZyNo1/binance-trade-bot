#!/usr/bin/env python3
"""
Telegram companion bot for binance-trade-bot.
Listens for commands and reads directly from the trade bot's SQLite DB + Binance API.

Commands:
  /status   — Holdings, portfolio value (spot + futures)
  /trades   — Recent trade history (including FAILED)
  /coins    — Monitored coins (futures-eligible marked)
  /price    — Current coin live price + 24h stats
  /profit   — Performance dashboard & P&L
  /regime   — Market regime + what the bot is doing
  /futures  — Futures wallet balance, open shorts, P&L
  /health   — System health: DB, backups, bot process
  /config   — Current bot configuration
  /kill     — Emergency: close all futures + transfer back
  /hop      — Potential next trade targets with filters
  /addcoin  — Add a coin to monitored list
  /removecoin — Remove a coin
  /swap     — Swap one coin for another
  /help     — This message
"""

import os
import sys
import json
import time
import sqlite3
import logging
import hashlib
import hmac
import subprocess
import requests
from datetime import datetime
from urllib.parse import urlencode

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = set(
    int(x) for x in os.environ.get("TELEGRAM_CHAT_IDS", "REDACTED_CHAT_ID").split(",") if x.strip()
)
DB_PATH = os.environ.get("DB_PATH", "data/crypto_trading.db")
BRIDGE_SYMBOL = os.environ.get("BRIDGE_SYMBOL", "USDC")
API_BASE = f"https://api.binance.com/api/v3"
FAPI_BASE = f"https://fapi.binance.com/fapi/v2"
FAPI_PUB = f"https://fapi.binance.com/fapi/v1"  # public market data (fundingRate, premiumIndex, ping)
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# Coins with USDC-M perpetuals (matching futures_manager.py)
FUTURES_ELIGIBLE = {"SOL", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
                    "SUI", "TIA", "ENA"}

# Config file path
CONFIG_PATH = os.environ.get("CONFIG_PATH", "data/config/user.cfg")
if not os.path.exists(CONFIG_PATH):
    CONFIG_PATH = "user.cfg"

# Docker container name
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "binance-trade-bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - telegram-bot - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)

# ── DB Helpers ───────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_coin():
    """Get the most recent coin from current_coin_history."""
    conn = get_db()
    row = conn.execute(
        "SELECT coin_id FROM current_coin_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row["coin_id"] if row else "?"


def _sign_request(params):
    """Sign Binance API request."""
    if not BINANCE_API_SECRET:
        return params
    query = urlencode(params)
    signature = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    params["signature"] = signature
    return params


def _signed_get(url, params=None):
    """Signed GET request to Binance API."""
    params = params or {}
    params["recvWindow"] = 5000
    params["timestamp"] = int(time.time() * 1000)
    params = _sign_request(params)
    return requests.get(
        url, params=params,
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        timeout=10,
    )


# ── Spot API Helpers ─────────────────────────────────────────────────────────

def get_holdings():
    """Get LIVE balances from Binance spot API."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        log.warning("No Binance API keys, falling back to DB")
        return _get_holdings_from_db()

    try:
        r = _signed_get(f"{API_BASE}/account")
        if r.status_code != 200:
            log.error(f"Binance account API failed: {r.status_code} {r.text[:200]}")
            return _get_holdings_from_db()

        balances = r.json().get("balances", [])
        held = []
        for b in balances:
            asset = b["asset"]
            free = float(b["free"])
            locked = float(b["locked"])
            total = free + locked
            if total > 0.0001:
                held.append({"coin_id": asset, "balance": total, "free": free, "locked": locked})

        if not held:
            return []

        # Get prices for all held coins
        symbols = [h["coin_id"] for h in held]
        prices = {}
        try:
            r2 = requests.get(f"{API_BASE}/ticker/price", timeout=10)
            if r2.status_code == 200:
                price_map = {p["symbol"]: float(p["price"]) for p in r2.json()}
                for h in held:
                    coin = h["coin_id"]
                    if coin == BRIDGE_SYMBOL:
                        prices[coin] = 1.0
                    else:
                        pair = f"{coin}{BRIDGE_SYMBOL}"
                        prices[coin] = price_map.get(pair, 0.0)
                    h["usd_price"] = prices[coin]
        except Exception as e:
            log.warning(f"Failed to fetch prices: {e}")
            for h in held:
                h["usd_price"] = 0.0

        held.sort(key=lambda x: x["balance"] * x["usd_price"], reverse=True)
        return held

    except Exception as e:
        log.error(f"get_holdings failed: {e}")
        return _get_holdings_from_db()


def _get_holdings_from_db():
    """Fallback: Get latest balance snapshot from DB."""
    conn = get_db()
    rows = conn.execute(
        """SELECT coin_id, balance, usd_price, btc_price, datetime
           FROM coin_value
           WHERE id IN (
               SELECT MAX(id) FROM coin_value
               WHERE interval = 'MINUTELY'
               GROUP BY coin_id
           )
           ORDER BY usd_price * balance DESC"""
    ).fetchall()
    conn.close()
    return rows


def get_trade_history(limit=10):
    """Get recent trades (all states including FAILED)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT alt_coin_id, crypto_coin_id, selling, state,
                  alt_trade_amount, crypto_trade_amount, datetime
           FROM trade_history
           ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def get_coins():
    """Get list of enabled coins."""
    conn = get_db()
    rows = conn.execute(
        "SELECT symbol FROM coins WHERE enabled = 1 ORDER BY symbol"
    ).fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


def get_portfolio_value(holdings):
    """Calculate total USD value of holdings."""
    total = 0.0
    for h in holdings:
        if isinstance(h, dict):
            total += h["balance"] * h.get("usd_price", 0)
        else:
            total += (h["balance"] or 0) * (h["usd_price"] or 0)
    return total


def get_live_price(symbol):
    """Get live price from Binance."""
    try:
        pair = f"{symbol}{BRIDGE_SYMBOL}"
        r = requests.get(
            f"{API_BASE}/ticker/price",
            params={"symbol": pair},
            timeout=10,
        )
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception as e:
        log.warning(f"Failed to get live price for {symbol}: {e}")
    return None


def get_24h_stats(symbol):
    """Get 24h price change stats from Binance."""
    try:
        pair = f"{symbol}{BRIDGE_SYMBOL}"
        r = requests.get(
            f"{API_BASE}/ticker/24hr",
            params={"symbol": pair},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return {
                "price": float(data["lastPrice"]),
                "change_pct": float(data["priceChangePercent"]),
                "high": float(data["highPrice"]),
                "low": float(data["lowPrice"]),
                "volume": float(data["quoteVolume"]),
            }
    except Exception as e:
        log.warning(f"Failed to get 24h stats for {symbol}: {e}")
    return None


# ── Futures API Helpers ──────────────────────────────────────────────────────

def get_futures_balance():
    """Get USDC balance in futures wallet."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return None
    try:
        r = _signed_get(f"{FAPI_BASE}/balance")
        if r.status_code != 200:
            log.warning(f"Futures balance API: {r.status_code} {r.text[:150]}")
            return None
        for bal in r.json():
            if bal.get("asset") == BRIDGE_SYMBOL:
                # 'availableBalance' is unreliable (returns 0 with no positions)
                # Use 'maxWithdrawAmount' for the available figure
                return {
                    "balance": float(bal.get("balance", 0)),
                    "available": float(bal.get("maxWithdrawAmount", bal.get("balance", 0))),
                    "pnl": float(bal.get("crossUnPnl", 0)),
                }
        return {"balance": 0.0, "available": 0.0, "pnl": 0.0}
    except Exception as e:
        log.warning(f"get_futures_balance failed: {e}")
        return None


def get_futures_positions():
    """Get open futures positions (shorts have negative positionAmt)."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return []
    try:
        r = _signed_get(f"{FAPI_BASE}/positionRisk")
        if r.status_code != 200:
            log.warning(f"Futures positions API: {r.status_code}")
            return []
        positions = []
        for p in r.json():
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                entry = float(p.get("entryPrice", 0))
                mark = float(p.get("markPrice", 0))
                leverage = p.get("leverage", "?")
                direction = "SHORT" if amt < 0 else "LONG"
                qty = abs(amt)
                # PnL for shorts: (entry - mark) * qty
                if direction == "SHORT" and entry > 0:
                    pnl_pct = ((entry - mark) / entry) * 100
                elif direction == "LONG" and entry > 0:
                    pnl_pct = ((mark - entry) / entry) * 100
                else:
                    pnl_pct = 0.0
                un_pnl = float(p.get("unRealizedProfit", 0))
                positions.append({
                    "symbol": p["symbol"],
                    "direction": direction,
                    "qty": qty,
                    "entry": entry,
                    "mark": mark,
                    "leverage": leverage,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": un_pnl,
                })
        return positions
    except Exception as e:
        log.warning(f"get_futures_positions failed: {e}")
        return []


def get_futures_funding(symbol):
    """Get current funding rate for a futures symbol."""
    try:
        r = requests.get(
            f"{FAPI_PUB}/fundingRate",
            params={"symbol": symbol, "limit": 1},
            timeout=10,
        )
        if r.status_code == 200 and r.json():
            return float(r.json()[0].get("fundingRate", 0))
    except Exception:
        pass
    return None


def get_futures_mark_price(symbol):
    """Get mark price for a futures symbol."""
    try:
        r = requests.get(
            f"{FAPI_PUB}/premiumIndex",
            params={"symbol": symbol},
            timeout=10,
        )
        if r.status_code == 200:
            return float(r.json().get("markPrice", 0))
    except Exception:
        pass
    return None


# ── Config Reader ────────────────────────────────────────────────────────────

def load_config():
    """Parse key=value from user.cfg, skipping comments."""
    config = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except Exception as e:
        log.warning(f"Could not read config: {e}")
    return config


# ── Command Handlers ─────────────────────────────────────────────────────────

def cmd_status():
    """Current holdings, portfolio value (spot + futures), regime."""
    current_coin = get_current_coin()
    holdings = get_holdings()
    spot_value = get_portfolio_value(holdings)
    fut_balance = get_futures_balance()
    fut_positions = get_futures_positions()

    # Current regime
    conn = get_db()
    regime_row = conn.execute(
        "SELECT regime FROM market_regime_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    regime = regime_row["regime"] if regime_row else "?"

    fut_value = fut_balance["balance"] if fut_balance else 0
    total_value = spot_value + fut_value

    lines = [f"🤖 **Bot Status**\n"]
    lines.append(f"🧭 Regime: `{regime.upper()}`")

    # Regime-aware "Holding" line
    if regime == "bear" and fut_positions:
        pos_summary = " + ".join(
            f"{p['symbol'].replace('USDC','')} {p['direction']}" for p in fut_positions
        )
        lines.append(f"📌 Holding: `{pos_summary}` (futures)")
    elif regime == "bear":
        lines.append(f"📌 Holding: `Cash (awaiting short signal)`")
    else:
        lines.append(f"📌 Holding: `{current_coin}`")

    lines.append(f"💰 **Total: `${total_value:.2f}`**")
    lines.append(f"   Spot: `${spot_value:.2f}` | Futures: `${fut_value:.2f}`\n")

    # Spot holdings
    lines.append("**📦 Spot Holdings:**")
    for h in holdings:
        if isinstance(h, dict):
            coin = h["coin_id"]
            balance = h["balance"]
            price = h.get("usd_price", 0)
            value = balance * price
        else:
            coin = h["coin_id"]
            balance = h["balance"] or 0
            price = h["usd_price"] or 0
            value = balance * price
        if value > 0.01:
            lines.append(f"  `{coin}`: {balance:.4f} @ ${price:.4f} = `${value:.2f}`")

    # Futures positions
    if fut_positions:
        lines.append(f"\n**🔻 Futures Positions ({len(fut_positions)}):**")
        for p in fut_positions:
            pnl_emoji = "🟢" if p["pnl_usd"] >= 0 else "🔴"
            lines.append(
                f"  {pnl_emoji} `{p['symbol']}` {p['direction']} "
                f"qty={p['qty']} entry=${p['entry']:.4f} "
                f"mark=${p['mark']:.4f} "
                f"P&L={p['pnl_pct']:+.1f}% (${p['pnl_usd']:+.2f})"
            )

    if fut_balance and fut_balance["available"] > 0 and not fut_positions:
        lines.append(f"\n💤 Futures wallet: `${fut_balance['available']:.2f}` idle (no open positions)")

    return "\n".join(lines)


def cmd_trades():
    """Recent trade history including FAILED states + futures positions."""
    trades = get_trade_history(10)

    lines = ["📋 **Recent Trades**\n"]

    if not trades:
        lines.append("_No spot trades yet._")
    else:
        for t in trades:
            action = "Sold" if t["selling"] else "Bought"
            coin = t["alt_coin_id"]
            amount = t["alt_trade_amount"] or 0
            cost = t["crypto_trade_amount"] or 0
            dt = t["datetime"][:19] if t["datetime"] else "?"
            state = t["state"] if t["state"] else "?"

            if state == "COMPLETE":
                icon = "🔴" if t["selling"] else "🟢"
                lines.append(f"{icon} `{dt}` {action} {amount:.2f} {coin} ↔ {cost:.2f} {t['crypto_coin_id']}")
            elif state == "FAILED":
                lines.append(f"⚠️ `{dt}` FAILED {action} {coin} — stuck in partial state!")
            else:
                lines.append(f"❓ `{dt}` {state} {action} {amount:.2f} {coin}")

        # Count states
        conn = get_db()
        state_counts = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM trade_history GROUP BY state"
        ).fetchall()
        conn.close()
        if state_counts:
            summary_parts = [f"{r['state']}: {r['cnt']}" for r in state_counts]
            lines.append(f"\n📊 Spot trades: `{' | '.join(summary_parts)}`")

    # ── Futures context ──
    positions = get_futures_positions()
    lines.append(f"\n{'─' * 20}")
    lines.append("**🔻 Futures Positions**")

    if positions:
        for p in positions:
            pnl_emoji = "🟢" if p["pnl_usd"] >= 0 else "🔴"
            funding = get_futures_funding(p["symbol"])
            funding_str = ""
            if funding is not None:
                f_emoji = "🟢" if funding < 0 else "🔴"
                funding_str = f" | Funding: {f_emoji}{funding*100:.4f}%"
            lines.append(
                f"  {pnl_emoji} `{p['symbol']}` {p['direction']} "
                f"qty={p['qty']} entry=`${p['entry']:.4f}` "
                f"mark=`${p['mark']:.4f}` "
                f"P&L={p['pnl_pct']:+.1f}% (`${p['pnl_usd']:+.2f}`)"
                f"{funding_str}"
            )
    else:
        lines.append("  💤 No open futures positions")

    return "\n".join(lines)


def cmd_coins():
    """List monitored coins with futures eligibility."""
    coins = get_coins()
    # Regime-aware: show futures position if in BEAR, not stale spot coin
    positions = get_futures_positions()
    if positions:
        current = f"{positions[0]['symbol'].replace(BRIDGE_SYMBOL, '')} (SHORT)"
    else:
        current = get_current_coin()

    fut_coins = [c for c in coins if c in FUTURES_ELIGIBLE]
    spot_only = [c for c in coins if c not in FUTURES_ELIGIBLE]

    lines = [f"👁 **Monitored Coins** ({len(coins)} total)\n"]
    lines.append(f"Bridge: `{BRIDGE_SYMBOL}`")
    lines.append(f"Current: `{current}`\n")

    lines.append(f"**🔻 Futures-eligible** ({len(fut_coins)}):")
    for i in range(0, len(fut_coins), 5):
        batch = fut_coins[i:i+5]
        row = "  ".join(f"`{c}`" for c in batch)
        lines.append(f"  {row}")

    if spot_only:
        lines.append(f"\n**📦 Spot-only** ({len(spot_only)}):")
        for i in range(0, len(spot_only), 5):
            batch = spot_only[i:i+5]
            row = "  ".join(f"`{c}`" for c in batch)
            lines.append(f"  {row}")

    return "\n".join(lines)


def cmd_price():
    """Live price of current coin + futures context if eligible."""
    # Regime-aware: in BEAR mode, show the shorted coin's price instead of stale spot coin
    positions = get_futures_positions()
    if positions:
        current_coin = positions[0]["symbol"].replace(BRIDGE_SYMBOL, "")
    else:
        current_coin = get_current_coin()
    stats = get_24h_stats(current_coin)

    if not stats:
        return f"❌ Could not fetch price for `{current_coin}`"

    change_emoji = "📈" if stats["change_pct"] >= 0 else "📉"
    lines = [f"💲 **{current_coin}/{BRIDGE_SYMBOL}**\n"]
    lines.append(f"Price: `${stats['price']:.6f}`")
    lines.append(f"24h: {change_emoji} {stats['change_pct']:+.2f}%")
    lines.append(f"High: `${stats['high']:.6f}`")
    lines.append(f"Low: `${stats['low']:.6f}`")
    lines.append(f"Volume: ${stats['volume']:,.0f}")

    # Futures context for eligible coins
    if current_coin in FUTURES_ELIGIBLE:
        fut_symbol = f"{current_coin}{BRIDGE_SYMBOL}"
        mark = get_futures_mark_price(fut_symbol)
        funding = get_futures_funding(fut_symbol)
        if mark is not None:
            basis_pct = ((mark - stats["price"]) / stats["price"]) * 100 if stats["price"] > 0 else 0
            lines.append(f"\n**🔻 Futures:**")
            lines.append(f"Mark: `${mark:.6f}`")
            lines.append(f"Basis: {basis_pct:+.3f}% (spot vs mark)")
            if funding is not None:
                f_emoji = "🟢 shorts get paid" if funding < 0 else "🔴 shorts pay"
                lines.append(f"Funding: {funding*100:.4f}% ({f_emoji})")
        else:
            lines.append(f"\n🔻 Futures eligible (no mark price data)")

    return "\n".join(lines)


def _verify_usdc_pair(coin):
    """Verify a coin has an active USDC pair on Binance."""
    try:
        r = requests.get(f"{API_BASE}/ticker/price", params={"symbol": f"{coin}{BRIDGE_SYMBOL}"}, timeout=10)
        if r.status_code == 200:
            return float(r.json()["price"]), None
        return None, f"No {coin}{BRIDGE_SYMBOL} pair on Binance (status {r.status_code})"
    except Exception as e:
        return None, str(e)


def _enable_coin(symbol):
    """Enable a coin in the DB + create pairs with all other enabled coins."""
    symbol = symbol.strip().upper()
    conn = get_db()

    row = conn.execute("SELECT symbol, enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if row and row["enabled"]:
        conn.close()
        return f"`{symbol}` is already in the active list."

    if row:
        conn.execute("UPDATE coins SET enabled = 1 WHERE symbol = ?", (symbol,))
        conn.commit()
    else:
        conn.execute("INSERT OR IGNORE INTO coins (symbol, enabled) VALUES (?, 1)", (symbol,))
        conn.commit()

    enabled = [r[0] for r in conn.execute("SELECT symbol FROM coins WHERE enabled = 1 AND symbol != ?", (symbol,)).fetchall()]
    for other in enabled:
        for a, b in [(symbol, other), (other, symbol)]:
            exists = conn.execute("SELECT id FROM pairs WHERE from_coin_id = ? AND to_coin_id = ?", (a, b)).fetchone()
            if not exists:
                conn.execute("INSERT INTO pairs (from_coin_id, to_coin_id, ratio) VALUES (?, ?, 1.0)", (a, b))
    conn.commit()
    conn.close()
    return f"✅ Added `{symbol}` — trade bot will pick it up in ~3 seconds."


def _disable_coin(symbol):
    """Disable a coin in the DB."""
    symbol = symbol.strip().upper()
    conn = get_db()

    row = conn.execute("SELECT symbol, enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        conn.close()
        return f"`{symbol}` is not in the database."

    if not row["enabled"]:
        conn.close()
        return f"`{symbol}` is already disabled."

    current = get_current_coin()
    if current == symbol:
        conn.close()
        return f"⚠️ Cannot remove `{symbol}` — it's the coin the bot is currently holding!"

    conn.execute("UPDATE coins SET enabled = 0 WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    return f"❌ Removed `{symbol}` — trade bot will stop scouting it in ~3 seconds."


def cmd_addcoin(args):
    """Add a coin to the monitored list."""
    if not args:
        return "Usage: `/addcoin TICKER`\nExample: `/addcoin LTC`"
    symbol = args.strip().upper()

    price, err = _verify_usdc_pair(symbol)
    if err:
        return f"❌ Cannot add `{symbol}`: {err}"

    vol_info = ""
    try:
        r = requests.get(f"{API_BASE}/ticker/24hr", params={"symbol": f"{symbol}{BRIDGE_SYMBOL}"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            vol = float(d["quoteVolume"])
            vol_info = f"\n📊 24h volume: ${vol:,.0f}"
            if vol < 500000:
                vol_info += "\n⚠️ Low volume — trades may have wide spreads"
    except Exception:
        pass

    result = _enable_coin(symbol)
    fut_note = " 🔻 Futures-eligible" if symbol in FUTURES_ELIGIBLE else ""
    return f"{result}{vol_info}\n💰 Price: ${price:.6f}{fut_note}"


def cmd_removecoin(args):
    """Remove a coin from the monitored list."""
    if not args:
        return "Usage: `/removecoin TICKER`\nExample: `/removecoin TIA`"
    return _disable_coin(args.strip().upper())


def cmd_swap(args):
    """Swap one coin for another."""
    if not args or " " not in args:
        return "Usage: `/swap OLD NEW`\nExample: `/swap TIA LTC`"
    parts = args.strip().upper().split()
    old, new = parts[0], parts[1]

    if old == new:
        return "Same coin, nothing to do."

    price, err = _verify_usdc_pair(new)
    if err:
        return f"❌ Cannot add `{new}`: {err}"

    result = []
    result.append(_disable_coin(old))
    result.append(_enable_coin(new))
    return "\n".join(result) + f"\n💰 `{new}` price: ${price:.6f}"


def cmd_futures():
    """Futures wallet status: balance, open positions, P&L, funding rates."""
    balance = get_futures_balance()
    positions = get_futures_positions()

    if balance is None and not positions:
        return "❌ Cannot reach futures API. Check API keys."

    lines = ["🔻 **Futures Dashboard**\n"]

    # Wallet balance
    if balance:
        lines.append(f"💼 **Wallet Balance:** `${balance['balance']:.2f}`")
        lines.append(f"   Available: `${balance['available']:.2f}`")
        if balance["pnl"] != 0:
            pnl_emoji = "🟢" if balance["pnl"] >= 0 else "🔴"
            lines.append(f"   Unrealized P&L: {pnl_emoji} `${balance['pnl']:+.2f}`")
        lines.append("")

    # Open positions
    if positions:
        lines.append(f"**📊 Open Positions ({len(positions)}):**\n")
        for p in positions:
            pnl_emoji = "🟢" if p["pnl_usd"] >= 0 else "🔴"
            funding = get_futures_funding(p["symbol"])
            funding_str = ""
            if funding is not None:
                if funding < 0:
                    funding_str = f" | Funding: 🟢 {funding*100:.4f}% (shorts get paid)"
                else:
                    funding_str = f" | Funding: 🔴 {funding*100:.4f}% (shorts pay)"

            lines.append(f"{pnl_emoji} **{p['symbol']}** — {p['direction']}")
            lines.append(f"   Qty: `{p['qty']}` | Leverage: `{p['leverage']}x`")
            lines.append(f"   Entry: `${p['entry']:.4f}` → Mark: `${p['mark']:.4f}`")
            lines.append(
                f"   P&L: **{p['pnl_pct']:+.1f}%** (${p['pnl_usd']:+.2f})"
                f"{funding_str}"
            )
            lines.append("")
    else:
        lines.append("💤 No open positions")

    # Quick check: what coins could be shorted right now?
    # Show worst performers among futures-eligible coins
    try:
        r = requests.get(f"{API_BASE}/ticker/24hr", timeout=10)
        if r.status_code == 200:
            performers = []
            for t in r.json():
                sym = t["symbol"]
                for coin in FUTURES_ELIGIBLE:
                    if sym == f"{coin}{BRIDGE_SYMBOL}":
                        performers.append((coin, float(t["priceChangePercent"])))
                        break
            performers.sort(key=lambda x: x[1])
            if performers:
                lines.append("**📉 Top short candidates** (24h perf):")
                for coin, perf in performers[:3]:
                    icon = "🔴" if perf < 0 else "🟢"
                    lines.append(f"  {icon} `{coin}`: {perf:+.2f}%")
    except Exception:
        pass

    return "\n".join(lines)


def cmd_health():
    """System health check: DB, bot container, backups, WAL mode."""
    lines = ["🏥 **System Health**\n"]

    # ── Database ──
    lines.append("**Database:**")
    db_ok = os.path.exists(DB_PATH)
    if db_ok:
        db_size = os.path.getsize(DB_PATH) / 1024
        lines.append(f"  ✅ DB exists ({db_size:.0f} KB)")

        # Check WAL mode
        try:
            conn = get_db()
            wal_row = conn.execute("PRAGMA journal_mode").fetchone()
            wal_mode = wal_row[0] if wal_row else "?"
            lines.append(f"  ✅ Journal mode: `{wal_mode}`")

            # DB backup check
            backup_dir = os.path.dirname(DB_PATH)
            backups = sorted(
                [f for f in os.listdir(backup_dir) if f.endswith(".db.bak")],
                reverse=True,
            ) if os.path.isdir(backup_dir) else []
            if backups:
                bak_path = os.path.join(backup_dir, backups[0])
                bak_age = time.time() - os.path.getmtime(bak_path)
                bak_age_str = f"{bak_age/3600:.1f}h ago" if bak_age < 86400 else f"{bak_age/86400:.1f}d ago"
                lines.append(f"  ✅ Last backup: `{backups[0]}` ({bak_age_str})")
            else:
                lines.append("  ⚠️ No DB backups found")

            # Row counts
            trade_count = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
            regime_count = conn.execute("SELECT COUNT(*) FROM market_regime_log").fetchone()[0]
            conn.close()
            lines.append(f"  📊 Trades: `{trade_count}` | Regime logs: `{regime_count}`")
        except Exception as e:
            lines.append(f"  ❌ DB error: {e}")
    else:
        lines.append(f"  ❌ DB not found at `{DB_PATH}`")

    # ── Bot Process ──
    lines.append("\n**Bot Process:**")
    bot_found = False
    try:
        # Strategy 1: Check for any Docker container running crypto_trading.py
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("|", 2)
            name = parts[0] if len(parts) > 0 else "?"
            status = parts[1] if len(parts) > 1 else "?"
            image = parts[2] if len(parts) > 2 else "?"
            # Match the trade bot by its Coolify image name or known patterns
            if os.environ.get("DOCKER_IMAGE", "") in image or CONTAINER_NAME in name or "binance" in name.lower():
                if "Up" in status:
                    # Extract uptime from status like "Up 5 minutes"
                    lines.append(f"  ✅ Running ({status.lower()})")
                    bot_found = True
                else:
                    lines.append(f"  ⚠️ Container status: {status}")
                    bot_found = True
                break
    except Exception:
        pass

    # Strategy 2: Check DB freshness — if last coin_value or regime log is recent, bot is alive
    if not bot_found:
        try:
            conn2 = get_db()
            # coin_value is written every minute, more reliable than regime log
            last_cv = conn2.execute(
                "SELECT datetime FROM coin_value WHERE interval = 'MINUTELY' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            check_dt = None
            source = ""
            if last_cv and last_cv["datetime"]:
                check_dt = last_cv["datetime"]
                source = "value snapshot"
            else:
                last_log = conn2.execute(
                    "SELECT datetime FROM market_regime_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if last_log and last_log["datetime"]:
                    check_dt = last_log["datetime"]
                    source = "regime log"
            conn2.close()

            if check_dt:
                log_dt = datetime.strptime(check_dt[:19], "%Y-%m-%d %H:%M:%S")
                age_sec = (datetime.now() - log_dt).total_seconds()
                if age_sec < 300:
                    lines.append(f"  ✅ Running (DB active, {source} {int(age_sec)}s ago)")
                    bot_found = True
                elif age_sec < 600:
                    lines.append(f"  ⚠️ Possibly stalled (last {source} {int(age_sec/60)}min ago)")
                    bot_found = True
        except Exception:
            pass

    if not bot_found:
        # Strategy 3: systemctl fallback
        try:
            result2 = subprocess.run(
                ["systemctl", "is-active", "binance-trade-bot"],
                capture_output=True, text=True, timeout=5,
            )
            status2 = result2.stdout.strip()
            if status2 == "active":
                lines.append("  ✅ systemd service: active")
            else:
                lines.append("  ❌ Trade bot NOT detected!")
        except Exception:
            lines.append("  ❌ Trade bot NOT detected (cannot check Docker or systemd)!")

    # ── PID Lock ──
    lines.append("\n**Instance Protection:**")
    pid_file = os.path.join(os.path.dirname(DB_PATH), "bot.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = f.read().strip()
            lines.append(f"  ✅ PID lock active (PID {pid})")
        except Exception:
            lines.append("  ⚠️ PID lock file exists but unreadable")
    else:
        lines.append("  ℹ️ No PID lock file (bot may use in-memory lock)")

    # ── API Connectivity ──
    lines.append("\n**Connectivity:**")
    try:
        r = requests.get(f"{API_BASE}/ping", timeout=5)
        if r.status_code == 200:
            lines.append("  ✅ Binance spot API: reachable")
        else:
            lines.append(f"  ⚠️ Binance spot API: status {r.status_code}")
    except Exception:
        lines.append("  ❌ Binance spot API: unreachable")

    try:
        r = requests.get(f"{FAPI_PUB}/ping", timeout=5)
        if r.status_code == 200:
            lines.append("  ✅ Binance futures API: reachable")
        else:
            lines.append(f"  ⚠️ Binance futures API: status {r.status_code}")
    except Exception:
        lines.append("  ❌ Binance futures API: unreachable")

    return "\n".join(lines)


def cmd_config():
    """Show current bot configuration."""
    config = load_config()

    if not config:
        return "❌ Could not read configuration file."

    # Friendly labels
    labels = {
        "bridge": "Bridge Currency",
        "scout_multiplier": "Scout Multiplier",
        "buy_timeout": "Buy Timeout (s)",
        "sell_timeout": "Sell Timeout (s)",
        "trailing_stop_enabled": "Trailing Stop",
        "trailing_stop_pct": "Trailing Stop %",
        "futures_enabled": "Futures Enabled",
        "futures_leverage": "Futures Leverage",
        "futures_max_margin_pct": "Futures Max Margin %",
        "futures_stop_loss_pct": "Futures Stop Loss %",
        "futures_trailing_stop_pct": "Futures Trailing Stop %",
        "futures_max_funding_rate": "Futures Max Funding Rate",
        "futures_check_interval": "Futures Check Interval (s)",
    }
    # Hide sensitive keys
    hidden = {"api_key", "api_secret_key", "key", "secret"}

    lines = ["⚙️ **Bot Configuration**\n"]

    # Trading settings
    lines.append("**Trading:**")
    for k in ["bridge", "scout_multiplier", "buy_timeout", "sell_timeout"]:
        if k in config:
            label = labels.get(k, k)
            lines.append(f"  `{label}`: `{config[k]}`")

    # Risk management
    lines.append("\n**Risk Management:**")
    for k in ["trailing_stop_enabled", "trailing_stop_pct"]:
        if k in config:
            label = labels.get(k, k)
            lines.append(f"  `{label}`: `{config[k]}`")

    # Futures settings
    futures_keys = [k for k in config if k.startswith("futures")]
    if futures_keys:
        lines.append("\n**🔻 Futures:**")
        for k in sorted(futures_keys):
            if k in hidden:
                continue
            label = labels.get(k, k.replace("futures_", "").replace("_", " ").title())
            val = config[k]
            # Convert fractions to percentages for readability
            if "margin_pct" in k:
                try:
                    val = f"{float(val)*100:.0f}%"
                except Exception:
                    pass
            if "funding_rate" in k:
                try:
                    val = f"{float(val)*100:.4f}%"
                except Exception:
                    pass
            lines.append(f"  `{label}`: `{val}`")

    # Coin count
    conn = get_db()
    enabled_count = conn.execute("SELECT COUNT(*) FROM coins WHERE enabled = 1").fetchone()[0]
    total_count = conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
    conn.close()
    lines.append(f"\n**Coins:** `{enabled_count}` active / `{total_count}` total")

    # Config file path
    lines.append(f"\n_file: `{CONFIG_PATH}`_")

    return "\n".join(lines)


def cmd_kill(args=None):
    """Emergency kill switch: close all futures positions + transfer back to spot.

    Usage: /kill confirm
    """
    if args and args.strip().lower() == "confirm":
        return _execute_kill()

    # Show what would happen and ask for confirmation
    positions = get_futures_positions()
    balance = get_futures_balance()

    lines = ["🚨 **EMERGENCY KILL SWITCH**\n"]
    lines.append("This will:")
    lines.append("  1. Close ALL open futures positions")
    lines.append("  2. Transfer all USDC back to spot wallet")
    lines.append("  3. Bot will NOT reopen futures until next bear regime cycle\n")

    if positions:
        lines.append(f"**{len(positions)} position(s) will be closed:**")
        for p in positions:
            lines.append(f"  `{p['symbol']}` {p['direction']} qty={p['qty']} P&L={p['pnl_pct']:+.1f}%")
    else:
        lines.append("No open positions to close.")

    if balance and balance["balance"] > 0:
        lines.append(f"\n`${balance['balance']:.2f}` will be transferred to spot.")

    lines.append("\n⚠️ **To execute, send:** `/kill confirm`")
    return "\n".join(lines)


def _execute_kill():
    """Execute the kill switch: close positions + transfer back."""
    lines = ["🚨 **KILL SWITCH EXECUTING...**\n"]

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return "❌ No API keys available. Cannot execute kill switch."

    positions = get_futures_positions()

    # Step 1: Close all positions
    if positions:
        for p in positions:
            try:
                close_side = "BUY" if p["direction"] == "SHORT" else "SELL"
                order_params = {
                    "symbol": p["symbol"],
                    "side": close_side,
                    "type": "MARKET",
                    "quantity": p["qty"],
                    "reduceOnly": "true",
                    "recvWindow": 5000,
                    "timestamp": int(time.time() * 1000),
                }
                order_params = _sign_request(order_params)
                r = requests.post(
                    f"{FAPI_BASE}/order",
                    params=order_params,
                    headers={"X-MBX-APIKEY": BINANCE_API_KEY},
                    timeout=10,
                )
                if r.status_code == 200:
                    lines.append(f"✅ Closed {p['symbol']} {p['direction']} (qty {p['qty']})")
                else:
                    lines.append(f"❌ Failed to close {p['symbol']}: {r.status_code} {r.text[:100]}")
            except Exception as e:
                lines.append(f"❌ Error closing {p['symbol']}: {e}")
    else:
        lines.append("✅ No open positions to close")

    # Step 2: Transfer all USDC back to spot
    time.sleep(2)  # Wait for positions to settle
    balance = get_futures_balance()
    if balance and balance["balance"] > 0.01:
        try:
            transfer_params = {
                "asset": BRIDGE_SYMBOL,
                "amount": f"{balance['balance']:.8f}".rstrip("0").rstrip("."),
                "type": 2,  # futures to spot
                "recvWindow": 5000,
                "timestamp": int(time.time() * 1000),
            }
            transfer_params = _sign_request(transfer_params)
            r = requests.post(
                "https://api.binance.com/sapi/v1/futures/transfer",
                params=transfer_params,
                headers={"X-MBX-APIKEY": BINANCE_API_KEY},
                timeout=10,
            )
            if r.status_code == 200:
                lines.append(f"✅ Transferred ${balance['balance']:.2f} {BRIDGE_SYMBOL} back to spot")
            else:
                lines.append(f"❌ Transfer failed: {r.status_code} {r.text[:100]}")
        except Exception as e:
            lines.append(f"❌ Transfer error: {e}")
    else:
        lines.append("✅ No USDC in futures wallet to transfer")

    lines.append("\n🏁 **Kill switch complete.** Bot is in spot-only mode.")
    lines.append("_The trade bot may re-enter futures on the next bear regime cycle._")
    return "\n".join(lines)


def cmd_regime():
    """Show current market regime and what the bot is doing about it."""
    conn = get_db()

    row = conn.execute(
        """SELECT regime, adx_value, avg_volatility, btc_correlation, datetime
           FROM market_regime_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()

    if not row:
        conn.close()
        try:
            r = requests.get(f"{API_BASE}/ticker/24hr", timeout=10)
            if r.status_code == 200:
                coins = conn.execute("SELECT symbol FROM coins WHERE enabled = 1").fetchall()
                total_vol = 0
                cnt = 0
                for c in coins:
                    pair = f"{c['symbol']}{BRIDGE_SYMBOL}"
                    for t in r.json():
                        if t["symbol"] == pair:
                            total_vol += abs(float(t["priceChangePercent"]))
                            cnt += 1
                            break
                avg_vol = total_vol / cnt if cnt > 0 else 0
                regime = "stormy" if avg_vol > 8 else "sideways"
                conn.close()
                return f"🧠 **Market Regime** (estimated)\n\nStatus: `{regime}`\nAvg volatility: `{avg_vol:.1f}%`\n\n_Bot is collecting data for full regime detection..._"
        except Exception:
            pass
        return "❌ No regime data yet. Bot needs a few minutes to classify the market."

    regime = row["regime"]
    adx = row["adx_value"] or 0
    vol = row["avg_volatility"] or 0

    emoji_map = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "stormy": "🟠"}
    strategy_map = {
        "bull": "🟢 **Bull** — Momentum mode\nBot is buying the strongest coins and riding trends. Spot long positions.",
        "bear": "🔴 **Bear** — Defense mode\nBot has sold to USDC and may be **shorting via USDC-M futures**. Capital is being preserved/shorted.",
        "sideways": "🟡 **Sideways** — Mean reversion mode\nBot is buying dips and selling rips on oscillating coins.",
        "stormy": "🟠 **Stormy** — Conservative mode\nBot uses double z-score thresholds. Only high-conviction trades.",
    }

    emoji = emoji_map.get(regime, "❓")
    strategy = strategy_map.get(regime, "Unknown regime")

    lines = [f"🧠 **Market Regime**\n"]
    lines.append(f"Status: {emoji} **{regime.upper()}**\n")
    lines.append(strategy)
    lines.append(f"\n📊 ADX: `{adx:.1f}` (>25 = trending)")

    # ADX interpretation
    if adx > 50:
        lines.append("   → 🔥 Very strong trend")
    elif adx > 25:
        lines.append("   → 📈 Trending")
    elif adx > 20:
        lines.append("   → 📉 Weak trend forming")
    else:
        lines.append("   → 😴 Range-bound / choppy")

    lines.append(f"📉 Avg volatility: `{vol:.1f}%`")

    if row["btc_correlation"] is not None:
        lines.append(f"🔗 BTC correlation: `{row['btc_correlation']:.2f}`")

    # How long in this regime
    regime_history = conn.execute(
        """SELECT regime, datetime FROM market_regime_log
           ORDER BY id DESC LIMIT 20"""
    ).fetchall()
    conn.close()

    if regime_history:
        current_since = None
        for r in regime_history:
            if r["regime"] == regime:
                current_since = r["datetime"]
            else:
                break
        if current_since:
            try:
                since_dt = datetime.strptime(current_since[:19], "%Y-%m-%d %H:%M:%S")
                duration = datetime.now() - since_dt
                hours = duration.total_seconds() / 3600
                dur_str = f"{hours:.1f}h" if hours < 48 else f"{hours/24:.1f}d"
                lines.append(f"\n⏱ In this regime for: `{dur_str}`")
            except Exception:
                lines.append(f"\nSince: `{str(current_since)[:19]}`")

    # ── Futures context during bear ──
    if regime == "bear":
        lines.append("\n**🔻 Bear Mode Active:**")
        fut_balance = get_futures_balance()
        positions = get_futures_positions()
        if fut_balance:
            lines.append(f"💼 Futures wallet: `${fut_balance['balance']:.2f}`")
        if positions:
            for p in positions:
                pnl_emoji = "🟢" if p["pnl_usd"] >= 0 else "🔴"
                lines.append(
                    f"  {pnl_emoji} Short `{p['symbol']}`: "
                    f"{p['pnl_pct']:+.1f}% (${p['pnl_usd']:+.2f})"
                )
        elif fut_balance and fut_balance["balance"] > 5:
            lines.append("  💤 Scouting for short entry...")
        else:
            lines.append("  💤 Waiting for USDC transfer to futures wallet...")

    # Regime distribution (last 20 samples)
    if len(regime_history) >= 2:
        from collections import Counter
        counts = Counter(r["regime"] for r in regime_history)
        total = sum(counts.values())
        lines.append(f"\n**Recent distribution:** (last {total} samples)")
        for r, c in counts.most_common():
            pct = c / total * 100
            lines.append(f"  {emoji_map.get(r, '❓')} {r}: {pct:.0f}%")

    return "\n".join(lines)


def cmd_profit():
    """Performance dashboard: clean P&L, position status, trade history."""

    # ── Gather data ──
    conn = get_db()

    total_deposited = 0.0
    try:
        for dr in conn.execute("SELECT amount FROM deposits").fetchall():
            total_deposited += dr["amount"] or 0
    except Exception:
        pass

    holdings = get_holdings()
    spot_value = get_portfolio_value(holdings)
    fut_balance = get_futures_balance()
    fut_wallet = fut_balance["balance"] if fut_balance else 0
    current_value = spot_value + fut_wallet

    positions = get_futures_positions()
    unrealized_pnl = sum(p["pnl_usd"] for p in positions) if positions else 0.0

    completed = conn.execute(
        "SELECT * FROM trade_history WHERE state = 'COMPLETE' ORDER BY id ASC"
    ).fetchall()
    failed_trades = conn.execute(
        "SELECT COUNT(*) as cnt FROM trade_history WHERE state = 'FAILED'"
    ).fetchone()["cnt"]

    # ── Round-trip hop analysis ──
    round_trips = []
    pending_sell = None
    for t in completed:
        if t["selling"]:
            pending_sell = t
        elif pending_sell:
            sold_usdc = float(pending_sell["crypto_trade_amount"] or 0)
            bought_usdc = float(t["crypto_trade_amount"] or 0)
            hop_pnl = bought_usdc - sold_usdc
            round_trips.append({
                "from_coin": pending_sell["alt_coin_id"],
                "to_coin": t["alt_coin_id"],
                "sold_usdc": sold_usdc,
                "bought_usdc": bought_usdc,
                "pnl": hop_pnl,
                "datetime": t["datetime"],
            })
            pending_sell = None

    # Flag phantom hops (deposit contamination)
    for rt in round_trips:
        sell = max(rt["sold_usdc"], 0.01)
        rt["phantom"] = abs(rt["bought_usdc"] - rt["sold_usdc"]) / sell > 0.25

    real_trips = [rt for rt in round_trips if not rt.get("phantom")]
    wins = sum(1 for rt in real_trips if rt["pnl"] > 0.01)
    losses = sum(1 for rt in real_trips if rt["pnl"] < -0.01)
    flat = sum(1 for rt in real_trips if abs(rt["pnl"]) <= 0.01)
    realized_from_hops = sum(rt["pnl"] for rt in real_trips)

    # ── Account-level P&L ──
    total_pnl = current_value - total_deposited
    pnl_pct = (total_pnl / total_deposited * 100) if total_deposited > 0 else 0

    # Uptime
    first_trade = conn.execute("SELECT MIN(datetime) as first_dt FROM trade_history").fetchone()
    if first_trade and first_trade["first_dt"]:
        start_dt = datetime.strptime(first_trade["first_dt"][:19], "%Y-%m-%d %H:%M:%S")
        uptime_hours = (datetime.now() - start_dt).total_seconds() / 3600
        uptime_str = f"{uptime_hours:.1f}h" if uptime_hours < 48 else f"{uptime_hours / 24:.1f}d"
    else:
        uptime_str = "?"

    conn.close()

    # ── Build output ──
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    lines = [f"📊 **Performance Report**\n"]

    # Section 1: Headline P&L
    lines.append(f"{pnl_emoji} **P&L: ${total_pnl:+.2f}** ({pnl_pct:+.1f}%)")
    lines.append(f"   In: `${total_deposited:.2f}` → Now: `${current_value:.2f}`")
    lines.append(f"   Spot: `${spot_value:.2f}` | Futures: `${fut_wallet:.2f}`")
    lines.append("")

    # Section 2: Open position
    if positions:
        lines.append("**🔻 Open Position**")
        for p in positions:
            emoji = "🟢" if p["pnl_usd"] >= 0 else "🔴"
            lines.append(
                f"   {emoji} `{p['symbol']}` {p['direction']} | {p['leverage']}x"
            )
            lines.append(
                f"   Entry: `${p['entry']:.4f}` → Mark: `${p['mark']:.4f}`"
            )
            lines.append(
                f"   Unrealized: `${p['pnl_usd']:+.2f}` ({p['pnl_pct']:+.1f}%)"
            )
        lines.append("")
    else:
        lines.append("**🔻 Futures:** 💤 No open positions\n")

    # Section 3: Trading stats
    lines.append("**📈 Trading**")
    lines.append(f"   `{uptime_str}` uptime | {len(real_trips)} hops")
    if failed_trades:
        lines.append(f"   ⚠️ {failed_trades} failed orders")

    total_decisions = wins + losses
    if total_decisions > 0:
        eff = wins / total_decisions * 100
    else:
        eff = 0
    lines.append(f"   {wins}W / {losses}L / {flat} flat → {eff:.0f}% efficiency")
    lines.append(f"   Realized from hops: `${realized_from_hops:+.2f}`")
    lines.append("")

    # Section 4: Hop history (compact)
    if round_trips:
        lines.append("**Hop History:**")
        for rt in round_trips[-8:]:
            if rt.get("phantom"):
                emoji = "💰"
                tag = " (deposit)"
            elif rt["pnl"] > 0.01:
                emoji = "🟢"
                tag = ""
            elif rt["pnl"] < -0.01:
                emoji = "🔴"
                tag = ""
            else:
                emoji = "⚪"
                tag = ""
            lines.append(
                f"   {emoji} `{rt['from_coin']}→{rt['to_coin']}` "
                f"`${rt['pnl']:+.2f}`{tag}"
            )
        if len(round_trips) > 8:
            lines.append(f"   _...+{len(round_trips) - 8} earlier_")

    return "\n".join(lines)


def cmd_hop():
    """Show potential next hops with full strategy filter breakdown."""
    # Regime-aware: in BEAR mode, skip spot hops (no spot position) and go straight to futures
    positions = get_futures_positions()
    if positions:
        # BEAR mode — just show futures short candidates
        open_short = positions[0]["symbol"].replace(BRIDGE_SYMBOL, "")
        lines = [f"🔻 **Short Candidates** (currently shorting `{open_short}`)\\n"]
        _append_futures_candidates(lines, positions)
        return "\\n".join(lines)

    current = get_current_coin()
    conn = get_db()

    ZSCORE_THRESHOLD = 1.5
    MOMENTUM_CRASH_THRESHOLD = 5.0
    VOLATILITY_REGIME_THRESHOLD = 8.0
    COOLDOWN_SECONDS = 300

    last_trade_row = conn.execute(
        "SELECT MAX(datetime) FROM trade_history WHERE state = 'COMPLETE'"
    ).fetchone()
    last_trade_time = last_trade_row[0] if last_trade_row else None
    cooldown_active = False
    cooldown_remaining = ""
    if last_trade_time:
        last_dt = datetime.strptime(last_trade_time[:19], "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.now() - last_dt).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            cooldown_active = True
            cooldown_remaining = f"{int(COOLDOWN_SECONDS - elapsed)}s"

    avg_volatility = 0.0
    vol_count = 0
    try:
        r = requests.get(f"{API_BASE}/ticker/24hr", timeout=10)
        if r.status_code == 200:
            coins_rows = conn.execute("SELECT symbol FROM coins WHERE enabled = 1").fetchall()
            coin_syms = {cr["symbol"] for cr in coins_rows}
            vol_map = {}
            for t in r.json():
                sym = t["symbol"]
                vol_map[sym] = float(t["priceChangePercent"])
            for sym in coin_syms:
                pair = f"{sym}{BRIDGE_SYMBOL}"
                if pair in vol_map:
                    avg_volatility += abs(vol_map[pair])
                    vol_count += 1
            if vol_count > 0:
                avg_volatility /= vol_count
    except Exception:
        pass

    regime = "stormy 🌩" if avg_volatility > VOLATILITY_REGIME_THRESHOLD else "normal ☀️"
    active_zscore_threshold = ZSCORE_THRESHOLD * 2 if avg_volatility > VOLATILITY_REGIME_THRESHOLD else ZSCORE_THRESHOLD

    rows = conn.execute(
        """SELECT p.id as pair_id, p.from_coin_id, p.to_coin_id, p.ratio as target_ratio,
                  sh.current_coin_price, sh.other_coin_price, sh.datetime
           FROM scout_history sh
           JOIN pairs p ON sh.pair_id = p.id
           JOIN coins c_to ON p.to_coin_id = c_to.symbol
           JOIN coins c_from ON p.from_coin_id = c_from.symbol
           WHERE sh.id IN (
               SELECT MAX(sh2.id) FROM scout_history sh2
               JOIN pairs p2 ON sh2.pair_id = p2.id
               JOIN coins cf ON p2.from_coin_id = cf.symbol
               JOIN coins ct ON p2.to_coin_id = ct.symbol
               WHERE cf.enabled = 1 AND ct.enabled = 1
               AND p2.from_coin_id = ?
               GROUP BY p2.id
           )
           AND p.from_coin_id = ?
           AND c_from.enabled = 1 AND c_to.enabled = 1
           ORDER BY sh.datetime DESC""",
        (current, current),
    ).fetchall()

    if not rows:
        conn.close()
        # Still show futures short candidates even without scout data
        lines = [f"⏳ No scout data yet for `{current}` — bot needs a few minutes to build ratios.\n"]
        lines.append("**🔻 Futures Short Candidates**\n")
        positions = get_futures_positions()
        has_open_short = any(p["direction"] == "SHORT" for p in positions)
        try:
            r = requests.get(f"{API_BASE}/ticker/24hr", timeout=10)
            if r.status_code == 200:
                short_candidates = []
                for t in r.json():
                    sym = t["symbol"]
                    for coin in FUTURES_ELIGIBLE:
                        if sym == f"{coin}{BRIDGE_SYMBOL}":
                            perf_pct = float(t["priceChangePercent"])
                            short_candidates.append({"coin": coin, "perf_pct": perf_pct, "fut_symbol": sym})
                            break
                falling = [c for c in short_candidates if c["perf_pct"] < 0]
                falling.sort(key=lambda x: x["perf_pct"])
                for c in falling[:5]:
                    funding = get_futures_funding(c["fut_symbol"])
                    mark = get_futures_mark_price(c["fut_symbol"])
                    c["funding"] = funding
                    c["mark_price"] = mark
                if has_open_short:
                    open_sym = next((p["symbol"] for p in positions if p["direction"] == "SHORT"), None)
                    lines.append(f"🔒 Currently shorting: `{open_sym}`\n")
                if falling:
                    lines.append(f"**📉 Falling coins** ({len(falling)} of {len(FUTURES_ELIGIBLE)}):")
                    for i, c in enumerate(falling[:5], 1):
                        icon = "🔴" if c["perf_pct"] < -3 else "🟠" if c["perf_pct"] < -1 else "🟡"
                        line = f"  {icon} #{i}: `{c['coin']}` {c['perf_pct']:+.2f}%"
                        if c["funding"] is not None:
                            f_emoji = "🟢" if c["funding"] < 0 else "🔴"
                            line += f" | Funding: {f_emoji}{c['funding']*100:.4f}%"
                        if c["mark_price"]:
                            line += f" | Mark: `${c['mark_price']:.4f}`"
                        lines.append(line)
                else:
                    lines.append("  🟢 No futures-eligible coins falling — no short candidates")
        except Exception:
            pass
        return "\n".join(lines)

    fee = 0.001
    multiplier = 3.0
    transaction_fee = fee + fee - fee * fee

    price_map = {}
    try:
        r = requests.get(f"{API_BASE}/ticker/price", timeout=10)
        if r.status_code == 200:
            price_map = {p["symbol"]: float(p["price"]) for p in r.json()}
    except Exception:
        pass

    candidates = []
    for r in rows:
        to_coin = r["to_coin_id"]
        pair_id = r["pair_id"]
        target = r["target_ratio"]
        cur_price = r["current_coin_price"]
        other_price = r["other_coin_price"]
        if not cur_price or not other_price or other_price == 0:
            continue
        current_ratio = cur_price / other_price
        score = (current_ratio - transaction_fee * multiplier * current_ratio) - target
        divergence_pct = ((current_ratio / target) - 1) * 100 if target > 0 else 0

        ps = conn.execute(
            "SELECT ema_ratio, std_ratio, sample_count FROM pair_stats WHERE pair_id = ?",
            (pair_id,),
        ).fetchone()
        zscore = None
        zscore_ok = None
        if ps and ps["std_ratio"] and ps["std_ratio"] > 0 and ps["sample_count"] and ps["sample_count"] >= 5:
            zscore = abs((current_ratio - ps["ema_ratio"]) / ps["std_ratio"])
            zscore_ok = zscore >= active_zscore_threshold

        momentum_ok = True
        target_pair = f"{to_coin}{BRIDGE_SYMBOL}"
        try:
            r24 = requests.get(
                f"{API_BASE}/ticker/24hr", params={"symbol": target_pair}, timeout=10
            )
            if r24.status_code == 200:
                price_change_pct = float(r24.json()["priceChangePercent"])
                if price_change_pct < -MOMENTUM_CRASH_THRESHOLD:
                    momentum_ok = False
        except Exception:
            pass

        score_ok = score > 0
        all_clear = score_ok and zscore_ok is True and momentum_ok and not cooldown_active

        candidates.append({
            "to": to_coin,
            "score": score,
            "divergence": divergence_pct,
            "zscore": zscore,
            "zscore_ok": zscore_ok,
            "momentum_ok": momentum_ok,
            "score_ok": score_ok,
            "all_clear": all_clear,
            "price": price_map.get(target_pair, 0),
            "futures": to_coin in FUTURES_ELIGIBLE,
        })

    conn.close()

    if not candidates:
        return f"❌ No viable pairs for `{current}`."

    candidates.sort(key=lambda x: x["score"], reverse=True)

    lines = [f"🚀 **Hops from `{current}`**\n"]
    lines.append(f"Market: `{regime}` (avg vol {avg_volatility:.1f}%)")

    cooldown_str = f"🔒 Cooldown active ({cooldown_remaining} left)" if cooldown_active else "✅ Cooldown clear"
    lines.append(cooldown_str)
    lines.append(f"Z-score threshold: `{active_zscore_threshold:.1f}` | Momentum guard: `skip if coin drops >{MOMENTUM_CRASH_THRESHOLD}%`\n")

    lines.append("Filter checklist per candidate:")
    lines.append("  ✅ = pass | ⏳ = building data | ❌ = blocked")
    lines.append("")

    for i, c in enumerate(candidates[:5], 1):
        price_str = f"${c['price']:.4f}" if c["price"] else "?"
        fut_badge = " 🔻" if c["futures"] else ""

        score_icon = "✅" if c["score_ok"] else "❌"
        score_detail = f"{c['score']:.6f}" if not c["score_ok"] else f"**{c['score']:.6f}**"
        lines.append(f"**#{i}: `{c['to']}`**{fut_badge} {price_str} | Divergence: {c['divergence']:+.2f}%")

        filters = f"  {score_icon} Score: {score_detail}"

        if c["zscore"] is not None:
            zs_icon = "✅" if c["zscore_ok"] else "❌"
            filters += f"\n  {zs_icon} Z-score: {c['zscore']:.1f} / {active_zscore_threshold:.1f} needed"
        else:
            filters += f"\n  ⏳ Z-score: collecting data..."

        mom_icon = "✅" if c["momentum_ok"] else "❌"
        mom_text = "stable" if c["momentum_ok"] else "CRASHING ⚠️"
        filters += f"\n  {mom_icon} Momentum: {mom_text}"

        if c["all_clear"]:
            filters += "\n  🟢 **TRADE READY**"
        elif cooldown_active and c["score_ok"] and c["zscore_ok"] is True and c["momentum_ok"]:
            filters += "\n  🟡 waiting on cooldown"
        else:
            filters += "\n  🔴 blocked"

        lines.append(filters)
        if i < 5:
            lines.append("")

    viable = [c for c in candidates if c["all_clear"]]
    close = [c for c in candidates if not c["all_clear"] and c["score_ok"]]
    if viable:
        best = viable[0]
        lines.append(f"\n🎯 **Next hop: `{best['to']}`** — all filters passed!")
    elif close:
        best = close[0]
        blocked = []
        if not best["zscore_ok"]:
            blocked.append(f"z-score ({best['zscore']:.1f} < {active_zscore_threshold:.1f})")
        if not best["momentum_ok"]:
            blocked.append("momentum crash")
        if cooldown_active:
            blocked.append("cooldown")
        lines.append(f"\n🎯 Closest: `{best['to']}` — blocked by: {', '.join(blocked)}")
    else:
        lines.append(f"\n⏸ Best: `{candidates[0]['to']}` — score needs {abs(candidates[0]['score']):.6f} more")

    # ── Futures Short Candidates ──
    positions = get_futures_positions()
    lines.append(f"\n{'─' * 20}")
    lines.append("**🔻 Futures Short Candidates**")
    _append_futures_candidates(lines, positions)

    return "\n".join(lines)


def _append_futures_candidates(lines, positions):
    """Append futures short candidates section to lines list."""
    has_open_short = any(p["direction"] == "SHORT" for p in positions)
    lines.append("")
    try:
        r = requests.get(f"{API_BASE}/ticker/24hr", timeout=10)
        if r.status_code == 200:
            short_candidates = []
            for t in r.json():
                sym = t["symbol"]
                for coin in FUTURES_ELIGIBLE:
                    if sym == f"{coin}{BRIDGE_SYMBOL}":
                        perf_pct = float(t["priceChangePercent"])
                        vol = float(t.get("quoteVolume", 0))
                        price = float(t.get("lastPrice", 0))
                        short_candidates.append({
                            "coin": coin,
                            "perf_pct": perf_pct,
                            "volume": vol,
                            "price": price,
                            "fut_symbol": sym,
                        })
                        break

            falling = [c for c in short_candidates if c["perf_pct"] < 0]
            falling.sort(key=lambda x: x["perf_pct"])

            for c in falling[:5]:
                funding = get_futures_funding(c["fut_symbol"])
                mark = get_futures_mark_price(c["fut_symbol"])
                c["funding"] = funding
                c["mark_price"] = mark

            if has_open_short:
                open_sym = next((p["symbol"] for p in positions if p["direction"] == "SHORT"), None)
                lines.append(f"🔒 Currently shorting: `{open_sym}`")
                lines.append("")

            if falling:
                lines.append(f"**📉 Falling coins** ({len(falling)} of {len(FUTURES_ELIGIBLE)} futures-eligible):")
                for i, c in enumerate(falling[:5], 1):
                    icon = "🔴" if c["perf_pct"] < -3 else "🟠" if c["perf_pct"] < -1 else "🟡"

                    is_shorted = any(
                        p["direction"] == "SHORT" and c["fut_symbol"] == p["symbol"]
                        for p in positions
                    )
                    badge = " 🔒 SHORTING" if is_shorted else ""

                    line = f"  {icon} #{i}: `{c['coin']}` {c['perf_pct']:+.2f}%{badge}"
                    if c["funding"] is not None:
                        f_emoji = "🟢" if c["funding"] < 0 else "🔴"
                        line += f" | Funding: {f_emoji}{c['funding']*100:.4f}%"
                    if c["mark_price"]:
                        line += f" | Mark: `${c['mark_price']:.4f}`"
                    lines.append(line)
            else:
                lines.append("  🟢 No futures-eligible coins are falling — no short candidates")
                lines.append(f"  (all {len(short_candidates)} futures-eligible coins are green)")
    except Exception:
        lines.append("  ❌ Could not fetch futures short candidates")


def cmd_deposit(args=""):
    """Record a deposit. Usage: /deposit <amount> [note]"""
    if not args or not args[0].strip():
        # Show current deposits
        conn = get_db()
        try:
            rows = conn.execute("SELECT id, amount, currency, source, note, datetime FROM deposits ORDER BY id ASC").fetchall()
        except Exception:
            return "❌ Deposits table not set up yet."
        conn.close()

        if not rows:
            return "📋 **No deposits recorded.**\n\nUse `/deposit <amount>` to record a top-up."

        total = sum(r["amount"] for r in rows)
        lines = [f"📋 **Deposits** (total: `${total:.2f}`)\n"]
        for r in rows:
            note = f" — {r['note']}" if r["note"] else ""
            lines.append(f"  💰 `${r['amount']:.2f}` {r['currency']} ({r['source']}){note} — `{r['datetime'][:16]}`")
        return "\n".join(lines)

    parts = args[0].strip().split(None, 1)
    try:
        amount = float(parts[0])
        if amount <= 0:
            return "❌ Amount must be positive."
    except ValueError:
        return "❌ Usage: `/deposit <amount> [note]`\nExample: `/deposit 50 topped up from main wallet`"

    note = parts[1] if len(parts) > 1 else ""

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO deposits (amount, currency, source, note) VALUES (?, 'USDC', 'telegram', ?)",
            (amount, note),
        )
        conn.commit()
        total = conn.execute("SELECT SUM(amount) FROM deposits").fetchone()[0]
        conn.close()
    except Exception as e:
        conn.close()
        return f"❌ Failed to record deposit: {e}"

    return f"✅ Deposited `${amount:.2f}` recorded (total: `${total:.2f}`)"


def cmd_help():
    """List available commands."""
    lines = ["🤖 **Available Commands**\n"]

    lines.append("**📊 Monitoring:**")
    lines.append("  /status — Holdings & total portfolio (spot + futures)")
    lines.append("  /trades — Recent trades (incl. FAILED)")
    lines.append("  /price — Current coin live price + 24h stats")
    lines.append("  /hop — Potential next trade targets & filters")
    lines.append("  /profit — P&L, win rate, fees, trade breakdown")

    lines.append("\n**🧠 Market:**")
    lines.append("  /regime — Market regime & what the bot is doing")
    lines.append("  /coins — Monitored coins (futures-eligible marked)")

    lines.append("\n**🔻 Futures:**")
    lines.append("  /futures — Futures wallet, positions, P&L, funding")
    lines.append("  /kill — ⚠️ Emergency: close all shorts + transfer back")

    lines.append("\n**🔧 System:**")
    lines.append("  /health — DB, backups, container, API connectivity")
    lines.append("  /config — Current bot configuration & settings")
    lines.append("  /deposit — Record a top-up (`/deposit <amount> [note]`)")

    lines.append("\n**⚙️ Coin Management:**")
    lines.append("  /addcoin TICKER — Add a coin")
    lines.append("  /removecoin TICKER — Remove a coin")
    lines.append("  /swap OLD NEW — Replace one coin with another")

    lines.append("\n  /help — This message")

    return "\n".join(lines)


# ── Telegram Bot Loop ────────────────────────────────────────────────────────
# Commands that take arguments
ARG_COMMANDS = {
    "/addcoin": cmd_addcoin,
    "/removecoin": cmd_removecoin,
    "/swap": cmd_swap,
    "/kill": cmd_kill,
    "/deposit": cmd_deposit,
}
# Commands without arguments
COMMANDS = {
    "/start": cmd_help,
    "/help": cmd_help,
    "/status": cmd_status,
    "/trades": cmd_trades,
    "/coins": cmd_coins,
    "/price": cmd_price,
    "/profit": cmd_profit,
    "/regime": cmd_regime,
    "/futures": cmd_futures,
    "/health": cmd_health,
    "/config": cmd_config,
    "/hop": cmd_hop,
}


def send_message(chat_id, text):
    """Send a message via Telegram Bot API."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error(f"sendMessage failed: {r.status_code} {r.text[:200]}")
            # Retry without markdown
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text.replace("`", "").replace("*", "")},
                timeout=15,
            )
    except Exception as e:
        log.error(f"sendMessage exception: {e}")


def poll():
    """Long-poll Telegram for updates.

    Handles 409 Conflict (another instance polling the same token) by
    retrying with backoff instead of spamming errors.
    """
    offset = 0
    consecutive_409s = 0
    log.info("Telegram bot polling started")

    # Use a dedicated session for polling
    session = requests.Session()

    while True:
        try:
            params = {"timeout": 30, "offset": offset}
            r = session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params,
                timeout=35,
            )

            if r.status_code == 409:
                # Another service is polling the same bot token.
                # Retry with increasing backoff instead of error-spamming.
                consecutive_409s += 1
                if consecutive_409s == 1:
                    log.warning(
                        "getUpdates 409: another instance is polling this bot token. "
                        "Retrying with backoff..."
                    )
                backoff = min(5 * consecutive_409s, 30)
                time.sleep(backoff)
                continue

            if r.status_code != 200:
                log.error(f"getUpdates failed: {r.status_code}")
                consecutive_409s = 0
                time.sleep(5)
                continue

            # Success — reset counter
            if consecutive_409s > 0:
                log.info(f"getUpdates recovered after {consecutive_409s} conflicts")
            consecutive_409s = 0

            data = r.json()
            if not data.get("ok"):
                log.error(f"getUpdates not ok: {data}")
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                msg = update.get("message")
                if not msg:
                    continue

                chat_id = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()

                # Auth check
                if chat_id not in ALLOWED_CHAT_IDS:
                    log.warning(f"Unauthorized chat_id: {chat_id}")
                    send_message(chat_id, "⛔ Unauthorized. This bot is private.")
                    continue

                # Parse command
                parts = text.strip().split(None, 1)
                cmd = parts[0].lower() if parts else ""
                args = parts[1] if len(parts) > 1 else ""

                handler = ARG_COMMANDS.get(cmd)
                if handler:
                    log.info(f"Command '{cmd}' from chat {chat_id} args='{args}'")
                    response = handler(args)
                    send_message(chat_id, response)
                else:
                    handler = COMMANDS.get(cmd)
                    if handler:
                        log.info(f"Command '{cmd}' from chat {chat_id}")
                        response = handler()
                        send_message(chat_id, response)
                    elif text:
                        send_message(
                            chat_id,
                            f"Unknown command. Send /help for available commands.",
                        )

        except requests.exceptions.Timeout:
            continue  # Normal for long polling
        except Exception as e:
            log.error(f"Poll loop error: {e}")
            time.sleep(5)


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set!")
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        log.error(f"DB not found at {DB_PATH}")
        sys.exit(1)

    # Set bot commands menu
    try:
        commands_payload = [
            {"command": "status", "description": "Holdings & total portfolio"},
            {"command": "trades", "description": "Recent trades (incl. FAILED)"},
            {"command": "coins", "description": "Monitored coins"},
            {"command": "price", "description": "Current coin live price"},
            {"command": "profit", "description": "Performance dashboard & P&L"},
            {"command": "regime", "description": "Market regime & strategy mode"},
            {"command": "futures", "description": "Futures wallet, positions, P&L"},
            {"command": "health", "description": "System health check"},
            {"command": "config", "description": "Bot configuration"},
            {"command": "hop", "description": "Show potential next trade"},
            {"command": "kill", "description": "⚠️ Emergency: close all futures"},
            {"command": "addcoin", "description": "Add a coin to trade list"},
            {"command": "removecoin", "description": "Remove a coin from list"},
            {"command": "swap", "description": "Swap one coin for another"},
            {"command": "deposit", "description": "Record a deposit/top-up"},
            {"command": "help", "description": "Available commands"},
        ]
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
            json={"commands": commands_payload},
            timeout=10,
        )
        if r.status_code == 200:
            log.info("Bot command menu registered")
        else:
            log.warning(f"setMyCommands failed: {r.status_code}")
    except Exception as e:
        log.warning(f"Could not set commands: {e}")

    log.info(f"Bot starting | DB: {DB_PATH} | Chat IDs: {ALLOWED_CHAT_IDS}")
    poll()


if __name__ == "__main__":
    main()
