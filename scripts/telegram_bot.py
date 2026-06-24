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
import html
import re
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


def html_escape(text):
    """HTML-escape text for safe insertion into Telegram HTML messages.

    Telegram's HTML parse_mode requires <, >, & to be escaped in text content
    (including inside <pre>/<code> blocks).
    """
    return html.escape(str(text), quote=False)


def format_table(headers, rows, aligns=None):
    """Build a clean, aligned monospace table string.

    Args:
        headers: list of column header strings.
        rows: list of lists; each inner list is one data row of cell values
            (numbers are coerced to str automatically).
        aligns: optional list of per-column alignment specifiers, one per
            column.  Accepted values:
              'l'  – left-justify  (default; text columns)
              'r'  – right-justify (numeric columns; aligns by last char)
              'd'  – decimal-align (splits on last '.', pads integer part
                     left and fractional part right so decimal points line up)

    Returns:
        A multi-line string with a header row, a '──' separator line, and
        aligned data rows.  Column widths are computed from the actual data
        so padding always fits.  The result is NOT html-escaped; the caller
        must html_escape() it and wrap it in <pre>...</pre>.
    """
    ncols = len(headers)
    if aligns is None:
        aligns = ["l"] * ncols
    # Pad aligns to match ncols
    while len(aligns) < ncols:
        aligns.append("l")

    # Normalise every cell to a string and pad short rows to ncols.
    str_rows = []
    for row in rows:
        cells = [str(c) for c in row]
        while len(cells) < ncols:
            cells.append("")
        str_rows.append(cells[:ncols])

    # ── Decimal-align pass: split each cell at the last '.' and pad ──
    # For each 'd' column we split every cell into left_of_dot and
    # right_of_dot, then left-pad the left side to the column max and
    # right-pad the right side.  This lines up ALL decimal points perfectly
    # regardless of sign, currency symbol, or magnitude.
    for col_idx in range(ncols):
        if aligns[col_idx] != "d":
            continue
        lefts = []
        rights = []
        has_dot = []
        for row in str_rows:
            cell = row[col_idx]
            if "." in cell:
                # Split on LAST dot (handles values like $1,234.56)
                idx = cell.rfind(".")
                lefts.append(cell[:idx])
                rights.append(cell[idx + 1:])
                has_dot.append(True)
            else:
                # No decimal — whole cell is the left part
                lefts.append(cell)
                rights.append("")
                has_dot.append(False)

        max_left = max((len(l) for l in lefts), default=0)
        max_right = max((len(r) for r in rights if r), default=0)

        for row_idx in range(len(str_rows)):
            left = lefts[row_idx].rjust(max_left)
            if has_dot[row_idx] and rights[row_idx]:
                right = rights[row_idx].ljust(max_right)
                str_rows[row_idx][col_idx] = left + "." + right
            elif has_dot[row_idx]:
                # Has dot but nothing after (e.g. "42.")
                str_rows[row_idx][col_idx] = left + "." + " " * max_right
            else:
                # No decimal point — pad right side with spaces to match
                str_rows[row_idx][col_idx] = left + " " * (max_right + 1 if max_right > 0 else 0)

    # Column widths from headers + data
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i in range(ncols):
            widths[i] = max(widths[i], len(row[i]))

    sep = "  "
    # Header: right-align headers for 'r'/'d' columns, left for 'l'
    header_cells = []
    for i in range(ncols):
        if aligns[i] in ("r", "d"):
            header_cells.append(headers[i].rjust(widths[i]))
        else:
            header_cells.append(headers[i].ljust(widths[i]))
    header_line = sep.join(header_cells)
    divider_line = sep.join("\u2500" * widths[i] for i in range(ncols))

    data_lines = []
    for row in str_rows:
        cells = []
        for i in range(ncols):
            if aligns[i] in ("r", "d"):
                cells.append(row[i].rjust(widths[i]))
            else:
                cells.append(row[i].ljust(widths[i]))
        data_lines.append(sep.join(cells))
    return "\n".join([header_line, divider_line] + data_lines)


def _annotate_pnl_emoji(table, pnl_values):
    """Prefix each data row of an aligned table with a 🟢/🔴 profit marker.

    The circle emoji render as two monospace cells, so the header and divider
    lines receive a matching 3-cell blank spacer ("   ") and every data row
    gets the emoji plus a trailing space.

    Args:
        table: output string from format_table() (header, divider, data rows).
        pnl_values: numeric P&L value for each data row, in row order.
            >= 0 is shown green (🟢), negative is red (🔴).
            Pass None for rows that should get a blank spacer (summary lines).

    Returns:
        A new multi-line string with the indicators prepended.
    """
    lines = table.split("\n")
    spacer = "   "  # 2 cells (emoji) + 1 trailing spacer
    out = []
    for i, line in enumerate(lines):
        if i < 2:
            out.append(spacer + line)
        else:
            idx = i - 2
            if idx < len(pnl_values):
                val = pnl_values[idx]
                if val is None:
                    out.append(spacer + line)
                else:
                    marker = "🟢" if val >= 0 else "🔴"
                    out.append(f"{marker} {line}")
            else:
                out.append(spacer + line)
    return "\n".join(out)


def pre_table(headers, rows, aligns=None, pnl_values=None):
    """Format, optionally P&L-annotate, HTML-escape, and wrap a table."""
    table = format_table(headers, rows, aligns=aligns)
    if pnl_values is not None:
        table = _annotate_pnl_emoji(table, pnl_values)
    return f"<pre>{html_escape(table)}</pre>"


def kv_table(rows, key_header="ITEM", value_header="VALUE"):
    """Two-column key/value table for Telegram HTML dashboards."""
    return pre_table([key_header, value_header], rows, aligns=["l", "l"])


def money(value, digits=2, signed=False):
    """Format a USDC-ish money value."""
    try:
        val = float(value)
    except Exception:
        return "$-"
    sign = "+" if signed else ""
    return f"${val:{sign}.{digits}f}"


def pct(value, digits=1, signed=True):
    """Format a percentage value."""
    try:
        val = float(value)
    except Exception:
        return "-"
    sign = "+" if signed else ""
    return f"{val:{sign}.{digits}f}%"


def pnl_emoji(value):
    try:
        return "🟢" if float(value) >= 0 else "🔴"
    except Exception:
        return "⚪"


def funding_flow(funding):
    """Return readable funding direction for a short position."""
    if funding is None:
        return "-"
    if funding > 0:
        return "GET"
    if funding < 0:
        return "PAY"
    return "FLAT"


def status_word(ok=None, warn=False):
    """Plain monospace-safe status token for tables."""
    if warn:
        return "WARN"
    if ok is True:
        return "OK"
    if ok is False:
        return "ERR"
    return "INFO"


def section(label):
    return f"\n<b>{label}</b>"


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


# ── Command Handlers ─────────────────────────────────────────────────────────

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
        ["Futures available", money(fut_balance["available"]) if fut_balance else "$-"],
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
    elif fut_balance and fut_balance["available"] > 0:
        lines.append(f"\n💤 <b>Futures:</b> {money(fut_balance['available'])} idle — no open positions")

    return "\n".join(lines)


def cmd_trades():
    """Recent trade history including FAILED states + futures positions."""
    trades = get_trade_history(10)

    lines = ["📋 <b>Trade Log</b> 🧾\n"]

    if not trades:
        lines.append("<i>No spot trades yet.</i>")
    else:
        trade_rows = []
        state_pnls = []
        for t in trades:
            direction = "SELL" if t["selling"] else "BUY"
            coin = t["alt_coin_id"]
            amount = t["alt_trade_amount"] or 0
            cost = t["crypto_trade_amount"] or 0
            dt = t["datetime"][:16] if t["datetime"] else "?"
            state = t["state"] if t["state"] else "?"

            if state == "COMPLETE":
                trade_rows.append([dt, direction, coin, f"{amount:.2f}", money(cost), t["crypto_coin_id"]])
                state_pnls.append(None)
            elif state == "FAILED":
                trade_rows.append([dt, "FAIL", coin, f"{amount:.2f}", "-", "check"])
                state_pnls.append(-1)
            else:
                trade_rows.append([dt, state[:6], coin, f"{amount:.2f}", "-", "open"])
                state_pnls.append(None)
        lines.append(pre_table(
            ["TIME", "SIDE", "COIN", "AMOUNT", "USDC", "NOTE"],
            trade_rows,
            aligns=["l", "l", "l", "d", "d", "l"],
            pnl_values=state_pnls,
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
        return f"<code>{symbol}</code> is already in the active list."

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
    return f"✅ Added <code>{symbol}</code> — trade bot will pick it up in ~3 seconds."


def _disable_coin(symbol):
    """Disable a coin in the DB."""
    symbol = symbol.strip().upper()
    conn = get_db()

    row = conn.execute("SELECT symbol, enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if not row:
        conn.close()
        return f"<code>{symbol}</code> is not in the database."

    if not row["enabled"]:
        conn.close()
        return f"<code>{symbol}</code> is already disabled."

    current = get_current_coin()
    if current == symbol:
        conn.close()
        return f"⚠️ Cannot remove <code>{symbol}</code> — it's the coin the bot is currently holding!"

    conn.execute("UPDATE coins SET enabled = 0 WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    return f"❌ Removed <code>{symbol}</code> — trade bot will stop scouting it in ~3 seconds."


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
    result.append(_disable_coin(old))
    result.append(_enable_coin(new))
    rows = [["Old", old], ["New", new], ["New price", money(price, 6)], ["Futures", "YES" if new in FUTURES_ELIGIBLE else "NO"]]
    return "\n".join([f"🔁 <b>Swap Coin</b> — <code>{html_escape(old)}</code> → <code>{html_escape(new)}</code>\n", *result, kv_table(rows)])


def cmd_futures():
    """Futures wallet status: balance, open positions, P&L, funding rates."""
    balance = get_futures_balance()
    positions = get_futures_positions()

    if balance is None and not positions:
        return "❌ Cannot reach futures API. Check API keys."

    lines = ["🔻 <b>Futures Dashboard</b> 🐻\n"]

    unrealized_total = sum(p["pnl_usd"] for p in positions) if positions else 0.0
    if balance:
        equity = balance["balance"] + unrealized_total
        lines.append(kv_table([
            ["Wallet balance", money(balance["balance"])],
            ["Unrealized P&L", money(unrealized_total, signed=True)],
            ["Equity", money(equity)],
            ["Available", money(balance["available"])],
        ]))

    if positions:
        lines.append(section(f"📊 Open Positions ({len(positions)})"))
        pos_rows = []
        pnl_values = []
        for p in positions:
            funding = get_futures_funding(p["symbol"])
            funding_str = f"{funding*100:+.4f}%" if funding is not None else "-"
            pos_rows.append([
                p["symbol"], p["direction"], f"{p['qty']}",
                f"{p['leverage']}x", money(p["entry"], 4), money(p["mark"], 4),
                pct(p["pnl_pct"]), money(p["pnl_usd"], signed=True), funding_str, funding_flow(funding),
            ])
            pnl_values.append(p["pnl_usd"])
        lines.append(pre_table(
            ["SYMBOL", "DIR", "QTY", "LEV", "ENTRY", "MARK", "P&L%", "P&L$", "FUND", "FLOW"],
            pos_rows,
            aligns=["l", "l", "r", "r", "d", "d", "d", "d", "d", "l"],
            pnl_values=pnl_values,
        ))
        lines.append("<i>Funding flow is from the short side: GET = shorts receive, PAY = shorts pay.</i>")
    else:
        lines.append("\n💤 <b>No open futures positions.</b>")

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
                lines.append(section("📉 Top Short Radar"))
                cand_rows = []
                for coin, perf in performers[:5]:
                    bias = "WEAK" if perf < 0 else "GREEN"
                    cand_rows.append([coin, pct(perf, 2), bias])
                lines.append(pre_table(["COIN", "24H%", "BIAS"], cand_rows, aligns=["l", "d", "l"]))
    except Exception:
        pass

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
            if os.environ.get("DOCKER_IMAGE", "") in image or CONTAINER_NAME in name or "binance" in name.lower():
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

    lines = ["⚙️ <b>Bot Configuration</b> 🎛\n"]

    lines.append(section("🧭 Trading"))
    lines.append(kv_table([
        ["Strategy", cfg("strategy", "momentum")],
        ["Bridge", cfg("bridge", BRIDGE_SYMBOL)],
        ["Scout multiplier", cfg("scout_multiplier", "6")],
        ["Buy timeout", cfg("buy_timeout", "20")],
        ["Sell timeout", cfg("sell_timeout", "20")],
    ]))

    lines.append(section("🛡 Spot Risk"))
    lines.append(kv_table([
        ["Trailing stop", cfg("trailing_stop_enabled", "yes")],
        ["Trailing giveback", cfg_pct_plain("trailing_stop_pct", "15.0")],
        ["Min edge", cfg_pct_fraction("min_profit_threshold", "0.015")],
        ["RSI filter", cfg("rsi_filter_enabled", "yes")],
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
        "bull": "🟢 <b>Bull</b> — Momentum mode\nBot is buying the strongest coins and riding trends. Spot long positions.",
        "bear": "🔴 <b>Bear</b> — Defense mode\nBot has sold to USDC and may be <b>shorting via USDC-M futures</b>. Capital is being preserved/shorted.",
        "sideways": "🟡 <b>Sideways</b> — Mean reversion mode\nBot is buying dips and selling rips on oscillating coins.",
        "stormy": "🟠 <b>Stormy</b> — Conservative mode\nBot uses double z-score thresholds. Only high-conviction trades.",
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
        ["Avg volatility", pct(vol, 1, signed=False)],
    ]

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
    unrealized_pnl = sum(p["pnl_usd"] for p in positions) if positions else 0.0

    # True equity = spot + futures wallet + unrealized P&L
    current_value = spot_value + fut_wallet + unrealized_pnl

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
        ["Futures equity", money(fut_wallet + unrealized_pnl)],
        ["Futures wallet", money(fut_wallet)],
        ["Unrealized P&L", money(unrealized_pnl, signed=True)],
        ["Uptime", uptime_str],
        ["Clean hops", str(len(real_trips))],
    ]))

    fut_realized = get_futures_realized()
    if positions:
        lines.append(section("🔻 Open Position"))
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
    else:
        lines.append("\n🔻 <b>Open Position:</b> 💤 none")

    if fut_realized and fut_realized["realized"] != 0:
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
    lines.append(section("📈 Trading"))
    trade_rows = [
        ["Wins / losses / flat", f"{wins}W / {losses}L / {flat} flat"],
        ["Efficiency", f"{eff:.0f}%"],
        ["Spot realized", money(realized_from_hops, signed=True)],
    ]
    if fut_realized:
        trade_rows.append(["Futures net", money(fut_realized["net"], signed=True)])
    if phantom_count:
        trade_rows.append(["Deposit-tagged hops", f"{phantom_count} excluded"])
    if failed_trades:
        trade_rows.append(["Failed orders", str(failed_trades)])
    lines.append(kv_table(trade_rows))

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
    """Show potential next hops with full strategy filter breakdown."""
    # Regime-aware: in BEAR mode, always show futures short candidates
    # regardless of whether there's an open position
    positions = get_futures_positions()

    # Check regime
    conn = get_db()
    regime_row = conn.execute(
        "SELECT regime FROM market_regime_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    regime = regime_row["regime"] if regime_row else ""
    conn.close()

    if regime == "bear" or positions:
        lines = ["🔻 <b>Short Radar</b> 🐻\n"]
        if positions:
            open_short = positions[0]["symbol"].replace(BRIDGE_SYMBOL, "")
            lines.append(kv_table([["Mode", "BEAR"], ["Position", f"Shorting {open_short}"]]))
        else:
            lines.append(kv_table([["Mode", "BEAR"], ["Position", "No open short"], ["Action", "Scouting for entry"]]))
        _append_futures_candidates(lines, positions)
        return "\n".join(lines)

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
        lines = [f"⏳ No scout data yet for <code>{html_escape(current)}</code> — bot needs a few minutes to build ratios.\n"]
        lines.append("<b>🔻 Futures Short Candidates</b>\n")
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
                    lines.append(f"🔒 Currently shorting: <code>{html_escape(open_sym)}</code>\n")
                if falling:
                    lines.append(f"📉 <b>Falling coins</b> ({len(falling)} of {len(FUTURES_ELIGIBLE)}):")
                    shorted_syms = {p["symbol"] for p in positions if p["direction"] == "SHORT"}
                    cand_rows = []
                    for c in falling[:5]:
                        badge = " [SHORTING]" if c["fut_symbol"] in shorted_syms else ""
                        funding_str = f"{c['funding']*100:.4f}%" if c["funding"] is not None else "-"
                        mark_str = f"${c['mark_price']:.4f}" if c["mark_price"] else "-"
                        cand_rows.append([c["coin"] + badge, f"{c['perf_pct']:+.2f}%", funding_str, mark_str])
                    table = format_table(["COIN", "24H%", "FUNDING", "MARK"], cand_rows, aligns=["l", "d", "d", "d"])
                    lines.append(f"<pre>{html_escape(table)}</pre>")
                else:
                    lines.append("🟢 No futures-eligible coins falling — no short candidates")
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
        return f"❌ No viable pairs for <code>{html_escape(current)}</code>."

    candidates.sort(key=lambda x: x["score"], reverse=True)

    lines = [f"🚀 <b>Hop Radar</b> — from <code>{html_escape(current)}</code>\n"]
    lines.append(kv_table([
        ["Market", f"{regime} / avg vol {avg_volatility:.1f}%"],
        ["Cooldown", f"active ({cooldown_remaining} left)" if cooldown_active else "clear"],
        ["Z-score need", f"{active_zscore_threshold:.1f}"],
        ["Momentum guard", f"skip if coin drops >{MOMENTUM_CRASH_THRESHOLD}%"],
    ]))

    lines.append(section("🧪 Filter Board"))
    cand_rows = []
    for i, c in enumerate(candidates[:5], 1):
        price_str = money(c["price"], 4) if c["price"] else "?"
        market = "FUT" if c["futures"] else "SPOT"
        score_mark = "OK" if c["score_ok"] else "NO"
        if c["zscore"] is not None:
            zscore_mark = "OK" if c["zscore_ok"] else "NO"
            zscore_str = f"{zscore_mark} {c['zscore']:.1f}/{active_zscore_threshold:.1f}"
        else:
            zscore_str = "WAIT data"
        momentum_str = "OK" if c["momentum_ok"] else "NO crash"
        if c["all_clear"]:
            status = "READY"
        elif cooldown_active and c["score_ok"] and c["zscore_ok"] is True and c["momentum_ok"]:
            status = "COOLDN"
        else:
            status = "BLOCK"
        cand_rows.append([
            str(i), c["to"], market, price_str, pct(c["divergence"], 2), score_mark,
            zscore_str, momentum_str, status,
        ])
    lines.append(pre_table(
        ["#", "COIN", "MKT", "PRICE", "DIV", "SCORE", "Z", "MOM", "STATUS"],
        cand_rows,
        aligns=["r", "l", "l", "d", "d", "l", "l", "l", "l"],
    ))

    viable = [c for c in candidates if c["all_clear"]]
    close = [c for c in candidates if not c["all_clear"] and c["score_ok"]]
    if viable:
        best = viable[0]
        lines.append(f"\n🎯 <b>Next hop: <code>{best['to']}</code></b> — all filters passed!")
    elif close:
        best = close[0]
        blocked = []
        if not best["zscore_ok"]:
            blocked.append(f"z-score ({best['zscore']:.1f} &lt; {active_zscore_threshold:.1f})")
        if not best["momentum_ok"]:
            blocked.append("momentum crash")
        if cooldown_active:
            blocked.append("cooldown")
        lines.append(f"\n🎯 Closest: <code>{best['to']}</code> — blocked by: {', '.join(blocked)}")
    else:
        lines.append(f"\n⏸ Best: <code>{candidates[0]['to']}</code> — score needs {abs(candidates[0]['score']):.6f} more")

    # ── Futures Short Candidates ──
    positions = get_futures_positions()
    lines.append(section("🔻 Futures Short Radar"))
    _append_futures_candidates(lines, positions)

    return "\n".join(lines)


def _append_futures_candidates(lines, positions):
    """Append futures short candidates section to lines list."""
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

            if falling:
                lines.append(section(f"📉 Falling Coins ({len(falling)}/{len(FUTURES_ELIGIBLE)})"))
                shorted_syms = {p["symbol"] for p in positions if p["direction"] == "SHORT"}
                cand_rows = []
                for c in falling[:5]:
                    tag = "LIVE" if c["fut_symbol"] in shorted_syms else "WATCH"
                    funding_str = f"{c['funding']*100:+.4f}%" if c["funding"] is not None else "-"
                    mark_str = money(c["mark_price"], 4) if c["mark_price"] else "-"
                    cand_rows.append([
                        c["coin"], pct(c["perf_pct"], 2), funding_str,
                        funding_flow(c["funding"]), mark_str, tag,
                    ])
                lines.append(pre_table(
                    ["COIN", "24H%", "FUND", "FLOW", "MARK", "TAG"],
                    cand_rows,
                    aligns=["l", "d", "d", "l", "d", "l"],
                ))
                if has_open_short:
                    open_sym = next((p["symbol"] for p in positions if p["direction"] == "SHORT"), None)
                    lines.append(f"🔒 Current short: <code>{html_escape(open_sym)}</code>")
            else:
                lines.append("\n🟢 No futures-eligible coins are falling — no short candidates")
                lines.append(f"<i>All {len(short_candidates)} futures-eligible coins are green right now.</i>")
    except Exception:
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
    lines.append("\n✨ Tip: dashboards use 🟢/🔴 on P&L rows for quick scanning.")
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
