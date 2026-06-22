#!/usr/bin/env python3
"""
Telegram companion bot for binance-trade-bot.
Listens for /status, /trades, /coins, /price, /help commands.
Reads directly from the trade bot's SQLite DB.
"""

import os
import sys
import json
import time
import sqlite3
import logging
import hashlib
import hmac
import requests
from datetime import datetime
from urllib.parse import urlencode

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = set(
    int(x) for x in os.environ.get("TELEGRAM_CHAT_IDS", "REDACTED_CHAT_ID").split(",") if x.strip()
)
DB_PATH = os.environ.get("DB_PATH", "REDACTED/crypto_trading.db")
BRIDGE_SYMBOL = os.environ.get("BRIDGE_SYMBOL", "USDC")
API_BASE = f"https://api.binance.com/api/v3"
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

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


def get_holdings():
    """Get LIVE balances from Binance API."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        log.warning("No Binance API keys, falling back to DB")
        return _get_holdings_from_db()

    try:
        params = {"recvWindow": 5000, "timestamp": int(time.time() * 1000)}
        params = _sign_request(params)
        r = requests.get(
            f"{API_BASE}/account",
            params=params,
            headers={"X-MBX-APIKEY": BINANCE_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"Binance account API failed: {r.status_code} {r.text[:200]}")
            return _get_holdings_from_db()

        balances = r.json().get("balances", [])
        # Filter out dust (< $0.01) and get prices for non-zero balances
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
        # Build a list of symbols to fetch
        symbols = [h["coin_id"] for h in held]
        prices = {}

        # Fetch all prices at once
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

        # Sort by value descending
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
    """Get completed trades."""
    conn = get_db()
    rows = conn.execute(
        """SELECT alt_coin_id, crypto_coin_id, selling, state,
                  alt_trade_amount, crypto_trade_amount, datetime
           FROM trade_history
           WHERE state = 'COMPLETE'
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
        total += h["balance"] * h["usd_price"]
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


# ── Command Handlers ─────────────────────────────────────────────────────────
def cmd_status():
    """Current holdings and portfolio value."""
    current_coin = get_current_coin()
    holdings = get_holdings()
    total_value = get_portfolio_value(holdings)

    lines = [f"🤖 **Bot Status**\n"]
    lines.append(f"📌 Current coin: `{current_coin}`")
    lines.append(f"💰 Portfolio value: `${total_value:.2f}`\n")
    lines.append("**Holdings:**")

    for h in holdings:
        coin = h["coin_id"]
        balance = h["balance"]
        price = h["usd_price"]
        value = balance * price
        if value > 0.001:  # Skip dust
            lines.append(f"  `{coin}`: {balance:.4f} @ ${price:.4f} = `${value:.2f}`")

    return "\n".join(lines)


def cmd_trades():
    """Recent trade history."""
    trades = get_trade_history(10)

    if not trades:
        return "📋 **No completed trades yet.**"

    lines = ["📋 **Recent Trades**\n"]

    for t in trades:
        action = "Sold" if t["selling"] else "Bought"
        coin = t["alt_coin_id"]
        amount = t["alt_trade_amount"] or 0
        cost = t["crypto_trade_amount"] or 0
        dt = t["datetime"][:19]

        if t["selling"]:
            lines.append(f"🔴 `{dt}` Sold {amount:.2f} {coin} → {cost:.2f} {t['crypto_coin_id']}")
        else:
            lines.append(f"🟢 `{dt}` Bought {amount:.2f} {coin} ← {cost:.2f} {t['crypto_coin_id']}")

    return "\n".join(lines)


def cmd_coins():
    """List monitored coins."""
    coins = get_coins()
    current = get_current_coin()

    lines = [f"👁 **Monitored Coins** ({len(coins)} total)\n"]
    lines.append(f"Bridge: `{BRIDGE_SYMBOL}`")
    lines.append(f"Current: `{current}`\n")

    # Group into rows of 5
    for i in range(0, len(coins), 5):
        batch = coins[i : i + 5]
        row = "  ".join(f"`{c}`" for c in batch)
        lines.append(row)

    return "\n".join(lines)


def cmd_price():
    """Live price of current coin."""
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

    # Check if already enabled
    row = conn.execute("SELECT symbol, enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if row and row["enabled"]:
        conn.close()
        return f"`{symbol}` is already in the active list."

    # Check if coin exists but disabled
    if row:
        conn.execute("UPDATE coins SET enabled = 1 WHERE symbol = ?", (symbol,))
        conn.commit()
    else:
        conn.execute("INSERT OR IGNORE INTO coins (symbol, enabled) VALUES (?, 1)", (symbol,))
        conn.commit()

    # Create pairs with all other enabled coins
    enabled = [r[0] for r in conn.execute("SELECT symbol FROM coins WHERE enabled = 1 AND symbol != ?", (symbol,)).fetchall()]
    for other in enabled:
        # both directions
        for a, b in [(symbol, other), (other, symbol)]:
            exists = conn.execute("SELECT id FROM pairs WHERE from_coin_id = ? AND to_coin_id = ?", (a, b)).fetchone()
            if not exists:
                conn.execute("INSERT INTO pairs (from_coin_id, to_coin_id, ratio) VALUES (?, ?, 1.0)", (a, b))
    conn.commit()
    conn.close()
    return f"✅ Added `{symbol}` — trade bot will pick it up in ~3 seconds."


def _disable_coin(symbol):
    """Disable a coin in the DB. Will not remove if it's the current coin being held."""
    symbol = symbol.strip().upper()
    conn = get_db()

    row = conn.execute("SELECT symbol, enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        conn.close()
        return f"`{symbol}` is not in the database."

    if not row["enabled"]:
        conn.close()
        return f"`{symbol}` is already disabled."

    # Check if it's the current coin
    current = get_current_coin()
    if current == symbol:
        conn.close()
        return f"⚠️ Cannot remove `{symbol}` — it's the coin the bot is currently holding! Wait for it to trade away first."

    conn.execute("UPDATE coins SET enabled = 0 WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    return f"❌ Removed `{symbol}` — trade bot will stop scouting it in ~3 seconds."


def cmd_addcoin(args):
    """Add a coin to the monitored list. Validates USDC pair first."""
    if not args:
        return "Usage: `/addcoin TICKER`\nExample: `/addcoin LTC`"
    symbol = args.strip().upper()

    # Verify USDC pair exists
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
    return f"{result}{vol_info}\n💰 Price: ${price:.6f}"


def cmd_removecoin(args):
    """Remove a coin from the monitored list."""
    if not args:
        return "Usage: `/removecoin TICKER`\nExample: `/removecoin TIA`"
    return _disable_coin(args.strip().upper())


def cmd_swap(args):
    """Swap one coin for another in one command."""
    if not args or " " not in args:
        return "Usage: `/swap OLD NEW`\nExample: `/swap TIA LTC`"
    parts = args.strip().upper().split()
    old, new = parts[0], parts[1]

    if old == new:
        return "Same coin, nothing to do."

    # Verify new coin has USDC pair
    price, err = _verify_usdc_pair(new)
    if err:
        return f"❌ Cannot add `{new}`: {err}"

    result = []
    result.append(_disable_coin(old))
    result.append(_enable_coin(new))
    return "\n".join(result) + f"\n💰 `{new}` price: ${price:.6f}"


def cmd_hop():
    """Show potential next hops with full strategy filter breakdown."""
    current = get_current_coin()
    conn = get_db()

    # ── Strategy config (matching the improved strategy) ──
    ZSCORE_THRESHOLD = 1.5
    MOMENTUM_CRASH_THRESHOLD = 5.0  # % drop in last hour = skip
    VOLATILITY_REGIME_THRESHOLD = 8.0  # avg market vol % → double z-score
    COOLDOWN_SECONDS = 300  # 5 min

    # ── Fetch last trade time (for cooldown) ──
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

    # ── Market regime (avg 24h volatility across enabled coins) ──
    avg_volatility = 0.0
    vol_count = 0
    try:
        r = requests.get(f"{API_BASE}/ticker/24hr", timeout=10)
        if r.status_code == 200:
            coins_rows = conn.execute("SELECT symbol FROM coins WHERE enabled = 1").fetchall()
            coin_syms = {cr["symbol"] for cr in coins_rows}
            price_map = {}
            vol_map = {}
            for t in r.json():
                sym = t["symbol"]
                price_map[sym] = float(t["lastPrice"])
                vol_map[sym] = float(t["priceChangePercent"])
            # Average % change across all enabled coins
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

    # ── Get latest scout per pair for current coin ──
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
        return f"❌ No scout data yet for `{current}`. The bot needs a few minutes to build up ratios."

    fee = 0.001
    multiplier = 3.0
    transaction_fee = fee + fee - fee * fee

    # ── Get live prices + 1h-ago prices for momentum ──
    price_map = {}
    one_hour_ago_prices = {}
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

        # ── Z-score from pair_stats ──
        ps = conn.execute(
            "SELECT ema_ratio, std_ratio, sample_count FROM pair_stats WHERE pair_id = ?",
            (pair_id,),
        ).fetchone()
        zscore = None
        zscore_ok = None  # None = not enough data yet
        if ps and ps["std_ratio"] and ps["std_ratio"] > 0 and ps["sample_count"] and ps["sample_count"] >= 5:
            zscore = abs((current_ratio - ps["ema_ratio"]) / ps["std_ratio"])
            zscore_ok = zscore >= active_zscore_threshold

        # ── Momentum: check if target coin is crashing (>5% drop in last hour) ──
        momentum_ok = True  # assume ok
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

        # ── Would the bot actually trade? ──
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
        })

    conn.close()

    if not candidates:
        return f"❌ No viable pairs for `{current}`."

    candidates.sort(key=lambda x: x["score"], reverse=True)

    # ── Header ──
    lines = [f"🚀 **Hops from `{current}`**\n"]
    lines.append(f"Market: `{regime}` (avg vol {avg_volatility:.1f}%)")

    cooldown_str = f"🔒 Cooldown active ({cooldown_remaining} left)" if cooldown_active else "✅ Cooldown clear"
    lines.append(f"{cooldown_str}")
    lines.append(f"Z-score threshold: `{active_zscore_threshold:.1f}` | Momentum guard: `skip if coin drops >{MOMENTUM_CRASH_THRESHOLD}%`\\n")

    # ── Legend ──
    lines.append("Filter checklist per candidate:")
    lines.append("  ✅ = pass | ⏳ = building data | ❌ = blocked")
    lines.append("")

    # ── Show top 5 ──
    for i, c in enumerate(candidates[:5], 1):
        price_str = f"${c['price']:.4f}" if c["price"] else "?"

        # Score line
        score_icon = "✅" if c["score_ok"] else "❌"
        score_detail = f"{c['score']:.6f}" if not c["score_ok"] else f"**{c['score']:.6f}**"
        lines.append(f"**#{i}: `{c['to']}`** {price_str} | Divergence: {c['divergence']:+.2f}%")

        # Filters
        filters = f"  {score_icon} Score: {score_detail}"

        # Z-score
        if c["zscore"] is not None:
            zs_icon = "✅" if c["zscore_ok"] else "❌"
            filters += f"\n  {zs_icon} Z-score: {c['zscore']:.1f} / {active_zscore_threshold:.1f} needed"
        else:
            filters += f"\n  ⏳ Z-score: collecting data..."

        # Momentum
        mom_icon = "✅" if c["momentum_ok"] else "❌"
        mom_text = "stable" if c["momentum_ok"] else "CRASHING ⚠️"
        filters += f"\n  {mom_icon} Momentum: {mom_text}"

        # Verdict
        if c["all_clear"]:
            filters += "\n  🟢 **TRADE READY**"
        elif cooldown_active and c["score_ok"] and c["zscore_ok"] is True and c["momentum_ok"]:
            filters += "\n  🟡 waiting on cooldown"
        else:
            filters += "\n  🔴 blocked"

        lines.append(filters)
        if i < 5:
            lines.append("")

    # ── Summary ──
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
        lines.append(f"\n⏸ Closest: `{best['to']}` — score is green but blocked by: {', '.join(blocked)}")
    else:
        lines.append(f"\n⏸ Best: `{candidates[0]['to']}` — score needs {abs(candidates[0]['score']):.6f} more")

    return "\n".join(lines)


def cmd_profit():
    """Performance dashboard: P&L, win rate, trade stats."""
    conn = get_db()

    # ── Starting portfolio value (earliest coin_value snapshot) ──
    row = conn.execute(
        """SELECT MIN(id) as min_id FROM coin_value WHERE interval = 'MINUTELY'"""
    ).fetchone()
    starting_value = 0.0
    start_time = None
    if row and row["min_id"]:
        snap = conn.execute(
            """SELECT coin_id, balance, usd_price, datetime FROM coin_value
               WHERE interval = 'MINUTELY' AND id = ?""",
            (row["min_id"],),
        ).fetchall()
        start_time = snap[0]["datetime"] if snap else None
        # Also get the full first snapshot set (all coins at that timestamp)
        if snap:
            ts = snap[0]["datetime"]
            all_first = conn.execute(
                """SELECT coin_id, balance, usd_price FROM coin_value
                   WHERE interval = 'MINUTELY' AND datetime = ?""",
                (ts,),
            ).fetchall()
            for s in all_first:
                starting_value += (s["balance"] or 0) * (s["usd_price"] or 0)

    # ── Current value via Binance API ──
    holdings = get_holdings()
    current_value = get_portfolio_value(holdings)

    # ── Total trades ──
    total_trades = conn.execute(
        "SELECT COUNT(*) as cnt FROM trade_history WHERE state = 'COMPLETE'"
    ).fetchone()["cnt"]

    # ── Round-trip profit per coin hop ──
    # Each hop = sell coin A → USDC, then buy coin B ← USDC
    # Group trades in pairs: sell then buy
    all_trades = conn.execute(
        """SELECT alt_coin_id, crypto_coin_id, selling, alt_trade_amount,
                  crypto_trade_amount, datetime
           FROM trade_history WHERE state = 'COMPLETE'
           ORDER BY id ASC"""
    ).fetchall()

    round_trips = []
    wins = 0
    losses = 0
    total_fees_paid = 0.0  # rough estimate
    best_trade = {"coin": "?", "pnl": -9999.0}
    worst_trade = {"coin": "?", "pnl": 9999.0}

    # Track sells to match with subsequent buys
    pending_sell = None
    for t in all_trades:
        if t["selling"]:
            pending_sell = t
        elif pending_sell:
            sold_usdc = pending_sell["crypto_trade_amount"] or 0
            bought_usdc = t["crypto_trade_amount"] or 0
            coin = t["alt_coin_id"]
            pnl = bought_usdc - sold_usdc
            fee_est = sold_usdc * 0.002  # 0.2% round trip

            round_trips.append({
                "from_coin": pending_sell["alt_coin_id"],
                "to_coin": coin,
                "sold_usdc": sold_usdc,
                "bought_usdc": bought_usdc,
                "pnl": pnl,
                "fee_est": fee_est,
                "datetime": t["datetime"],
            })
            total_fees_paid += fee_est

            if pnl >= 0:
                wins += 1
            else:
                losses += 1

            if pnl > best_trade["pnl"]:
                best_trade = {"coin": f"{pending_sell['alt_coin_id']}→{coin}", "pnl": pnl}
            if pnl < worst_trade["pnl"]:
                worst_trade = {"coin": f"{pending_sell['alt_coin_id']}→{coin}", "pnl": pnl}

            pending_sell = None

    total_pnl = current_value - starting_value
    pnl_pct = (total_pnl / starting_value * 100) if starting_value > 0 else 0

    # ── Uptime ──
    if start_time:
        start_dt = datetime.strptime(start_time[:19], "%Y-%m-%d %H:%M:%S")
        now_dt = datetime.now()
        uptime_hours = (now_dt - start_dt).total_seconds() / 3600
        uptime_str = f"{uptime_hours:.1f}h" if uptime_hours < 48 else f"{uptime_hours / 24:.1f}d"
    else:
        uptime_str = "?"
        uptime_hours = 0

    conn.close()

    # ── Build message ──
    pnl_emoji = "📈" if total_pnl >= 0 else "📉"
    lines = [f"📊 **Performance Report**\n"]
    lines.append(f"{pnl_emoji} **P&L: ${total_pnl:+.2f}** ({pnl_pct:+.1f}%)")
    lines.append(f"🏦 Starting: `${starting_value:.2f}` → Current: `${current_value:.2f}`")
    lines.append(f"⏱ Uptime: `{uptime_str}` | Trades: `{total_trades}`\n")

    if round_trips:
        win_rate = wins / len(round_trips) * 100
        wr_emoji = "✅" if win_rate >= 50 else "⚠️"
        lines.append(f"**Round-trip Trades:** {len(round_trips)}")
        lines.append(f"{wr_emoji} Win rate: `{win_rate:.0f}%` ({wins}W / {losses}L)")
        lines.append(f"💸 Est. fees paid: `~${total_fees_paid:.2f}`\n")

        # Per-trade P&L
        lines.append("**Trade breakdown:**")
        cumul_pnl = 0.0
        for rt in round_trips:
            cumul_pnl += rt["pnl"]
            emoji = "🟢" if rt["pnl"] >= 0 else "🔴"
            lines.append(
                f"  {emoji} `{rt['from_coin']}→{rt['to_coin']}` "
                f"${rt['sold_usdc']:.2f} → ${rt['bought_usdc']:.2f} "
                f"({rt['pnl']:+.2f})"
            )
        lines.append(f"\n  Cumulative P&L from trades: `${cumul_pnl:+.2f}`")

    # ── Best / Worst ──
    if best_trade["pnl"] > -9999:
        lines.append(f"\n🏆 Best: `{best_trade['coin']}` ${best_trade['pnl']:+.2f}")
        lines.append(f"📉 Worst: `{worst_trade['coin']}` ${worst_trade['pnl']:+.2f}")

    return "\n".join(lines)


def cmd_help():
    """List available commands."""
    lines = ["🤖 **Available Commands**\n"]
    lines.append("/status — Current holdings and balance")
    lines.append("/trades — Recent trade history")
    lines.append("/coins — List of monitored coins")
    lines.append("/price — Current coin live price")
    lines.append("/profit — Performance dashboard & P&L")
    lines.append("/addcoin TICKER — Add a coin to the list")
    lines.append("/removecoin TICKER — Remove a coin from the list")
    lines.append("/swap OLD NEW — Replace one coin with another")
    lines.append("/hop — Show potential next trade targets")
    lines.append("/help — This message")
    return "\n".join(lines)


# ── Telegram Bot Loop ────────────────────────────────────────────────────────
# Commands that take arguments (coin ticker)
ARG_COMMANDS = {
    "/addcoin": cmd_addcoin,
    "/removecoin": cmd_removecoin,
    "/swap": cmd_swap,
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
    """Long-poll Telegram for updates."""
    offset = 0
    log.info("Telegram bot polling started")

    while True:
        try:
            params = {"timeout": 30, "offset": offset}
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params=params,
                timeout=35,
            )

            if r.status_code != 200:
                log.error(f"getUpdates failed: {r.status_code}")
                time.sleep(5)
                continue

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
            {"command": "status", "description": "Current holdings and balance"},
            {"command": "trades", "description": "Recent trade history"},
            {"command": "coins", "description": "List of monitored coins"},
            {"command": "price", "description": "Current coin live price"},
            {"command": "profit", "description": "Performance dashboard & P&L"},
            {"command": "hop", "description": "Show potential next trade"},
            {"command": "addcoin", "description": "Add a coin to trade list"},
            {"command": "removecoin", "description": "Remove a coin from list"},
            {"command": "swap", "description": "Swap one coin for another"},
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
