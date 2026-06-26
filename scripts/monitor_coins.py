#!/usr/bin/env python3
"""Regime-aware autonomous coin list manager for binance-trade-bot.

Scans Binance's top USDC pairs, scores each candidate against the current
market regime using multi-timeframe momentum, RSI, volatility, and
diversification metrics, then maintains an optimal coin list in the DB.

Scoring philosophy:
  BULL     — Momentum leaders with room to run (strong 7d/14d trend,
             RSI 50-72 sweet spot, high volume)
  SIDEWAYS — Oscillation quality (moderate volatility for rotation
             signals, neutral momentum, high volume)
  BEAR     — Short-ready targets (liquid USDC perp required, negative
             momentum, RSI > 30 so room to fall)
  STORMY   — Defensive (highest volume + lowest volatility only)

Works directly against SQLite — no file editing, no Docker restart.
The trade bot picks up DB changes on its next scout cycle (~3s).
"""

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import ccxt

DB_PATH = os.environ.get("DB_PATH", "/data/binance-bot-data/crypto_trading.db")
STATE_FILE = os.path.join(os.path.dirname(__file__), "monitor_state.json")
BRIDGE = "USDC"

# ── Thresholds ───────────────────────────────────────────────────────────────
VOLUME_REMOVE_THRESHOLD = 50_000
VOLUME_WARN_THRESHOLD = 500_000
LOW_VOLUME_DAYS_TO_REPLACE = 3
MAX_COINS = 20
MIN_COINS = 12
MIN_HISTORY_DAYS = 30          # Require 30d of daily candles
CORRELATION_THRESHOLD = 0.88   # Reject candidates too correlated with list
SETTLE_DAYS_AFTER_ADD = 5      # Don't upgrade-swap a coin added < N days ago
MAX_UPGRADES_PER_RUN = 1       # Conservative: max 1 swap per daily run
UPGRADE_EDGE_MULT = 1.8        # Candidate must score 1.8x the weakest coin

# Coins that should never be in the momentum rotation list
EXCLUDE_SYMBOLS = {
    "BTC", "ETH", "BNB", "USDC", "USDT", "FDUSD", "TUSD", "BUSD",
    "USDP", "DAI", "USTC", "EUR", "GBP", "AUD", "BRL", "TRY",
    "USD1", "PAXG", "DEUSD", "UAH", "RUB", "ZAR", "MXN", "ARS",
    "COLON", "PLN", "RON", "NGN",
}

# ── DB helpers ───────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_enabled_coins():
    conn = _db()
    coins = [r[0] for r in conn.execute(
        "SELECT symbol FROM coins WHERE enabled = 1 AND symbol != ?", (BRIDGE,)
    ).fetchall()]
    conn.close()
    return sorted(coins)


def get_held_coin():
    conn = _db()
    row = conn.execute(
        "SELECT coin_id FROM current_coin_history ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_current_regime():
    """Read the latest regime from the bot's DB."""
    conn = _db()
    row = conn.execute(
        "SELECT regime FROM market_regime_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else "sideways"


def get_futures_symbols():
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT key, value FROM bot_state WHERE key LIKE 'futures_%'"
        ).fetchall()
        symbols = set()
        for r in rows:
            if r["key"] == "futures_open_positions":
                for p in json.loads(r["value"]):
                    sym = p.get("symbol", "").replace(BRIDGE, "")
                    if sym:
                        symbols.add(sym)
        conn.close()
        return symbols
    except Exception:
        conn.close()
        return set()


def db_disable_coin(symbol):
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
    conn = _db()
    row = conn.execute("SELECT enabled FROM coins WHERE symbol = ?", (symbol,)).fetchone()
    if row and row[0]:
        conn.close()
        return False
    if row:
        conn.execute("UPDATE coins SET enabled = 1 WHERE symbol = ?", (symbol,))
    else:
        conn.execute("INSERT OR IGNORE INTO coins (symbol, enabled) VALUES (?, 1)", (symbol,))
    conn.commit()
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
    return {"low_volume_days": {}, "last_run": None, "coin_added_date": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Metrics ──────────────────────────────────────────────────────────────────

def _wilder_rsi(closes, period=14):
    """Compute RSI using Wilder's smoothing (matches the trade bot)."""
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(0, change)
        loss = max(0, -change)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _pct_change(old, new):
    if old <= 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def _std(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(var)


def _correlation(a, b):
    """Pearson correlation of two return series."""
    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va == 0 or vb == 0:
        return 0.0
    return cov / math.sqrt(va * vb)


# ── Market data ──────────────────────────────────────────────────────────────

class MarketScanner:
    """Fetches and caches market data for coin scoring."""

    def __init__(self):
        self.spot = ccxt.binance()
        self.futures = None  # Lazy init only for BEAR
        self._perp_symbols = None
        self._daily_klines = {}
        self._hourly_klines = {}
        self._daily_returns = {}

    def _init_futures(self):
        if self.futures is None:
            self.futures = ccxt.binance({"options": {"defaultType": "future"}})

    def has_perp(self, coin):
        """Check if a USDC perpetual future exists for this coin."""
        self._init_futures()
        if self.futures is None:
            return False
        if self._perp_symbols is None:
            try:
                markets = self.futures.load_markets()
                self._perp_symbols = {
                    s.split("/")[0]
                    for s in markets
                    if s.endswith("/USDC:USDC")
                }
                # Handle 1000X variants (1000PEPE → PEPE, 1000BONK → BONK)
                for s in list(self._perp_symbols):
                    if s.startswith("1000"):
                        self._perp_symbols.add(s[4:])
            except Exception:
                self._perp_symbols = set()
        return coin in self._perp_symbols

    def get_daily_klines(self, coin, limit=31):
        """Get daily OHLCV, return list of close prices."""
        if coin not in self._daily_klines:
            try:
                ohlcv = self.spot.fetch_ohlcv(f"{coin}/{BRIDGE}", "1d", limit=limit)
                self._daily_klines[coin] = [k[4] for k in ohlcv]  # close prices
            except Exception:
                self._daily_klines[coin] = []
        return self._daily_klines[coin]

    def get_hourly_klines(self, coin, limit=100):
        """Get hourly OHLCV, return list of close prices."""
        if coin not in self._hourly_klines:
            try:
                ohlcv = self.spot.fetch_ohlcv(f"{coin}/{BRIDGE}", "1h", limit=limit)
                self._hourly_klines[coin] = [k[4] for k in ohlcv]
            except Exception:
                self._hourly_klines[coin] = []
        return self._hourly_klines[coin]

    def get_daily_returns(self, coin):
        """Daily log returns for correlation computation."""
        if coin not in self._daily_returns:
            closes = self.get_daily_klines(coin)
            rets = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            self._daily_returns[coin] = rets
        return self._daily_returns[coin]


# ── Coin metrics ─────────────────────────────────────────────────────────────

class CoinMetrics:
    """Computed metrics for a single coin."""

    def __init__(self, coin, scanner, quote_volume):
        self.coin = coin
        self.volume = quote_volume

        daily = scanner.get_daily_klines(coin)
        hourly = scanner.get_hourly_klines(coin)

        self.has_data = len(daily) >= 14 and len(hourly) >= 15
        self.has_perp = scanner.has_perp(coin)

        # Multi-timeframe momentum (% change)
        self.mom_7d = _pct_change(daily[-8], daily[-1]) if len(daily) >= 8 else 0.0
        self.mom_14d = _pct_change(daily[-15], daily[-1]) if len(daily) >= 15 else 0.0
        self.mom_30d = _pct_change(daily[-31], daily[-1]) if len(daily) >= 31 else self.mom_14d

        # RSI on hourly closes (matches bot's interval)
        self.rsi = _wilder_rsi(hourly, 14) if len(hourly) >= 15 else 50.0

        # Volatility: std dev of recent hourly returns (annualized-ish)
        if len(hourly) >= 20:
            hr_returns = [
                hourly[i] / hourly[i - 1] - 1.0
                for i in range(max(1, len(hourly) - 48), len(hourly))
                if hourly[i - 1] > 0
            ]
            self.volatility = _std(hr_returns) * 100 if hr_returns else 0.0
        else:
            self.volatility = 0.0

        # Daily returns for correlation
        self.daily_returns = scanner.get_daily_returns(coin)

    def max_correlation(self, other_returns_list):
        """Max correlation with any set of daily return series."""
        if not self.daily_returns or not other_returns_list:
            return 0.0
        return max(
            abs(_correlation(self.daily_returns, r)) for r in other_returns_list
        ) if other_returns_list else 0.0

    def summary(self):
        """One-line summary for logging."""
        return (
            f"{self.coin}: 7d={self.mom_7d:+.1f}% 14d={self.mom_14d:+.1f}% "
            f"RSI={self.rsi:.0f} vol={self.volatility:.2f}% "
            f"qvol=${self.volume/1e6:.1f}M perp={'Y' if self.has_perp else 'N'}"
        )


# ── Regime scoring ───────────────────────────────────────────────────────────

def score_bull(m):
    """Score for BULL regime: momentum leaders with room to run."""
    if not m.has_data:
        return -999, "insufficient history"
    score = 0.0

    # Momentum is king in bull markets
    score += m.mom_7d * 1.8
    score += m.mom_14d * 0.8
    score += m.mom_30d * 0.3

    # Volume liquidity (log-scaled, capped)
    score += min(math.log10(max(m.volume, 1)) * 3, 15)

    # RSI sweet spot: 50-72 (strong but not exhausted)
    if m.rsi > 82:
        score -= 25
        reason = "overbought"
    elif m.rsi > 72:
        score -= 8
    elif 55 <= m.rsi <= 72:
        score += 12  # Ideal momentum zone
    elif m.rsi < 35:
        score -= 10  # Weak, not participating in rally

    # Volatility: moderate is good for rotation signals
    if m.volatility > 4.0:
        score -= 5  # Too chaotic
    elif m.volatility >= 1.0:
        score += 5  # Generates clean signals

    return score, None


def score_sideways(m):
    """Score for SIDEWAYS regime: oscillation quality."""
    if not m.has_data:
        return -999, "insufficient history"
    score = 0.0

    # Volume is primary (need liquid pairs for frequent rotation)
    score += min(math.log10(max(m.volume, 1)) * 3.5, 18)

    # Penalize strong trends (want oscillation, not directional)
    score -= abs(m.mom_7d) * 0.4
    score -= abs(m.mom_14d) * 0.15

    # Volatility: 1-4% hourly is ideal for rotation signals
    if m.volatility < 0.5:
        score -= 12  # Dead coin, no signals
    elif m.volatility <= 4.0:
        score += 10  # Sweet spot
    elif m.volatility > 6.0:
        score -= 5  # Too noisy

    # RSI mid-range is preferred
    if 35 <= m.rsi <= 65:
        score += 5

    return score, None


def score_bear(m):
    """Score for BEAR regime: short-ready targets."""
    if not m.has_data:
        return -999, "insufficient history"
    if not m.has_perp:
        return -999, "no USDC perp"

    score = 0.0

    # Negative momentum = good short target
    score += abs(min(m.mom_7d, 0)) * 2.0
    score += abs(min(m.mom_14d, 0)) * 1.0

    # Volume liquidity for shorting
    score += min(math.log10(max(m.volume, 1)) * 3, 15)

    # RSI: want 30-55 (room to fall, not oversold bounce territory)
    if m.rsi < 22:
        score -= 25  # Already oversold, squeeze risk
    elif m.rsi > 65:
        score -= 8  # Bouncing, risky short
    elif 30 <= m.rsi <= 55:
        score += 12  # Ideal short zone

    return score, None


def score_stormy(m):
    """Score for STORMY regime: defensive, high-liquidity only."""
    if not m.has_data:
        return -999, "insufficient history"
    score = 0.0

    # Prioritize volume above all
    score += min(math.log10(max(m.volume, 1)) * 4, 25)

    # Low volatility is better (safe haven)
    if m.volatility > 5.0:
        score -= 15
    elif m.volatility < 2.0:
        score += 8

    return score, None


SCORERS = {
    "bull": score_bull,
    "sideways": score_sideways,
    "bear": score_bear,
    "stormy": score_stormy,
}


def score_coin(m, regime):
    """Score a coin for the given regime. Returns (score, reject_reason)."""
    scorer = SCORERS.get(regime, score_sideways)
    return scorer(m)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    coins = get_enabled_coins()
    state = load_state()
    state["last_run"] = datetime.utcnow().isoformat()

    held_coin = get_held_coin()
    futures_coins = get_futures_symbols()
    regime = get_current_regime()
    protected = ({held_coin} if held_coin else set()) | futures_coins

    print(f"📊 Regime: {regime.upper()} | Active coins: {len(coins)}")
    if held_coin:
        print(f"📌 Holding: {held_coin} (protected)")
    if futures_coins:
        print(f"🔻 Futures open: {', '.join(sorted(futures_coins))} (protected)")

    exchange = ccxt.binance()
    scanner = MarketScanner()
    actions = []

    # ── CIRCUIT BREAKER ──
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"❌ ABORTED: Binance load_markets() failed: {e}")
        print("COIN_LIST_OK")
        return

    if len(markets) < 500:
        print(f"❌ ABORTED: {len(markets)} markets (< 500). API failure suspected.")
        print("COIN_LIST_OK")
        return

    # ── Batch fetch tickers ──
    try:
        all_tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"❌ ABORTED: fetch_tickers() failed: {e}")
        print("COIN_LIST_OK")
        return

    # ── Health check: current coins ──
    to_remove = []
    for coin in coins:
        pair = f"{coin}/{BRIDGE}"
        if pair not in markets or not markets[pair].get("active", False):
            actions.append(f"🚨 REMOVE {coin}: pair delisted/inactive")
            to_remove.append(coin)
            state["low_volume_days"].pop(coin, None)
            continue
        t = all_tickers.get(pair, {})
        vol = float(t.get("quoteVolume") or 0)
        if vol < VOLUME_REMOVE_THRESHOLD:
            actions.append(f"💀 REMOVE {coin}: volume ${vol:,.0f}")
            to_remove.append(coin)
            state["low_volume_days"].pop(coin, None)
        elif vol < VOLUME_WARN_THRESHOLD:
            days = state["low_volume_days"].get(coin, 0) + 1
            state["low_volume_days"][coin] = days
            if days >= LOW_VOLUME_DAYS_TO_REPLACE and len(coins) - len(to_remove) > MIN_COINS:
                actions.append(f"🔄 REPLACE {coin}: low vol ${vol:,.0f} for {days}d")
                to_remove.append(coin)
                state["low_volume_days"].pop(coin, None)
            elif days >= LOW_VOLUME_DAYS_TO_REPLACE:
                actions.append(f"⚠️ {coin}: low vol {days}d but at min coin count")
            # else: silently track
        else:
            state["low_volume_days"].pop(coin, None)

    # ── CIRCUIT BREAKER: never nuke >30% ──
    max_removals = max(2, int(len(coins) * 0.30))
    if len(to_remove) > max_removals:
        print(f"❌ ABORTED: {len(to_remove)} flagged (max {max_removals}). API failure?")
        save_state(state)
        print("COIN_LIST_OK")
        return

    # ── Protect held coin + futures ──
    for coin in list(to_remove):
        if coin in protected:
            actions.append(f"⏸️ SKIP {coin}: protected (held/futures)")
            to_remove.remove(coin)

    for coin in to_remove:
        db_disable_coin(coin)

    # ── Score current coins (for upgrade evaluation) ──
    remaining = [c for c in coins if c not in to_remove]
    current_metrics = {}
    current_returns = []

    for coin in remaining:
        pair = f"{coin}/{BRIDGE}"
        vol = float(all_tickers.get(pair, {}).get("quoteVolume") or 0)
        m = CoinMetrics(coin, scanner, vol)
        current_metrics[coin] = m
        if m.daily_returns:
            current_returns.append(m.daily_returns)

    if verbose and current_metrics:
        print("\n── Current coin scores ──")
        scored_list = []
        for coin, m in current_metrics.items():
            s, _ = score_coin(m, regime)
            scored_list.append((coin, s, m))
        scored_list.sort(key=lambda x: x[1], reverse=True)
        for coin, s, m in scored_list:
            print(f"  {coin:8s} score={s:7.1f} | {m.summary()}")

    # ── Discover candidates ──
    candidates_discovered = []
    for symbol_ticker, t in all_tickers.items():
        if not symbol_ticker.endswith(f"/{BRIDGE}"):
            continue
        coin = symbol_ticker.replace(f"/{BRIDGE}", "")
        if coin in remaining or coin in EXCLUDE_SYMBOLS:
            continue
        # Skip non-standard symbols (Chinese chars, special tokens, leveraged tokens)
        if not coin.isascii() or not coin.isalpha() or len(coin) > 12:
            continue
        vol = float(t.get("quoteVolume") or 0)
        if vol < VOLUME_WARN_THRESHOLD:
            continue
        pair = f"{coin}/{BRIDGE}"
        if pair not in markets or not markets[pair].get("active", False):
            continue
        candidates_discovered.append((coin, vol))

    candidates_discovered.sort(key=lambda x: x[1], reverse=True)
    # Evaluate top 25 candidates by volume
    candidates_to_score = candidates_discovered[:25]

    scored_candidates = []
    for coin, vol in candidates_to_score:
        m = CoinMetrics(coin, scanner, vol)
        if not m.has_data:
            continue
        score, reason = score_coin(m, regime)
        if reason:
            continue

        # Correlation check
        max_corr = m.max_correlation(current_returns) if current_returns else 0.0
        if max_corr > CORRELATION_THRESHOLD:
            continue

        scored_candidates.append({
            "coin": coin,
            "score": score,
            "metrics": m,
            "correlation": max_corr,
        })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)

    if verbose and scored_candidates:
        print(f"\n── Top 10 {regime.upper()} candidates ──")
        for c in scored_candidates[:10]:
            m = c["metrics"]
            print(
                f"  {c['coin']:8s} score={c['score']:7.1f} corr={c['correlation']:.2f} | "
                f"{m.summary()}"
            )
    elif verbose:
        print(f"\n── Candidates: {len(candidates_discovered)} discovered, {len(candidates_to_score)} scored, 0 passed filters ──")
        # Show why they were rejected
        if candidates_to_score:
            for coin, vol in candidates_to_score[:5]:
                m = CoinMetrics(coin, scanner, vol)
                score, reason = score_coin(m, regime)
                corr = m.max_correlation(current_returns) if current_returns else 0.0
                reject = reason or ('corr>{:.2f}'.format(corr) if corr > CORRELATION_THRESHOLD else None)
                print(f"  {coin:8s} score={score:7.1f} reject={reject or 'passed'} corr={corr:.2f} | {m.summary()}")

    # ── Fill removal slots ──
    removed_count = len(to_remove)
    added_coins = []
    if removed_count > 0 and len(remaining) < MAX_COINS:
        slots = min(removed_count, MAX_COINS - len(remaining))
        for cand in scored_candidates[:slots]:
            if db_enable_coin(cand["coin"]):
                m = cand["metrics"]
                actions.append(
                    f"➕ ADD {cand['coin']} (score {cand['score']:.1f}): "
                    f"7d={m.mom_7d:+.1f}% RSI={m.rsi:.0f} "
                    f"vol=${m.volume/1e6:.1f}M corr={cand['correlation']:.2f}"
                )
                added_coins.append(cand["coin"])
                state["coin_added_date"][cand["coin"]] = datetime.utcnow().strftime("%Y-%m-%d")

    # ── Upgrade pass: swap weakest coin for much better candidate ──
    if scored_candidates and len(remaining + added_coins) >= MIN_COINS:
        # Score all current coins
        current_scored = []
        for coin, m in current_metrics.items():
            if coin in protected or coin in added_coins:
                continue
            # Skip recently added coins (settle period)
            added_date = state.get("coin_added_date", {}).get(coin)
            if added_date:
                days_since = (datetime.utcnow() - datetime.strptime(added_date, "%Y-%m-%d")).days
                if days_since < SETTLE_DAYS_AFTER_ADD:
                    continue
            score, _ = score_coin(m, regime)
            if score > -900:
                current_scored.append({"coin": coin, "score": score, "metrics": m})

        if current_scored:
            current_scored.sort(key=lambda x: x["score"])
            weakest = current_scored[0]

            # Find best available candidate not already added
            for cand in scored_candidates:
                if cand["coin"] in added_coins:
                    continue
                if cand["coin"] in remaining:
                    continue
                if cand["score"] > weakest["score"] * UPGRADE_EDGE_MULT and cand["score"] > 0:
                    # Swap!
                    if db_disable_coin(weakest["coin"]):
                        db_enable_coin(cand["coin"])
                        wm = weakest["metrics"]
                        cm = cand["metrics"]
                        actions.append(
                            f"⬆️ UPGRADE {weakest['coin']} (score {weakest['score']:.1f}) → "
                            f"{cand['coin']} (score {cand['score']:.1f})"
                        )
                        state["coin_added_date"][cand["coin"]] = (
                            datetime.utcnow().strftime("%Y-%m-%d")
                        )
                        state["coin_added_date"].pop(weakest["coin"], None)
                    break  # Max 1 upgrade per run

    # ── Clean up state ──
    current_enabled = get_enabled_coins()
    state["low_volume_days"] = {
        k: v for k, v in state["low_volume_days"].items() if k in current_enabled
    }
    state["coin_added_date"] = {
        k: v for k, v in state.get("coin_added_date", {}).items() if k in current_enabled
    }
    save_state(state)

    # ── Output ──
    db_changes = [a for a in actions if any(k in a for k in ("REMOVE", "REPLACE", "ADD", "UPGRADE"))]
    if not actions:
        print(f"✅ All {len(coins)} coins healthy for {regime.upper()} regime.")
        print("COIN_LIST_OK")
    elif not db_changes:
        print(f"✅ All {len(coins)} coins healthy for {regime.upper()} regime.")
        for a in actions:
            print(f"  {a}")
        print("COIN_LIST_OK")
    else:
        print(f"\n🤖 {len(db_changes)} regime-aware action(s) [{regime.upper()}]:")
        for a in actions:
            print(f"  {a}")
        print(f"\n📋 Active: {len(current_enabled)} coins — {', '.join(sorted(current_enabled))}")
        print("COIN_LIST_CHANGED")


if __name__ == "__main__":
    main()
