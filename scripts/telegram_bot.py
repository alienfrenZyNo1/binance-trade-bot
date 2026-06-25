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
  /shadow   — Research-only regime/candidate shadow report
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
import html
import re
import subprocess
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from binance_trade_bot.formatting.telegram_html import (
    format_duration,
    format_table,
    funding_flow,
    html_escape,
    kv_table,
    money,
    pct,
    pnl_emoji,
    pre_table,
    section,
    status_word,
)

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
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY") or os.environ.get("API_KEY", "")
BINANCE_API_SECRET = (
    os.environ.get("BINANCE_API_SECRET")
    or os.environ.get("BINANCE_API_SECRET_KEY")
    or os.environ.get("API_SECRET_KEY", "")
)

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


def _holding_fields(holding):
    """Return normalized symbol/balance/price/value fields for a holding row."""
    if isinstance(holding, dict):
        symbol = holding.get("coin_id")
        balance = float(holding.get("balance") or 0)
        price = float(holding.get("usd_price") or 0)
    else:
        symbol = holding["coin_id"]
        balance = float(holding["balance"] or 0)
        price = float(holding["usd_price"] or 0)
    return symbol, balance, price, balance * price


def build_spot_open_position(current_coin, holdings, last_buy):
    """Build open spot-position unrealized P&L from the latest buy and live holding.

    The account-level P&L already includes spot mark-to-market value, but /profit
    also needs an explicit open-position line so unrealized spot gains/losses are
    not hidden behind the headline equity number.
    """
    if not current_coin or current_coin == "?" or not last_buy:
        return None

    holding = None
    for candidate in holdings:
        symbol, balance, price, value = _holding_fields(candidate)
        if symbol == current_coin and balance > 0 and price > 0 and value > 0.01:
            holding = (symbol, balance, price, value)
            break
    if holding is None:
        return None

    symbol, balance, mark, value = holding
    bought_qty = float(last_buy["alt_trade_amount"] or 0)
    cost = float(last_buy["crypto_trade_amount"] or 0)
    if bought_qty <= 0 or cost <= 0:
        return None

    entry = cost / bought_qty
    cost_basis = balance * entry
    pnl_usd = value - cost_basis
    pnl_pct = (pnl_usd / cost_basis * 100) if cost_basis > 0 else 0.0
    return {
        "symbol": symbol,
        "qty": balance,
        "entry": entry,
        "mark": mark,
        "cost_basis": cost_basis,
        "value": value,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
    }


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
                try:
                    liquidation = float(p.get("liquidationPrice", 0) or 0)
                except Exception:
                    liquidation = 0.0
                try:
                    notional = abs(float(p.get("notional", 0) or 0))
                except Exception:
                    notional = abs(mark * qty)
                try:
                    break_even = float(p.get("breakEvenPrice", 0) or 0)
                except Exception:
                    break_even = 0.0
                positions.append({
                    "symbol": p["symbol"],
                    "direction": direction,
                    "qty": qty,
                    "entry": entry,
                    "break_even": break_even,
                    "mark": mark,
                    "leverage": leverage,
                    "margin_type": (p.get("marginType") or "?").upper(),
                    "liquidation": liquidation,
                    "notional": notional,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": un_pnl,
                })
        return positions
    except Exception as e:
        log.warning(f"get_futures_positions failed: {e}")
        return []


def get_futures_funding(symbol):
    """Get current/predicted funding rate for a futures symbol.

    Uses premiumIndex.lastFundingRate. Positive funding = longs pay shorts,
    so shorts get paid; negative funding = shorts pay.
    """
    try:
        r = requests.get(
            f"{FAPI_PUB}/premiumIndex",
            params={"symbol": symbol},
            timeout=10,
        )
        if r.status_code == 200:
            rate = r.json().get("lastFundingRate")
            if rate is not None:
                return float(rate)
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


def get_futures_algo_orders(symbol=None):
    """Get open Binance futures algo orders (hard stop + server trailing)."""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return []
    try:
        params = {}
        if symbol:
            params["symbol"] = symbol
        r = _signed_get(f"{FAPI_PUB}/openAlgoOrders", params=params)
        if r.status_code != 200:
            log.warning(f"Futures openAlgoOrders API: {r.status_code} {r.text[:150]}")
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"get_futures_algo_orders failed: {e}")
        return []


def _algo_order_type(order):
    return order.get("orderType") or order.get("type") or "?"


def _fmt_price(value, digits=4):
    if value in (None, "", "-"):
        return "-"
    try:
        return money(float(value), digits)
    except Exception:
        return "-"


def _float_or_none(value):
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except Exception:
        return None


def _protection_rows_for_position(position, orders):
    """Build compact protection rows for a futures position."""
    symbol = position["symbol"]
    entry = position["entry"] or 0
    direction = position["direction"]
    symbol_orders = [o for o in orders if o.get("symbol") == symbol]
    hard = next((o for o in symbol_orders if _algo_order_type(o) == "STOP_MARKET"), None)
    trail = next((o for o in symbol_orders if _algo_order_type(o) == "TRAILING_STOP_MARKET"), None)

    rows = []
    if hard:
        trigger = _float_or_none(hard.get("triggerPrice") or hard.get("stopPrice"))
        risk = "-"
        if trigger and entry > 0:
            if direction == "SHORT":
                risk = pct(-abs((trigger - entry) / entry * 100), 1)
            else:
                risk = pct(-abs((entry - trigger) / entry * 100), 1)
        rows.append(["Hard stop", _fmt_price(trigger), risk, "LIVE"])
    else:
        rows.append(["Hard stop", "-", "-", "MISSING"])

    if trail:
        activate = _float_or_none(trail.get("activatePrice") or trail.get("activationPrice"))
        callback = _float_or_none(trail.get("callbackRate"))
        lock = "-"
        if activate and callback is not None and entry > 0:
            if direction == "SHORT":
                worst_trigger = activate * (1 + callback / 100)
                lock = pct((entry - worst_trigger) / entry * 100, 1)
            else:
                worst_trigger = activate * (1 - callback / 100)
                lock = pct((worst_trigger - entry) / entry * 100, 1)
        cb = f" {callback:.1f}%" if callback is not None else ""
        rows.append(["Trail arm", _fmt_price(activate), f"lock {lock}", f"LIVE{cb}"])
    else:
        rows.append(["Trail arm", "-", "-", "MISSING"])
    return rows


def get_futures_realized():
    """Get realized PnL, funding, and commissions from futures income history.

    Paginate backwards from now to the first recorded deposit so `/profit`
    doesn't silently miss older income once the account has more than one page
    of futures entries.
    """
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        return None
    try:
        start_ms = None
        try:
            conn = get_db()
            row = conn.execute("SELECT MIN(datetime) AS dt FROM deposits").fetchone()
            conn.close()
            if row and row["dt"]:
                start_ms = int(datetime.fromisoformat(row["dt"]).timestamp() * 1000)
        except Exception:
            # Fall back to Binance default window if deposits table/date parsing fails.
            start_ms = None

        income = []
        end_ms = int(time.time() * 1000)
        pages = 0
        while pages < 20:
            params = {"limit": 1000, "endTime": end_ms}
            if start_ms:
                params["startTime"] = start_ms
            r = _signed_get(f"{FAPI_PUB}/income", params=params)
            if r.status_code != 200:
                return None
            batch = r.json()
            if not batch:
                break
            income.extend(batch)
            pages += 1
            if len(batch) < 1000:
                break
            oldest = min(int(e.get("time", end_ms)) for e in batch)
            if start_ms and oldest <= start_ms:
                break
            if oldest >= end_ms:
                break
            end_ms = oldest - 1

        realized = 0.0
        funding = 0.0
        commission = 0.0
        # Track per-position realized for breakdown
        position_pnl = {}  # symbol → total realized

        for entry in income:
            itype = entry.get("incomeType", "")
            amount = float(entry.get("income", 0))
            symbol = entry.get("symbol", "")

            if itype == "REALIZED_PNL":
                realized += amount
                position_pnl[symbol] = position_pnl.get(symbol, 0) + amount
            elif itype == "FUNDING_FEE":
                funding += amount
            elif itype == "COMMISSION":
                commission += amount

        return {
            "realized": realized,
            "funding": funding,
            "commission": commission,
            "net": realized + funding + commission,
            "positions": position_pnl,
        }
    except Exception as e:
        log.warning(f"get_futures_realized failed: {e}")
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


# ── Bot state / Strategy helpers ─────────────────────────────────────────────

def cfg_value(config, key, fallback=""):
    """Return effective config value with env var override semantics."""
    env_val = os.environ.get(key.upper())
    if env_val not in (None, ""):
        return env_val
    return config.get(key, fallback)



def get_bot_state(key, default=None):
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception:
        return default


def get_latest_regime():
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM market_regime_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row
    except Exception:
        return None


def get_recently_held_map():
    raw = get_bot_state("recently_held", "{}") or "{}"
    try:
        return {str(k): float(v) for k, v in json.loads(raw).items()}
    except Exception:
        return {}


def get_momentum_performance(coin_symbol, lookback_hours):
    """Return N-hour price performance for a spot coin, matching momentum strategy."""
    try:
        r = requests.get(
            f"{API_BASE}/klines",
            params={
                "symbol": f"{coin_symbol}{BRIDGE_SYMBOL}",
                "interval": "1h",
                "limit": int(lookback_hours) + 1,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        klines = r.json()
        if not klines or len(klines) < 2:
            return None
        start_price = float(klines[0][1])
        end_price = float(klines[-1][4])
        if start_price <= 0:
            return None
        return ((end_price / start_price) - 1.0) * 100
    except Exception:
        return None


def get_one_hour_performance(coin_symbol):
    try:
        r = requests.get(
            f"{API_BASE}/klines",
            params={"symbol": f"{coin_symbol}{BRIDGE_SYMBOL}", "interval": "1h", "limit": 2},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        klines = r.json()
        if not klines:
            return None
        k = klines[-1]
        open_price = float(k[1])
        close_price = float(k[4])
        if open_price <= 0:
            return None
        return ((close_price / open_price) - 1.0) * 100
    except Exception:
        return None


def compute_rsi_simple(closes, period=14):
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    window = changes[-period:]
    gains = [c for c in window if c > 0]
    losses = [-c for c in window if c < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_rsi(coin_symbol, period=14):
    try:
        r = requests.get(
            f"{API_BASE}/klines",
            params={"symbol": f"{coin_symbol}{BRIDGE_SYMBOL}", "interval": "1h", "limit": period + 2},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        closes = [float(k[4]) for k in r.json()]
        return compute_rsi_simple(closes, period)
    except Exception:
        return None


def get_coin_momentum_stats(coin_symbol, lookback_hours, rsi_period=14):
    """Fetch one kline window and derive momentum, latest 1h move, and RSI."""
    try:
        limit = max(int(lookback_hours) + 1, rsi_period + 2, 2)
        r = requests.get(
            f"{API_BASE}/klines",
            params={"symbol": f"{coin_symbol}{BRIDGE_SYMBOL}", "interval": "1h", "limit": limit},
            timeout=10,
        )
        if r.status_code != 200:
            return {"perf": None, "one_h": None, "rsi": None}
        klines = r.json()
        if not klines or len(klines) < 2:
            return {"perf": None, "one_h": None, "rsi": None}

        perf = None
        if len(klines) >= int(lookback_hours) + 1:
            lookback_window = klines[-(int(lookback_hours) + 1):]
            start_price = float(lookback_window[0][1])
            end_price = float(lookback_window[-1][4])
            if start_price > 0:
                perf = ((end_price / start_price) - 1.0) * 100

        latest = klines[-1]
        open_price = float(latest[1])
        close_price = float(latest[4])
        one_h = ((close_price / open_price) - 1.0) * 100 if open_price > 0 else None
        closes = [float(k[4]) for k in klines]
        rsi = compute_rsi_simple(closes, rsi_period)
        return {"perf": perf, "one_h": one_h, "rsi": rsi}
    except Exception:
        return {"perf": None, "one_h": None, "rsi": None}


def get_momentum_context():
    """Collect live data used by the active momentum-rotation strategy."""
    config = load_config()
    lookback = int(float(cfg_value(config, "momentum_lookback_hours", "18")))
    min_edge = float(cfg_value(config, "momentum_min_edge", "8.0"))
    min_target_perf = float(cfg_value(config, "momentum_min_target_perf", "0.0"))
    cooldown_seconds = int(float(cfg_value(config, "trade_cooldown_seconds", "7200")))
    churn_seconds = int(float(cfg_value(config, "churn_block_seconds", "14400")))
    confirmation_cycles = int(float(cfg_value(config, "confirmation_cycles", "3")))
    max_drop_1h = float(cfg_value(config, "momentum_max_drop_1h", "5.0"))
    rsi_enabled = str(cfg_value(config, "rsi_filter_enabled", "yes")).lower() in ("yes", "true", "1", "on")
    rsi_overbought = float(cfg_value(config, "rsi_overbought", "68"))

    current = get_current_coin()
    coins = get_coins()
    stats = {coin: get_coin_momentum_stats(coin, lookback) for coin in coins}
    perf = {coin: item["perf"] for coin, item in stats.items()}
    current_perf = perf.get(current)

    last_trade_ts = 0.0
    raw_last = get_bot_state("last_trade_time", "0") or "0"
    try:
        last_trade_ts = float(raw_last)
    except Exception:
        last_trade_ts = 0.0
    cooldown_left = max(0, cooldown_seconds - int(time.time() - last_trade_ts)) if last_trade_ts > 0 else 0

    recently_held = get_recently_held_map()
    candidates = []
    for coin in coins:
        if coin == current:
            continue
        coin_perf = perf.get(coin)
        if coin_perf is None or current_perf is None:
            continue
        edge = coin_perf - current_perf
        coin_stats = stats.get(coin, {})
        one_h = coin_stats.get("one_h")
        rsi = coin_stats.get("rsi") if rsi_enabled else None
        churn_left = 0
        if coin in recently_held:
            churn_left = max(0, churn_seconds - int(time.time() - recently_held[coin]))
        blockers = []
        if cooldown_left > 0:
            blockers.append("COOL")
        if churn_left > 0:
            blockers.append("CHURN")
        if coin_perf <= min_target_perf:
            blockers.append("MOM")
        if edge < min_edge:
            blockers.append("EDGE")
        if one_h is not None and one_h < -max_drop_1h:
            blockers.append("1H")
        if rsi_enabled and rsi is not None and rsi > rsi_overbought:
            blockers.append("RSI")
        candidates.append({
            "coin": coin,
            "perf": coin_perf,
            "edge": edge,
            "one_h": one_h,
            "rsi": rsi,
            "futures": coin in FUTURES_ELIGIBLE,
            "blockers": blockers,
            "status": "SIGNAL" if not blockers else ",".join(blockers[:2]),
        })

    candidates.sort(key=lambda c: c["edge"], reverse=True)
    return {
        "config": config,
        "lookback": lookback,
        "min_edge": min_edge,
        "min_target_perf": min_target_perf,
        "cooldown_seconds": cooldown_seconds,
        "cooldown_left": cooldown_left,
        "churn_seconds": churn_seconds,
        "confirmation_cycles": confirmation_cycles,
        "max_drop_1h": max_drop_1h,
        "rsi_enabled": rsi_enabled,
        "rsi_overbought": rsi_overbought,
        "current": current,
        "current_perf": current_perf,
        "perf": perf,
        "stats": stats,
        "candidates": candidates,
    }


def _regime_result_dict(result):
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        return result.to_dict()
    return {
        "regime": getattr(result, "regime", "unknown"),
        "confidence": getattr(result, "confidence", 0.0),
        "score": getattr(result, "score", 0.0),
        "reasons": getattr(result, "reasons", []),
        "metrics": getattr(result, "metrics", {}),
    }


def collect_shadow_regime(days=3, include_futures=True):
    """Run the research-only classifier for Telegram shadow reporting.

    This uses public Binance endpoints only and returns a dict; it never places
    orders, writes bot state, or changes the live strategy.
    """
    from scripts import research_regime_classifier as regime_research

    coins = get_coins()
    references = list(regime_research.DEFAULT_REFERENCES)
    data = regime_research.fetch_market_data(
        coins,
        references=references,
        bridge=BRIDGE_SYMBOL,
        days=days,
    )
    futures_data = None
    if include_futures:
        futures_data = regime_research.fetch_futures_signals(
            regime_research.DEFAULT_FUTURES_SYMBOLS,
            limit=6,
        )
    result = regime_research.classify_regime(
        data,
        references=references,
        breadth_coins=coins,
        futures_data=futures_data,
    )
    return result.to_dict()


def _shadow_action(regime, ctx):
    candidates = list(ctx.get("candidates") or [])
    lookback = ctx.get("lookback", "?")
    if regime == "stormy":
        return "STANDBY / protect capital - no new risk until storm clears"
    if regime == "bear":
        futures = [c for c in candidates if c.get("futures") and c.get("perf") is not None]
        falling = [c for c in futures if c.get("perf", 0) < 0]
        if falling:
            weakest = sorted(falling, key=lambda c: c.get("perf", 0))[0]
            return f"Would scout SHORT {weakest['coin']} ({lookback}h {pct(weakest.get('perf'), 1)})"
        return f"Would hold USDC / wait - no futures-eligible coin is negative enough"
    clear = [
        c for c in candidates
        if not c.get("blockers") and c.get("perf") is not None and c.get("perf", 0) > 0
    ]
    if clear:
        best = clear[0]
        return f"Would watch SPOT hop to {best['coin']} after {ctx.get('confirmation_cycles', '?')} confirmations"
    if candidates:
        best = candidates[0]
        blockers = ",".join(best.get("blockers") or []) or "waiting"
        return f"Would wait - closest {best.get('coin', '?')} blocked by {blockers}"
    return "Would wait - momentum data still building"


def _shadow_candidate_rows(regime, ctx, limit=5):
    candidates = list(ctx.get("candidates") or [])
    if regime in ("bear", "stormy"):
        candidates = [c for c in candidates if c.get("futures") and c.get("perf") is not None]
        candidates.sort(key=lambda c: c.get("perf", 0))
    rows = []
    pnl_vals = []
    for c in candidates[:limit]:
        status = "CLEAR" if not c.get("blockers") else ",".join(c.get("blockers")[:2])
        action = "SHORT" if regime == "bear" and c.get("perf", 0) < 0 else ("WATCH" if not c.get("blockers") else "WAIT")
        rows.append([
            c.get("coin", "?"),
            pct(c.get("perf"), 2) if c.get("perf") is not None else "-",
            pct(c.get("edge"), 2) if c.get("edge") is not None else "-",
            pct(c.get("one_h"), 1) if c.get("one_h") is not None else "-",
            f"{c.get('rsi'):.0f}" if c.get("rsi") is not None else "-",
            action,
            status,
        ])
        pnl_vals.append(c.get("edge") if regime not in ("bear", "stormy") else c.get("perf"))
    return rows, pnl_vals


def build_shadow_report(regime_result, momentum_ctx, *, live_regime=None):
    """Build compact Telegram HTML for research shadow mode."""
    result = _regime_result_dict(regime_result)
    regime = str(result.get("regime", "unknown")).lower()
    confidence = float(result.get("confidence", 0.0) or 0.0)
    score = float(result.get("score", 0.0) or 0.0)
    reasons = list(result.get("reasons") or [])
    metrics = result.get("metrics") or {}
    breadth = metrics.get("breadth") or {}
    futures = metrics.get("futures") or {}
    live = (live_regime or "unknown").lower()
    action = _shadow_action(regime, momentum_ctx)
    compare = "matches live" if live == regime else "DIFFERS from live"
    emoji_map = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "stormy": "🟠"}

    lines = [f"🧪 <b>Shadow Regime Report</b> {emoji_map.get(regime, '❓')}\n"]
    lines.append("⚠️ <b>SHADOW ONLY — NO LIVE ORDERS</b>")
    lines.append(kv_table([
        ["Research regime", f"{regime.upper()} ({confidence:.0%})"],
        ["Live bot", live.upper()],
        ["Compare", compare],
        ["Score", f"{score:+.2f}"],
        ["Hypothetical", action],
    ], key_header="ITEM", value_header="VALUE"))

    signal_rows = []
    if breadth:
        signal_rows.extend([
            ["Above EMA50", f"{float(breadth.get('above_ema50_pct', 0)):.0%}"],
            ["Advancers 24h", f"{float(breadth.get('advancers_24h_pct', 0)):.0%}"],
            ["Median 24h", pct(breadth.get("median_ret_24h", 0), 1)],
            ["Median vol", pct(breadth.get("median_vol_24h", 0), 1, signed=False)],
        ])
    if futures and futures.get("valid_symbols", 0):
        signal_rows.extend([
            ["Futures symbols", str(futures.get("valid_symbols", 0))],
            ["Funding", pct(futures.get("avg_funding_pct", 0), 3)],
            ["OI value", pct(futures.get("median_oi_value_change_pct", 0), 1)],
            ["Taker buy/sell", f"{float(futures.get('avg_taker_buy_sell_ratio', 0) or 0):.2f}"],
        ])
    if signal_rows:
        lines.append(section("📊 Top Signals"))
        lines.append(pre_table(["SIGNAL", "VALUE"], signal_rows[:8], aligns=["l", "l"]))

    if reasons:
        lines.append(section("🧾 Why"))
        reason_rows = [[str(i + 1), reason] for i, reason in enumerate(reasons[:4])]
        lines.append(pre_table(["#", "REASON"], reason_rows, aligns=["r", "l"]))

    rows, pnl_vals = _shadow_candidate_rows(regime, momentum_ctx)
    if rows:
        lines.append(section("🎯 Candidate Actions"))
        lines.append(pre_table(
            ["COIN", f"{momentum_ctx.get('lookback', '?')}H%", "EDGE", "1H", "RSI", "HYP", "BLOCK"],
            rows,
            aligns=["l", "d", "d", "d", "r", "l", "l"],
            pnl_values=pnl_vals,
        ))
    else:
        lines.append("\n🎯 <b>Candidate Actions:</b> <i>momentum data still building</i>")

    lines.append("<i>Shadow mode compares research signals with live behavior; it never places trades.</i>")
    return "\n".join(lines)


def cmd_shadow():
    """Research-only shadow regime/candidate report."""
    try:
        result = collect_shadow_regime()
        ctx = get_momentum_context()
        live_row = get_latest_regime()
        live_regime = live_row["regime"] if live_row else "unknown"
        return build_shadow_report(result, ctx, live_regime=live_regime)
    except Exception as e:
        log.warning(f"Could not build shadow report: {e}")
        return f"❌ <b>Shadow Regime Report</b>\n\nCould not build shadow report: {html_escape(e)}"


def cmd_status():
    """Current holdings, portfolio value (spot + futures), regime."""
    current_coin = get_current_coin()
    holdings = get_holdings()
    spot_value = get_portfolio_value(holdings)
    fut_balance = get_futures_balance()
    fut_positions = get_futures_positions()

    conn = get_db()
    regime_row = conn.execute(
        "SELECT regime FROM market_regime_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    regime = regime_row["regime"] if regime_row else "?"
    regime_emoji = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "stormy": "🟠"}.get(regime, "❓")

    fut_value = fut_balance["balance"] if fut_balance else 0
    unrealized_total = sum(p["pnl_usd"] for p in fut_positions) if fut_positions else 0.0
    fut_equity = fut_value + unrealized_total
    total_value = spot_value + fut_equity

    if regime == "bear" and fut_positions:
        holding = " + ".join(
            f"{p['symbol'].replace(BRIDGE_SYMBOL, '')} {p['direction']}" for p in fut_positions
        ) + " (futures)"
    elif regime == "bear":
        holding = "Cash — scouting shorts"
    else:
        holding = current_coin

    lines = [f"🤖 <b>Bot Status</b> {regime_emoji}\n"]
    lines.append(kv_table([
        ["Mode", regime.upper()],
        ["Holding", holding],
        ["Total equity", money(total_value)],
        ["Spot wallet", money(spot_value)],
        ["Futures equity", money(fut_equity)],
        ["Futures transferable", money(fut_balance["available"]) if fut_balance else "$-"],
    ]))

    hold_rows = []
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
            hold_rows.append([coin, f"{balance:.4f}", money(price, 4), money(value)])
    if hold_rows:
        lines.append(section("📦 Spot Wallet"))
        lines.append(pre_table(["COIN", "BALANCE", "PRICE", "VALUE"], hold_rows, aligns=["l", "d", "d", "d"]))
    else:
        lines.append("\n📦 <b>Spot Wallet:</b> <i>empty / dust only</i>")

    if fut_positions:
        lines.append(section(f"🔻 Futures Positions ({len(fut_positions)})"))
        pos_rows = []
        for p in fut_positions:
            pos_rows.append([
                p["symbol"], p["direction"], f"{p['qty']}",
                money(p["entry"], 4), money(p["mark"], 4),
                pct(p["pnl_pct"]), money(p["pnl_usd"], signed=True),
            ])
        lines.append(pre_table(
            ["SYMBOL", "DIR", "QTY", "ENTRY", "MARK", "P&L%", "P&L$"],
            pos_rows,
            aligns=["l", "l", "r", "d", "d", "d", "d"],
            pnl_values=[p["pnl_usd"] for p in fut_positions],
        ))
    elif fut_balance and fut_balance["balance"] > 0:
        lines.append(
            f"\n💤 <b>Futures:</b> {money(fut_balance['balance'])} idle "
            f"({money(fut_balance['available'])} transferable)"
        )

    return "\n".join(lines)


def cmd_trades():
    """Recent trade history including FAILED states + futures positions."""
    trades = get_trade_history(10)

    lines = ["📋 <b>Trade Log</b> 🧾\n"]

    if not trades:
        lines.append("<i>No spot trades yet.</i>")
    else:
        trade_rows = []
        for t in trades:
            direction = "SELL" if t["selling"] else "BUY"
            coin = t["alt_coin_id"]
            amount = t["alt_trade_amount"] or 0
            cost = t["crypto_trade_amount"] or 0
            dt = t["datetime"][:16] if t["datetime"] else "?"
            state = t["state"] if t["state"] else "?"

            if state == "COMPLETE":
                trade_rows.append([dt, direction, coin, f"{amount:.2f}", money(cost), t["crypto_coin_id"]])
            elif state == "FAILED":
                trade_rows.append([dt, "FAIL", coin, f"{amount:.2f}", "-", "check"])
            else:
                trade_rows.append([dt, state[:6], coin, f"{amount:.2f}", "-", "open"])
        lines.append(pre_table(
            ["TIME", "SIDE", "COIN", "AMOUNT", "USDC", "NOTE"],
            trade_rows,
            aligns=["l", "l", "l", "d", "d", "l"],
        ))

        conn = get_db()
        state_counts = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM trade_history GROUP BY state"
        ).fetchall()
        conn.close()
        if state_counts:
            lines.append(section("📊 Spot Summary"))
            rows = [[r["state"] or "?", str(r["cnt"])] for r in state_counts]
            lines.append(pre_table(["STATE", "COUNT"], rows, aligns=["l", "r"]))

    positions = get_futures_positions()
    lines.append(section("🔻 Futures Snapshot"))

    if positions:
        pos_rows = []
        for p in positions:
            pos_rows.append([
                p["symbol"], p["direction"], f"{p['qty']}",
                money(p["entry"], 4), money(p["mark"], 4),
                pct(p["pnl_pct"]), money(p["pnl_usd"], signed=True),
            ])
        lines.append(pre_table(
            ["SYMBOL", "DIR", "QTY", "ENTRY", "MARK", "P&L%", "P&L$"],
            pos_rows,
            aligns=["l", "l", "r", "d", "d", "d", "d"],
            pnl_values=[p["pnl_usd"] for p in positions],
        ))
    else:
        lines.append("💤 No open futures positions")

    return "\n".join(lines)


def cmd_coins():
    """List monitored coins with futures eligibility."""
    coins = get_coins()
    positions = get_futures_positions()
    if positions:
        current = f"{positions[0]['symbol'].replace(BRIDGE_SYMBOL, '')} SHORT"
    else:
        conn = get_db()
        regime_row = conn.execute(
            "SELECT regime FROM market_regime_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if regime_row and regime_row["regime"] == "bear":
            current = "Cash — scouting shorts"
        else:
            current = get_current_coin()

    fut_coins = [c for c in coins if c in FUTURES_ELIGIBLE]
    spot_only = [c for c in coins if c not in FUTURES_ELIGIBLE]

    lines = [f"👁 <b>Coin Universe</b> 🪐\n"]
    lines.append(kv_table([
        ["Bridge", BRIDGE_SYMBOL],
        ["Current", current],
        ["Total coins", str(len(coins))],
        ["Futures-ready", str(len(fut_coins))],
        ["Spot-only", str(len(spot_only))],
    ]))

    def chunk_rows(items, size=5):
        return [[str(i // size + 1), "  ".join(items[i:i + size])] for i in range(0, len(items), size)]

    if fut_coins:
        lines.append(section("🔻 Futures-Eligible"))
        lines.append(pre_table(["#", "COINS"], chunk_rows(fut_coins), aligns=["r", "l"]))

    if spot_only:
        lines.append(section("📦 Spot-Only"))
        lines.append(pre_table(["#", "COINS"], chunk_rows(spot_only), aligns=["r", "l"]))

    return "\n".join(lines)


def cmd_price():
    """Live price of current coin + futures context if eligible."""
    positions = get_futures_positions()
    if positions:
        current_coin = positions[0]["symbol"].replace(BRIDGE_SYMBOL, "")
    else:
        conn = get_db()
        regime_row = conn.execute(
            "SELECT regime FROM market_regime_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        regime = regime_row["regime"] if regime_row else ""
        conn.close()

        if regime == "bear":
            lines = ["💤 <b>Price Radar</b> — no open position, scouting shorts\n"]
            _append_futures_candidates(lines, [])
            return "\n".join(lines)

        current_coin = get_current_coin()
    stats = get_24h_stats(current_coin)

    if not stats:
        return f"❌ Could not fetch price for <code>{html_escape(current_coin)}</code>"

    change_emoji = "📈" if stats["change_pct"] >= 0 else "📉"
    lines = [f"💲 <b>{html_escape(current_coin)}/{BRIDGE_SYMBOL} Price Radar</b> {change_emoji}\n"]
    lines.append(pre_table(
        ["METRIC", "VALUE"],
        [
            ["Last", money(stats["price"], 6)],
            ["24h change", pct(stats["change_pct"], 2)],
            ["24h high", money(stats["high"], 6)],
            ["24h low", money(stats["low"], 6)],
            ["24h volume", f"${stats['volume']:,.0f}"],
        ],
        aligns=["l", "d"],
    ))

    if current_coin in FUTURES_ELIGIBLE:
        fut_symbol = f"{current_coin}{BRIDGE_SYMBOL}"
        mark = get_futures_mark_price(fut_symbol)
        funding = get_futures_funding(fut_symbol)
        if mark is not None:
            basis_pct = ((mark - stats["price"]) / stats["price"]) * 100 if stats["price"] > 0 else 0
            fut_rows = [
                ["Mark price", money(mark, 6)],
                ["Mark-vs-spot", pct(basis_pct, 3)],
            ]
            if funding is not None:
                fut_rows.append(["Funding", f"{funding*100:+.4f}%"])
                fut_rows.append(["Short flow", funding_flow(funding)])
            lines.append(section("🔻 Futures Context"))
            lines.append(pre_table(["METRIC", "VALUE"], fut_rows, aligns=["l", "d"]))
        else:
            lines.append("\n🔻 Futures eligible, but no mark-price data returned")

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
        return f"<code>{html_escape(symbol)}</code> is already in the active list."

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
    return f"✅ Added <code>{html_escape(symbol)}</code> — trade bot will pick it up in ~3 seconds."


def _disable_coin(symbol):
    """Disable a coin in the DB."""
    symbol = symbol.strip().upper()
    conn = get_db()

    row = conn.execute("SELECT symbol, enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        conn.close()
        return f"<code>{html_escape(symbol)}</code> is not in the database."

    if not row["enabled"]:
        conn.close()
        return f"<code>{html_escape(symbol)}</code> is already disabled."

    current = get_current_coin()
    if current == symbol:
        conn.close()
        return f"⚠️ Cannot remove <code>{html_escape(symbol)}</code> — it's the coin the bot is currently holding!"

    open_futures = [p for p in get_futures_positions() if p["symbol"].replace(BRIDGE_SYMBOL, "") == symbol]
    if open_futures:
        conn.close()
        return f"⚠️ Cannot remove <code>{html_escape(symbol)}</code> — there is an open futures position for it."

    conn.execute("UPDATE coins SET enabled = 0 WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    return f"✅ Disabled <code>{html_escape(symbol)}</code> — trade bot will stop scouting it in ~3 seconds."


def cmd_addcoin(args):
    """Add a coin to the monitored list."""
    if not args:
        return "🪙 <b>Add Coin</b>\n\nUsage: <code>/addcoin TICKER</code>\nExample: <code>/addcoin LTC</code>"
    symbol = args.strip().upper()

    price, err = _verify_usdc_pair(symbol)
    if err:
        return f"❌ <b>Add Coin Failed</b>\n\n<code>{html_escape(symbol)}</code>: {html_escape(err)}"

    volume = None
    low_volume = False
    try:
        r = requests.get(f"{API_BASE}/ticker/24hr", params={"symbol": f"{symbol}{BRIDGE_SYMBOL}"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            volume = float(d["quoteVolume"])
            low_volume = volume < 500000
    except Exception:
        pass

    result = _enable_coin(symbol)
    rows = [
        ["Pair", f"{symbol}{BRIDGE_SYMBOL}"],
        ["Price", money(price, 6)],
        ["24h volume", f"${volume:,.0f}" if volume is not None else "unknown"],
        ["Futures", "YES" if symbol in FUTURES_ELIGIBLE else "NO"],
    ]
    lines = [f"🪙 <b>Add Coin</b> — <code>{html_escape(symbol)}</code>\n", result, kv_table(rows)]
    if low_volume:
        lines.append("⚠️ Low volume — spreads may be wider than usual.")
    return "\n".join(lines)


def cmd_removecoin(args):
    """Remove a coin from the monitored list."""
    if not args:
        return "🧹 <b>Remove Coin</b>\n\nUsage: <code>/removecoin TICKER</code>\nExample: <code>/removecoin TIA</code>"
    symbol = args.strip().upper()
    return f"🧹 <b>Remove Coin</b> — <code>{html_escape(symbol)}</code>\n\n{_disable_coin(symbol)}"


def cmd_swap(args):
    """Swap one coin for another."""
    if not args or " " not in args:
        return "🔁 <b>Swap Coin</b>\n\nUsage: <code>/swap OLD NEW</code>\nExample: <code>/swap TIA LTC</code>"
    parts = args.strip().upper().split()
    old, new = parts[0], parts[1]

    if old == new:
        return "🔁 <b>Swap Coin</b>\n\nSame coin, nothing to do."

    price, err = _verify_usdc_pair(new)
    if err:
        return f"❌ <b>Swap Failed</b>\n\nCannot add <code>{html_escape(new)}</code>: {html_escape(err)}"

    result = []
    disable_result = _disable_coin(old)
    if not disable_result.startswith("✅ Disabled"):
        return "\n".join([
            f"🔁 <b>Swap Blocked</b> — <code>{html_escape(old)}</code> → <code>{html_escape(new)}</code>\n",
            disable_result,
            "No new coin was enabled because the old coin was not disabled.",
        ])
    result.append(disable_result)
    result.append(_enable_coin(new))
    rows = [["Old", old], ["New", new], ["New price", money(price, 6)], ["Futures", "YES" if new in FUTURES_ELIGIBLE else "NO"]]
    return "\n".join([f"🔁 <b>Swap Coin</b> — <code>{html_escape(old)}</code> → <code>{html_escape(new)}</code>\n", *result, kv_table(rows)])


def cmd_futures():
    """Futures wallet status: balance, open positions, P&L, funding rates."""
    balance = get_futures_balance()
    positions = get_futures_positions()

    if balance is None and not positions:
        return "❌ Cannot reach futures API. Check API keys."

    unrealized_total = sum(p["pnl_usd"] for p in positions) if positions else 0.0
    wallet_balance = balance["balance"] if balance else 0.0
    available = balance["available"] if balance else 0.0
    equity = wallet_balance + unrealized_total
    margin_used = 0.0
    for p in positions:
        try:
            lev = max(float(p.get("leverage") or 1), 1.0)
            margin_used += float(p.get("notional") or 0) / lev
        except Exception:
            pass
    mood = pnl_emoji(unrealized_total) if positions else "🟡"
    regime_row = get_latest_regime()
    regime = regime_row["regime"] if regime_row else "?"

    lines = [f"🔻 <b>Futures Control Room</b> {mood}\n"]

    status_text = "Live short" if any(p["direction"] == "SHORT" for p in positions) else "Scouting"
    if positions:
        short_names = ", ".join(p["symbol"].replace(BRIDGE_SYMBOL, "") for p in positions)
        status_text = f"{status_text}: {short_names}"
    else:
        status_text = "No open position"

    lines.append(kv_table([
        ["Status", status_text],
        ["Regime", regime.upper()],
        ["Wallet", money(wallet_balance)],
        ["Equity", money(equity)],
        ["Transferable", money(available)],
        ["Margin used", money(margin_used)],
        ["Unrealized P&L", money(unrealized_total, signed=True)],
    ]))

    all_algo_orders = get_futures_algo_orders() if positions else []

    if positions:
        for p in positions:
            funding = get_futures_funding(p["symbol"])
            funding_str = f"{funding*100:+.4f}%" if funding is not None else "-"
            liq = money(p.get("liquidation"), 4) if p.get("liquidation") else "-"
            break_even = money(p.get("break_even"), 4) if p.get("break_even") else "-"
            lines.append(section(f"{pnl_emoji(p['pnl_usd'])} {p['symbol']} {p['direction']}"))
            lines.append(kv_table([
                ["Size", f"{p['qty']} @ {p['leverage']}x {p.get('margin_type', '?')}"],
                ["Notional", money(p.get("notional", p["qty"] * p["mark"]))],
                ["Entry -> mark", f"{money(p['entry'], 4)} -> {money(p['mark'], 4)}"],
                ["Break-even", break_even],
                ["P&L", f"{pct(p['pnl_pct'])} / {money(p['pnl_usd'], signed=True)}"],
                ["Liquidation", liq],
                ["Funding", f"{funding_str} / {funding_flow(funding)}"],
            ]))

            protection_rows = _protection_rows_for_position(p, all_algo_orders)
            lines.append(section("🛡 Server Protection"))
            lines.append(pre_table(
                ["PROTECT", "TRIGGER", "IMPACT", "STATUS"],
                protection_rows,
                aligns=["l", "d", "l", "l"],
            ))
            if any(r[-1] == "MISSING" for r in protection_rows):
                lines.append("⚠️ Protection mismatch detected — hard stop/trailing should be checked immediately.")
            else:
                lines.append("✅ Hard stop + server trailing are live on Binance.")
    else:
        if regime == "bear":
            lines.append("\n💤 <b>No open futures position.</b> Bot is waiting for a clean short setup.")
        else:
            lines.append("\n💤 <b>No open futures position.</b> Futures are on standby while spot momentum mode is active.")

    _append_futures_candidates(lines, positions, limit=4, include_section=True)

    return "\n".join(lines)


def cmd_health():
    """System health check: DB, bot container, backups, WAL mode."""
    rows = []

    db_ok = os.path.exists(DB_PATH)
    if db_ok:
        db_size = os.path.getsize(DB_PATH) / 1024
        rows.append(["OK", "Database", f"exists, {db_size:.0f} KB"])
        try:
            conn = get_db()
            wal_row = conn.execute("PRAGMA journal_mode").fetchone()
            wal_mode = wal_row[0] if wal_row else "?"
            rows.append(["OK", "Journal", wal_mode])

            backup_dir = os.path.dirname(DB_PATH)
            backups = sorted(
                [f for f in os.listdir(backup_dir) if f.endswith(".db.bak")],
                reverse=True,
            ) if os.path.isdir(backup_dir) else []
            if backups:
                bak_path = os.path.join(backup_dir, backups[0])
                bak_age = time.time() - os.path.getmtime(bak_path)
                bak_age_str = f"{bak_age/3600:.1f}h ago" if bak_age < 86400 else f"{bak_age/86400:.1f}d ago"
                rows.append(["OK", "Backup", f"{backups[0]} ({bak_age_str})"])
            else:
                rows.append(["WARN", "Backup", "none found"])

            trade_count = conn.execute("SELECT COUNT(*) FROM trade_history").fetchone()[0]
            regime_count = conn.execute("SELECT COUNT(*) FROM market_regime_log").fetchone()[0]
            conn.close()
            rows.append(["INFO", "Rows", f"{trade_count} trades | {regime_count} regime logs"])
        except Exception as e:
            rows.append(["ERR", "Database", str(e)[:48]])
    else:
        rows.append(["ERR", "Database", f"missing at {DB_PATH}"])

    bot_found = False
    try:
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
            docker_image = os.environ.get("DOCKER_IMAGE", "")
            if (docker_image and docker_image in image) or CONTAINER_NAME in name or "binance" in name.lower():
                rows.append(["OK" if "Up" in status else "WARN", "Main bot", status.lower()])
                bot_found = True
                break
    except Exception:
        pass

    if not bot_found:
        try:
            conn2 = get_db()
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
                    rows.append(["OK", "Main bot", f"DB active, {source} {int(age_sec)}s ago"])
                    bot_found = True
                elif age_sec < 600:
                    rows.append(["WARN", "Main bot", f"last {source} {int(age_sec/60)}min ago"])
                    bot_found = True
        except Exception:
            pass

    if not bot_found:
        try:
            result2 = subprocess.run(
                ["systemctl", "is-active", "binance-trade-bot"],
                capture_output=True, text=True, timeout=5,
            )
            status2 = result2.stdout.strip()
            rows.append(["OK" if status2 == "active" else "ERR", "Main bot", f"systemd {status2 or 'unknown'}"])
        except Exception:
            rows.append(["ERR", "Main bot", "not detected"])

    pid_file = os.path.join(os.path.dirname(DB_PATH), "bot.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = f.read().strip()
            rows.append(["OK", "PID lock", f"active pid {pid}"])
        except Exception:
            rows.append(["WARN", "PID lock", "file unreadable"])
    else:
        rows.append(["INFO", "PID lock", "not present"])

    try:
        r = requests.get(f"{API_BASE}/ping", timeout=5)
        rows.append(["OK" if r.status_code == 200 else "WARN", "Spot API", "reachable" if r.status_code == 200 else f"status {r.status_code}"])
    except Exception:
        rows.append(["ERR", "Spot API", "unreachable"])

    try:
        r = requests.get(f"{FAPI_PUB}/ping", timeout=5)
        rows.append(["OK" if r.status_code == 200 else "WARN", "Futures API", "reachable" if r.status_code == 200 else f"status {r.status_code}"])
    except Exception:
        rows.append(["ERR", "Futures API", "unreachable"])

    problem_count = sum(1 for r in rows if r[0] in ("WARN", "ERR"))
    mood = "🟢" if problem_count == 0 else "🟡" if all(r[0] != "ERR" for r in rows) else "🔴"
    lines = [f"🏥 <b>System Health</b> {mood}\n"]
    lines.append(pre_table(["STATUS", "CHECK", "DETAIL"], rows, aligns=["l", "l", "l"]))
    return "\n".join(lines)


def cmd_config():
    """Show current bot configuration."""
    config = load_config()

    if not config:
        return "❌ Could not read configuration file."

    def cfg(key, fallback=""):
        env_key = key.upper()
        env_val = os.environ.get(env_key)
        if env_val not in (None, ""):
            return env_val
        return config.get(key, fallback)

    def cfg_pct_fraction(key, fallback):
        val = cfg(key, fallback)
        try:
            pct_val = float(val) * 100
            return f"{pct_val:.1f}%".replace(".0%", "%")
        except Exception:
            return val

    def cfg_pct_plain(key, fallback):
        val = cfg(key, fallback)
        try:
            return f"{float(val):.1f}%"
        except Exception:
            return val

    def cfg_funding(key, fallback):
        val = cfg(key, fallback)
        try:
            return f"{float(val) * 100:.4f}%"
        except Exception:
            return val

    def cfg_yes_no(key, fallback="no"):
        return str(cfg(key, fallback)).strip().lower() in ("yes", "true", "1", "on")

    lines = ["⚙️ <b>Bot Configuration</b> 🎛\n"]

    lines.append(section("🧭 Trading"))
    lines.append(kv_table([
        ["Strategy", cfg("strategy", "momentum")],
        ["Bridge", cfg("bridge", BRIDGE_SYMBOL)],
        ["Scout multiplier", cfg("scout_multiplier", "6")],
        ["Buy timeout", cfg("buy_timeout", "20")],
        ["Sell timeout", cfg("sell_timeout", "20")],
    ]))

    lines.append(section("🛡 Spot Momentum"))
    lines.append(kv_table([
        ["Lookback", f"{cfg('momentum_lookback_hours', '18')}h"],
        ["Rotation edge", cfg_pct_plain("momentum_min_edge", "8.0")],
        ["Cooldown", format_duration(cfg("trade_cooldown_seconds", "7200"))],
        ["Confirm cycles", cfg("confirmation_cycles", "3")],
        ["Trailing stop", cfg("trailing_stop_enabled", "yes")],
        ["Trailing giveback", cfg_pct_plain("trailing_stop_pct", "15.0")],
        ["RSI max", cfg("rsi_overbought", "68")],
        ["1h crash guard", cfg_pct_plain("momentum_max_drop_1h", "5.0")],
    ]))

    lines.append(section("🔻 Futures Risk"))
    lines.append(kv_table([
        ["Leverage", f"{cfg('futures_leverage', '1')}x"],
        ["Max margin", cfg_pct_fraction("futures_max_margin_pct", "0.5")],
        ["Hard stop", cfg_pct_plain("futures_stop_loss_pct", "15.0")],
        ["Client trail", cfg_pct_plain("futures_trailing_stop_pct", "10.0")],
        ["Server trail", cfg("futures_server_trailing_enabled", "yes")],
        ["Trail activation", cfg_pct_plain("futures_trailing_activation_pct", "3.0")],
        ["Server callback", cfg_pct_plain("futures_server_trailing_callback_rate", "1.0")],
        ["Funding guard", cfg_funding("futures_max_funding_rate", "0.0001")],
        ["Check interval", f"{cfg('futures_check_interval', '60')}s"],
    ]))

    lines.append(section("🟡 Canary Capital Guard"))
    canary_enabled = cfg_yes_no("canary_mode_enabled", "no")
    lines.append(kv_table([
        ["Mode", "enabled" if canary_enabled else "disabled"],
        ["Spot trade cap", money(float(cfg("canary_max_spot_trade_usdc", "0") or 0))],
        ["Futures margin cap", cfg_pct_fraction("canary_futures_max_margin_pct", "0")],
        ["Futures absolute cap", money(float(cfg("canary_max_futures_margin_usdc", "0") or 0))],
    ]))

    conn = get_db()
    enabled_count = conn.execute("SELECT COUNT(*) FROM coins WHERE enabled = 1").fetchone()[0]
    total_count = conn.execute("SELECT COUNT(*) FROM coins").fetchone()[0]
    conn.close()
    lines.append(section("🪙 Coin List"))
    lines.append(kv_table([["Active", str(enabled_count)], ["Total", str(total_count)]]))

    lines.append(f"\n<i>Effective values include env vars + defaults. file: <code>{html_escape(CONFIG_PATH)}</code></i>")

    return "\n".join(lines)


def cmd_kill(args=None):
    """Emergency kill switch: close all futures positions + transfer back to spot.

    Usage: /kill confirm
    """
    if args and args.strip().lower() == "confirm":
        return _execute_kill()

    positions = get_futures_positions()
    balance = get_futures_balance()

    lines = ["🚨 <b>Emergency Kill Switch</b>\n"]
    lines.append("⚠️ This is the big red button. It will close futures exposure and move USDC back to spot.")
    lines.append(kv_table([
        ["Step 1", "Close all open futures positions"],
        ["Step 2", f"Transfer {BRIDGE_SYMBOL} back to spot"],
        ["Step 3", "Bot waits until next bear-cycle entry"],
    ]))

    if positions:
        lines.append(section(f"🔻 Positions To Close ({len(positions)})"))
        pos_rows = []
        for p in positions:
            pos_rows.append([p["symbol"], p["direction"], f"{p['qty']}", pct(p["pnl_pct"]), money(p["pnl_usd"], signed=True)])
        lines.append(pre_table(
            ["SYMBOL", "DIR", "QTY", "P&L%", "P&L$"],
            pos_rows,
            aligns=["l", "l", "r", "d", "d"],
            pnl_values=[p["pnl_usd"] for p in positions],
        ))
    else:
        lines.append("\n✅ No open positions to close.")

    if balance and balance["balance"] > 0:
        lines.append(f"\n💼 Futures wallet to sweep: <code>{money(balance['balance'])}</code>")

    lines.append("\n⚠️ <b>To execute:</b> <code>/kill confirm</code>")
    return "\n".join(lines)


def _execute_kill():
    """Execute the kill switch: close positions + transfer back."""
    lines = ["🚨 <b>Kill Switch Executing</b>\n"]

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
                    lines.append(f"❌ Failed to close {p['symbol']}: {r.status_code} {html_escape(r.text[:100])}")
            except Exception as e:
                lines.append(f"❌ Error closing {p['symbol']}: {html_escape(e)}")
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
                lines.append(f"✅ Transferred {money(balance['balance'])} {BRIDGE_SYMBOL} back to spot")
            else:
                lines.append(f"❌ Transfer failed: {r.status_code} {html_escape(r.text[:100])}")
        except Exception as e:
            lines.append(f"❌ Transfer error: {html_escape(e)}")
    else:
        lines.append("✅ No USDC in futures wallet to transfer")

    lines.append("\n🏁 <b>Kill switch complete.</b> Bot is in spot-only mode.")
    lines.append("<i>The trade bot may re-enter futures on the next bear regime cycle.</i>")
    return "\n".join(lines)


def cmd_regime():
    """Show current market regime and what the bot is doing about it."""
    conn = get_db()

    row = conn.execute(
        """SELECT regime, adx_value, avg_volatility, btc_correlation, datetime
           FROM market_regime_log ORDER BY id DESC LIMIT 1"""
    ).fetchone()

    if not row:
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
                conn.close()
                avg_vol = total_vol / cnt if cnt > 0 else 0
                regime = "stormy" if avg_vol > 8 else "sideways"
                est_lines = ["🧠 <b>Market Regime</b> (estimated)\n"]
                est_lines.append(kv_table([
                    ["Status", regime.upper()],
                    ["Avg volatility", pct(avg_vol, 1, signed=False)],
                    ["Confidence", "collecting data"],
                ]))
                est_lines.append("<i>Bot is collecting data for full regime detection...</i>")
                return "\n".join(est_lines)
        except Exception:
            pass
        conn.close()
        return "❌ No regime data yet. Bot needs a few minutes to classify the market."

    regime = row["regime"]
    adx = row["adx_value"] or 0
    vol = row["avg_volatility"] or 0

    emoji_map = {"bull": "🟢", "bear": "🔴", "sideways": "🟡", "stormy": "🟠"}
    strategy_map = {
        "bull": "🟢 <b>Bull</b> — Spot momentum mode\nBot holds/rotates into the strongest coin when it outperforms by the configured edge.",
        "bear": "🔴 <b>Bear</b> — Futures defense mode\nBot sells spot to USDC, transfers margin, and shorts the weakest futures-eligible coin.",
        "sideways": "🟡 <b>Sideways</b> — Cautious momentum mode\nBot still uses momentum rotation, but waits for a clean edge before moving.",
        "stormy": "🟠 <b>Stormy</b> — Risk-off mode\nBot should be extra selective; check /hop and /futures for current candidates.",
    }

    emoji = emoji_map.get(regime, "❓")
    strategy = strategy_map.get(regime, "Unknown regime")

    lines = [f"🧠 <b>Market Regime</b> {emoji}\n"]
    lines.append(strategy)

    if adx > 50:
        trend_text = "Very strong trend"
    elif adx > 25:
        trend_text = "Trending"
    elif adx > 20:
        trend_text = "Weak trend forming"
    else:
        trend_text = "Range-bound / choppy"

    signal_rows = [
        ["Status", regime.upper()],
        ["ADX", f"{adx:.1f}"],
        ["Trend", trend_text],
    ]
    if vol:
        signal_rows.append(["Avg volatility", pct(vol, 1, signed=False)])

    if row["btc_correlation"] is not None:
        signal_rows.append(["BTC correlation", f"{row['btc_correlation']:.2f}"])

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
                signal_rows.append(["Duration", dur_str])
            except Exception:
                signal_rows.append(["Since", str(current_since)[:19]])

    lines.append(section("📊 Signals"))
    lines.append(pre_table(["METRIC", "VALUE"], signal_rows, aligns=["l", "l"]))

    # ── Futures context during bear ──
    if regime == "bear":
        lines.append(section("🔻 Bear Mode Active"))
        fut_balance = get_futures_balance()
        positions = get_futures_positions()
        if fut_balance:
            lines.append(kv_table([["Futures wallet", money(fut_balance["balance"])]], key_header="ITEM", value_header="VALUE"))
        if positions:
            bear_pos_rows = []
            for p in positions:
                bear_pos_rows.append([p["symbol"], "SHORT", pct(p["pnl_pct"]), money(p["pnl_usd"], signed=True)])
            lines.append(pre_table(
                ["SYMBOL", "DIR", "P&L%", "P&L$"],
                bear_pos_rows,
                aligns=["l", "l", "d", "d"],
                pnl_values=[p["pnl_usd"] for p in positions],
            ))
        elif fut_balance and fut_balance["balance"] > 5:
            lines.append("💤 Scouting for short entry...")
        else:
            lines.append("💤 Waiting for USDC transfer to futures wallet...")

    # Regime distribution (last 20 samples)
    if len(regime_history) >= 2:
        from collections import Counter
        counts = Counter(r["regime"] for r in regime_history)
        total = sum(counts.values())
        lines.append(section(f"📊 Recent Mix ({total} samples)"))
        dist_rows = [[r.upper(), str(c), f"{c / total * 100:.0f}%"] for r, c in counts.most_common()]
        lines.append(pre_table(["REGIME", "COUNT", "PCT"], dist_rows, aligns=["l", "r", "r"]))

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

    positions = get_futures_positions()
    futures_unrealized_pnl = sum(p["pnl_usd"] for p in positions) if positions else 0.0

    current_coin = get_current_coin()
    last_spot_buy = conn.execute(
        """SELECT alt_coin_id, alt_trade_amount, crypto_trade_amount, datetime
           FROM trade_history
           WHERE state = 'COMPLETE' AND selling = 0 AND alt_coin_id = ?
           ORDER BY id DESC LIMIT 1""",
        (current_coin,),
    ).fetchone()
    spot_position = build_spot_open_position(current_coin, holdings, last_spot_buy)
    spot_unrealized_pnl = spot_position["pnl_usd"] if spot_position else 0.0
    unrealized_pnl = spot_unrealized_pnl + futures_unrealized_pnl

    # True equity = spot mark-to-market value + futures wallet + futures unrealized P&L.
    # Spot unrealized is already included in spot_value, so don't add it twice.
    current_value = spot_value + fut_wallet + futures_unrealized_pnl

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
    lines = [f"📊 <b>Performance Report</b> {pnl_emoji}\n"]
    lines.append(f"{pnl_emoji} <b>{money(total_pnl, signed=True)}</b> ({pct(pnl_pct)})")
    lines.append(kv_table([
        ["Deposited", money(total_deposited)],
        ["Current equity", money(current_value)],
        ["Spot wallet", money(spot_value)],
        ["Futures equity", money(fut_wallet + futures_unrealized_pnl)],
        ["Futures wallet", money(fut_wallet)],
        ["Unrealized total", money(unrealized_pnl, signed=True)],
        ["Spot unrealized", money(spot_unrealized_pnl, signed=True)],
        ["Futures unrealized", money(futures_unrealized_pnl, signed=True)],
        ["Uptime", uptime_str],
        ["Clean hops", str(len(real_trips))],
    ]))

    fut_realized = get_futures_realized()
    if positions:
        lines.append(section("🔻 Open Futures Position"))
        pos_rows = []
        for p in positions:
            pos_rows.append([
                p["symbol"], p["direction"], f"{p['leverage']}x",
                money(p["entry"], 4), money(p["mark"], 4),
                pct(p["pnl_pct"]), money(p["pnl_usd"], signed=True),
            ])
        lines.append(pre_table(
            ["SYMBOL", "DIR", "LEV", "ENTRY", "MARK", "P&L%", "P&L$"],
            pos_rows,
            aligns=["l", "l", "r", "d", "d", "d", "d"],
            pnl_values=[p["pnl_usd"] for p in positions],
        ))
    elif spot_position:
        lines.append(section("🟢 Open Spot Position"))
        lines.append(pre_table(
            ["COIN", "TYPE", "QTY", "ENTRY", "MARK", "P&L%", "P&L$"],
            [[
                spot_position["symbol"], "SPOT", f"{spot_position['qty']:.6g}",
                money(spot_position["entry"], 4), money(spot_position["mark"], 4),
                pct(spot_position["pnl_pct"]), money(spot_position["pnl_usd"], signed=True),
            ]],
            aligns=["l", "l", "r", "d", "d", "d", "d"],
            pnl_values=[spot_position["pnl_usd"]],
        ))
    else:
        lines.append("\n🔻 <b>Open Position:</b> 💤 none")

    if fut_realized and any(abs(fut_realized[k]) > 0.000001 for k in ("realized", "funding", "commission", "net")):
        lines.append(section("💰 Futures Realized"))
        sorted_positions = sorted(fut_realized["positions"].items())
        fr_rows = [[sym, money(pnl, signed=True)] for sym, pnl in sorted_positions]
        fr_rows.append(["Funding", money(fut_realized["funding"], signed=True)])
        fr_rows.append(["Fees", money(fut_realized["commission"], signed=True)])
        fr_rows.append(["Net", money(fut_realized["net"], signed=True)])
        pnl_vals = [pnl for _, pnl in sorted_positions] + [None, None, None]
        lines.append(pre_table(["SYMBOL", "P&L"], fr_rows, aligns=["l", "d"], pnl_values=pnl_vals))

    total_decisions = wins + losses
    eff = (wins / total_decisions * 100) if total_decisions > 0 else 0
    phantom_count = len(round_trips) - len(real_trips)
    lines.append(section("🧾 Rotation Cash Deltas"))
    trade_rows = [
        ["Wins / losses / flat", f"{wins}W / {losses}L / {flat} flat"],
        ["Cash efficiency", f"{eff:.0f}%"],
        ["Spot cash delta", money(realized_from_hops, signed=True)],
    ]
    if fut_realized:
        trade_rows.append(["Futures net", money(fut_realized["net"], signed=True)])
    if phantom_count:
        trade_rows.append(["Deposit-tagged hops", f"{phantom_count} excluded"])
    if failed_trades:
        trade_rows.append(["Failed orders", str(failed_trades)])
    lines.append(kv_table(trade_rows))
    lines.append("<i>Spot cash delta is fee/slippage cash movement between rotations; headline account P&amp;L is the source of truth.</i>")

    if round_trips:
        lines.append(section("🧾 Hop History"))
        hop_rows = []
        hop_pnls = []
        for rt in round_trips[-8:]:
            if rt.get("phantom"):
                tag = "DEPOSIT"
                hop_pnls.append(None)
            elif rt["pnl"] > 0.01:
                tag = "WIN"
                hop_pnls.append(rt["pnl"])
            elif rt["pnl"] < -0.01:
                tag = "LOSS"
                hop_pnls.append(rt["pnl"])
            else:
                tag = "FLAT"
                hop_pnls.append(None)
            hop_rows.append([rt["from_coin"], rt["to_coin"], money(rt["pnl"], signed=True), tag])
        if len(round_trips) > 8:
            hop_rows.append(["...", f"+{len(round_trips) - 8} earlier", "", ""])
            hop_pnls.append(None)
        lines.append(pre_table(["FROM", "TO", "P&L", "TAG"], hop_rows, aligns=["l", "l", "d", "l"], pnl_values=hop_pnls))

    return "\n".join(lines)


def cmd_hop():
    """Show the live momentum-rotation decision board."""
    positions = get_futures_positions()
    regime_row = get_latest_regime()
    regime = regime_row["regime"] if regime_row else ""
    ctx = get_momentum_context()
    current = ctx["current"]
    lookback = ctx["lookback"]

    if regime == "bear" or positions:
        lines = ["🔻 <b>Short Radar</b> 🐻\n"]
        if positions:
            open_short = ", ".join(
                p["symbol"].replace(BRIDGE_SYMBOL, "") for p in positions if p["direction"] == "SHORT"
            ) or positions[0]["symbol"].replace(BRIDGE_SYMBOL, "")
            pos_text = f"Shorting {open_short}"
        else:
            pos_text = "No open short"
        lines.append(kv_table([
            ["Mode", (regime or "bear").upper()],
            ["Position", pos_text],
            ["Lookback", f"{lookback}h momentum"],
            ["Funding guard", cfg_value(ctx["config"], "futures_max_funding_rate", "0.0001")],
        ]))
        _append_futures_candidates(lines, positions, limit=5, include_section=True, context=ctx)
        return "\n".join(lines)

    lines = [f"🚀 <b>Hop Radar</b> — momentum from <code>{html_escape(current)}</code>\n"]
    current_perf = ctx["current_perf"]
    current_perf_text = pct(current_perf, 2) if current_perf is not None else "building"
    cooldown_text = (
        f"active ({format_duration(ctx['cooldown_left'])} left)"
        if ctx["cooldown_left"] > 0 else "clear"
    )
    lines.append(kv_table([
        ["Current", f"{current} / {current_perf_text}"],
        ["Lookback", f"{lookback}h"],
        ["Required edge", pct(ctx["min_edge"], 1, signed=False)],
        ["Cooldown", cooldown_text],
        ["Confirm", f"{ctx['confirmation_cycles']} cycles"],
        ["Guards", f"RSI ≤ {ctx['rsi_overbought']:.0f}, 1h drop > -{ctx['max_drop_1h']:.0f}%"],
    ]))

    candidates = ctx["candidates"]
    if current_perf is None or not candidates:
        lines.append("\n⏳ Momentum data is still building. Try again after the next scout cycle.")
        _append_futures_candidates(lines, positions, limit=4, include_section=True, context=ctx)
        return "\n".join(lines)

    lines.append(section("🧪 Momentum Board"))
    rows = []
    pnl_vals = []
    for c in candidates[:5]:
        rsi = f"{c['rsi']:.0f}" if c["rsi"] is not None else "-"
        one_h = pct(c["one_h"], 1) if c["one_h"] is not None else "-"
        status = c["status"] or "WAIT"
        rows.append([
            c["coin"],
            pct(c["perf"], 2),
            pct(c["edge"], 2),
            one_h,
            rsi,
            status,
        ])
        pnl_vals.append(c["edge"])
    lines.append(pre_table(
        ["COIN", f"{lookback}H%", "EDGE", "1H", "RSI", "STATUS"],
        rows,
        aligns=["l", "d", "d", "d", "r", "l"],
        pnl_values=pnl_vals,
    ))

    clear = [c for c in candidates if not c["blockers"]]
    if clear:
        best = clear[0]
        lines.append(
            f"\n🎯 <b>Best signal:</b> <code>{html_escape(best['coin'])}</code> — filters clear; "
            f"bot needs {ctx['confirmation_cycles']} confirming cycles."
        )
    else:
        best = candidates[0]
        blocker_names = {
            "COOL": "cooldown",
            "CHURN": "recently held",
            "MOM": "target not positive",
            "EDGE": "edge too small",
            "1H": "1h crash guard",
            "RSI": "RSI overbought",
        }
        blocked = ", ".join(blocker_names.get(b, b) for b in best["blockers"][:3]) or "waiting"
        lines.append(
            f"\n⏸ <b>Closest:</b> <code>{html_escape(best['coin'])}</code> — blocked by {html_escape(blocked)}."
        )

    lines.append("<i>Status: COOL=cooldown, EDGE=needs more outperformance, RSI=overbought, 1H=sharp drop.</i>")
    _append_futures_candidates(lines, positions, limit=4, include_section=True, context=ctx)
    return "\n".join(lines)


def _append_futures_candidates(lines, positions, limit=5, include_section=False, context=None):
    """Append futures short candidates using the same momentum window as the strategy."""
    if include_section:
        lines.append(section("📉 Short Radar"))

    try:
        ctx = context or get_momentum_context()
        lookback = ctx["lookback"]
        config = ctx["config"]
        funding_guard = float(cfg_value(config, "futures_max_funding_rate", "0.0001"))
        perf_map = ctx.get("perf", {})
        stats = ctx.get("stats", {})
        enabled_futures = [c for c in get_coins() if c in FUTURES_ELIGIBLE]
        shorted_syms = {p["symbol"] for p in positions if p["direction"] == "SHORT"}
        short_candidates = []
        for coin in enabled_futures:
            perf = perf_map.get(coin)
            if perf is None:
                continue
            one_h = (stats.get(coin) or {}).get("one_h")
            short_candidates.append({"coin": coin, "perf": perf, "one_h": one_h, "sym": f"{coin}{BRIDGE_SYMBOL}"})

        falling = [c for c in short_candidates if c["perf"] < 0]
        falling.sort(key=lambda c: c["perf"])
        if not falling:
            lines.append(f"🟢 No futures-eligible coins are negative on {lookback}h momentum.")
            return

        cand_rows = []
        pnl_vals = []
        for c in falling[:limit]:
            funding = get_futures_funding(c["sym"])
            mark = get_futures_mark_price(c["sym"])
            funding_str = f"{funding*100:+.4f}%" if funding is not None else "-"
            flow = funding_flow(funding)
            if c["sym"] in shorted_syms:
                tag = "LIVE"
            elif funding is not None and funding < -funding_guard:
                tag = "PAYHI"
            else:
                tag = "WATCH"
            cand_rows.append([
                c["coin"],
                pct(c["perf"], 2),
                pct(c["one_h"], 1) if c["one_h"] is not None else "-",
                funding_str,
                flow,
                money(mark, 4) if mark else "-",
                tag,
            ])
            pnl_vals.append(c["perf"])
        lines.append(pre_table(
            ["COIN", f"{lookback}H%", "1H", "FUND", "FLOW", "MARK", "TAG"],
            cand_rows,
            aligns=["l", "d", "d", "d", "l", "d", "l"],
            pnl_values=pnl_vals,
        ))
        if shorted_syms:
            open_sym = next(iter(shorted_syms))
            lines.append(f"🔒 Current short: <code>{html_escape(open_sym)}</code>")
        lines.append("<i>TAG: WATCH=eligible, PAYHI=funding cost above guard, LIVE=open short.</i>")
    except Exception as e:
        log.warning(f"Could not build futures short radar: {e}")
        lines.append("❌ Could not fetch futures short candidates")


def cmd_deposit(args=""):
    """Record a deposit. Usage: /deposit <amount> [note]"""
    argstr = args.strip() if isinstance(args, str) else " ".join(args).strip()
    if not argstr:
        # Show current deposits
        conn = get_db()
        try:
            rows = conn.execute("SELECT id, amount, currency, source, note, datetime FROM deposits ORDER BY id ASC").fetchall()
        except Exception:
            return "❌ Deposits table not set up yet."
        conn.close()

        if not rows:
            return "📋 <b>Deposits</b>\n\nNo deposits recorded. Use <code>/deposit &lt;amount&gt; [note]</code> to record a top-up."

        total = sum(r["amount"] for r in rows)
        lines = [f"📋 <b>Deposits</b> 💰\n", kv_table([["Total recorded", money(total)], ["Entries", str(len(rows))]])]
        dep_rows = []
        for r in rows:
            dep_rows.append([
                money(r["amount"]), r["currency"], r["source"],
                r["note"] or "-", r["datetime"][:16],
            ])
        lines.append(pre_table(["AMOUNT", "CUR", "SOURCE", "NOTE", "DATE"], dep_rows, aligns=["d", "l", "l", "l", "l"]))
        return "\n".join(lines)

    parts = argstr.split(None, 1)
    try:
        amount = float(parts[0])
        if amount <= 0:
            return "❌ <b>Deposit</b>\n\nAmount must be positive."
    except ValueError:
        return "❌ <b>Deposit</b>\n\nUsage: <code>/deposit &lt;amount&gt; [note]</code>\nExample: <code>/deposit 50 topped up from main wallet</code>"

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
        return f"❌ Failed to record deposit: {html_escape(e)}"

    deposit_table = kv_table([["Amount", money(amount)], ["New total", money(total)], ["Source", "telegram"]])
    return f"✅ <b>Deposit Recorded</b>\n\n{deposit_table}"


def cmd_help():
    """List available commands."""
    lines = ["🤖 <b>Command Menu</b> 🎮\n"]
    rows = [
        ["Monitor", "/status", "portfolio + current position"],
        ["Monitor", "/profit", "P&L, deposits, clean hops"],
        ["Monitor", "/trades", "recent spot/futures activity"],
        ["Market", "/price", "live spot + futures context"],
        ["Market", "/hop", "next hop / short radar"],
        ["Market", "/regime", "bull/bear/sideways state"],
        ["Market", "/shadow", "research-only what-if report"],
        ["Market", "/coins", "monitored coin universe"],
        ["Futures", "/futures", "wallet, shorts, funding"],
        ["System", "/health", "DB, bot, APIs"],
        ["System", "/config", "effective settings"],
        ["System", "/deposit", "show or record top-ups"],
        ["Danger", "/kill", "emergency close + sweep"],
        ["Coins", "/addcoin TICKER", "add a coin"],
        ["Coins", "/removecoin TICKER", "remove a coin"],
        ["Coins", "/swap OLD NEW", "replace one coin"],
    ]
    lines.append(pre_table(["GROUP", "COMMAND", "WHAT IT DOES"], rows, aligns=["l", "l", "l"]))
    lines.append("\n✨ Tip: dashboards use 🟢/🔴 on P&amp;L rows for quick scanning.")
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
    "/shadow": cmd_shadow,
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
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error(f"sendMessage failed: {r.status_code} {r.text[:200]}")
            # Retry without HTML markup (strip tags + unescape entities)
            clean = re.sub(r"<[^>]+>", "", text)
            clean = html.unescape(clean)
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": clean},
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
