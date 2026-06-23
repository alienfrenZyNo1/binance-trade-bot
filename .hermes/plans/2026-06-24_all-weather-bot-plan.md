# All-Weather Trading Bot — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Transform the bot from a single-strategy, spot-only, long-only coin rotator into a multi-regime, multi-strategy system that can short sell via Binance Futures, earn yield on idle capital, and make money (or at minimum lose far less) in ALL market conditions.

**Architecture:** Phased rollout — each phase produces a working, deployable bot. Phase 1 improves the existing spot bot. Phase 2 adds Binance Futures support. Phase 3 adds passive yield. Phase 4 wires everything into a unified all-weather engine.

**Tech Stack:** Python 3.11, python-binance (upgrade from 1.0.27 to 1.0.19+ for futures), SQLAlchemy 1.4, SQLite, Docker/Coolify. No ccxt needed — python-binance handles both spot and USDC-M futures.

**Estimated Effort:** 4 phases × ~10-15 tasks each = 40-60 tasks total across 2-3 weeks of development.

**HARD CONSTRAINT: Binance EU (Ireland) does NOT support USDT.** The bot MUST use USDC as bridge currency and USDC-M Perpetual Futures only. USDT pairs, USDT-M futures, and any USDT conversion are impossible. All code must use `config.BRIDGE.symbol` (USDC), never hardcode USDT. This constraint eliminates 11 of 15 coins from futures trading.

---

## Current State Summary

- **Bot location:** `REDACTED_PATH` (Docker on Coolify at `REDACTED_IP`)
- **Strategy:** `momentum` — pure momentum rotation, skips bear markets entirely
- **Balance:** ~$62 (USDC bridge), 15 altcoins
- **Key files:**
  - `binance_trade_bot/auto_trader.py` (192 lines) — base trader class
  - `binance_trade_bot/crypto_trading.py` — main loop + scheduler
  - `binance_trade_bot/binance_api_manager.py` (602 lines) — spot-only API
  - `binance_trade_bot/binance_stream_manager.py` (192 lines) — spot-only WebSocket
  - `binance_trade_bot/strategies/momentum_strategy.py` (485 lines) — current strategy
  - `binance_trade_bot/strategies/improved_strategy.py` (1047 lines) — unused adaptive strategy
  - `binance_trade_bot/config.py` (270 lines) — configparser-based
  - `binance_trade_bot/database.py` — SQLAlchemy + SQLite
  - `binance_trade_bot/indicators.py` — ADX, RSI, EMA, Bollinger
  - `user.cfg` (119 lines) — runtime config
  - `docker-entrypoint.sh` — copies config, exports env vars

**Architectural constraints discovered:**
1. Single-position model — always holds exactly one coin or bridge
2. Spot-only — no futures/margin anywhere in codebase
3. Single-strategy — `get_strategy(name)` returns one class
4. `python-binance==1.0.27` — very old, limited futures support
5. WebSocket manager is spot-only (`!miniTicker`, `!userData`)
6. `CurrentCoin` model tracks ONE coin — entire system assumes this
7. `OrderGuard` serializes to one order at a time

---

## Phase 1: Improve Existing Spot Bot (Low Risk, High Value)

**Goal:** Get the current momentum strategy performing better — fewer losses in sideways, better regime detection, reduced whipsaw. No new architecture, just tuning.

### Task 1.1: Reduce Sideways Whipsaw Losses

**Objective:** Stop the bot from making losing trades during sideways/choppy markets where momentum signals produce false positives.

**Files:**
- Modify: `binance_trade_bot/strategies/momentum_strategy.py:323-360`

**Changes:**
- Add ADX filter: if ADX < 18 (no clear trend), skip ALL trades (not just bear). Currently only bear is skipped. Low ADX = sideways = momentum doesn't work.
- Add volatility squeeze filter: if Bollinger Band width is in the lowest 20th percentile of the last 50 candles, skip trades (no trend has formed yet).
- These are in addition to the existing regime (bear) skip.

**Code location:** In `momentum_strategy.py`, after `_update_market_regime()` at line 324 and before the momentum scoring section:

```python
# NEW: Sideways filter — skip when no clear trend
if self._regime_adx < getattr(self.config, 'MOMENTUM_MIN_ADX', 18):
    return

# NEW: Squeeze filter — skip during volatility compression
bb_width = self._get_bb_width(ref_coin)  # new helper method
if bb_width < getattr(self.config, 'MOMENTUM_SQUEEZE_THRESHOLD', 0.5):
    return
```

**Verification:** Run the optimizer backtest with these filters and compare trade count + P&L. Expect: fewer trades, slightly lower bull returns, significantly lower sideways losses.

---

### Task 1.2: Improve Regime Detection Accuracy

**Objective:** Current regime detection uses ADX + EMA + DI on SOL only. Add BTC confirmation and make detection more robust to reduce false bear/bull switches.

**Files:**
- Modify: `binance_trade_bot/strategies/momentum_strategy.py:74-138` (`_update_market_regime`)

**Changes:**
- Add BTC regime as a confirming signal: only switch to BEAR if BOTH SOL and BTC show bearish signals (currently only SOL checked).
- Add hysteresis: require 2 consecutive regime readings before switching (prevents rapid flip-flopping). Currently switches on every check if conditions change.
- Add volume confirmation: if 24h volume is declining, don't switch to BULL even if price looks bullish (low-volume pumps are traps).

**Verification:** Compare regime log history before/after. Should see fewer regime switches and longer periods in each regime.

---

### Task 1.3: Smarter Trailing Stop

**Objective:** Current trailing stop is a flat 15% from peak. Make it adaptive — tighter in sideways (protect small gains), looser in strong bull (let winners run).

**Files:**
- Modify: `binance_trade_bot/strategies/momentum_strategy.py` (trailing stop section)

**Changes:**
- Regime-aware trailing stop:
  - Bull: 20% (let winners run)
  - Sideways: 8% (protect small gains)
  - Bear: N/A (no positions in bear)
- Time-based tightening: if a position has been open > 24h, tighten trail by 2% per additional day (old positions are more likely to reverse).

**Config additions in `user.cfg`:**
```
trailing_stop_bull=20
trailing_stop_sideways=8
trailing_stop_tighten_rate=2
```

**Verification:** Backtest with adaptive stops vs flat 15%. Expect lower max drawdown with similar or better P&L.

---

### Task 1.4: Fee-Aware Trade Decisions

**Objective:** The bot currently trades when it detects a momentum edge of 8%. But at $62 with 0.075% fees per side (0.15% round trip), some trades are below the profit threshold after fees. Make the bot fee-aware.

**Files:**
- Modify: `binance_trade_bot/strategies/momentum_strategy.py` (momentum scoring section)

**Changes:**
- Calculate expected fee cost for the proposed trade: `fee = balance * 0.0015` (0.075% × 2 sides)
- Require `expected_gain > fee * 3` before executing (3x fee buffer for slippage)
- This replaces the static `MOMENTUM_MIN_EDGE=8.0` with a dynamic threshold based on position size

**Verification:** Compare trade count. Should see fewer small trades that were barely profitable after fees.

---

### Task 1.5: Backtest All Phase 1 Changes Together

**Objective:** Validate that Phase 1 changes improve overall performance vs the current live config.

**Files:**
- Modify: `optimize_momentum.py` (add new filters to backtest engine)

**Changes:**
- Add ADX filter, squeeze filter, adaptive trailing stop, and fee-aware threshold to the backtest engine
- Run full 180-day backtest with train/OOS split
- Compare: current config vs Phase 1 config on both P&L, max drawdown, Sharpe, trade count, and fee percentage
- Record results for decision: deploy if OOS P&L improves or max drawdown drops >10%

**Expected outcome:**
- Fewer total trades (less whipsaw)
- Lower max drawdown (adaptive stops + sideways filter)
- Similar or slightly better P&L (fewer losing trades offsets fewer winning trades)
- Lower fee percentage of portfolio

---

**Goal:** Add ability to open SHORT positions via Binance USDC-M Perpetual Futures. This is the ONLY way to genuinely profit in bear markets.

**HARD CONSTRAINT — Ireland/Binance EU: USDT is not supported. All futures MUST be USDC-M Perpetuals.**

This eliminates several approaches:
- ❌ `ccxt.binanceusdm` — this is USDT-M only, cannot be used
- ❌ Any USDT conversion step — USDT pairs are not tradeable from this account
- ❌ Migrating bridge to USDT — not possible for Ireland
- ✅ `python-binance` Futures API (supports both USDT-M and USDC-M)
- ✅ `ccxt.binance` with `options.defaultType='future'` targeting USDC-M specifically (needs verification)

**USDC-M Perpetual Futures — Available pairs (as of 2026):**
Only a subset of coins have USDC-M perpetuals on Binance. Of the bot's 15 coins:

| Coin | USDC-M Perpetual | Liquidity |
------|-----------------|----------|
| BTC | ✅ | Excellent |
| ETH | ✅ | Excellent |
| SOL | ✅ | Good |
| ADA | ✅ | Moderate |
| LINK | ✅ | Moderate |
| XRP | ✅ | Good |
| AVAX | ✅ | Moderate |
| DOGE | ❌ | — |
| NEAR | ❌ | — |
| APT | ❌ | — |
| SUI | ❌ | — |
| INJ | ❌ | — |
| AAVE | ❌ | — |
| PEPE | ❌ | — |
| TIA | ❌ | — |
| ENA | ❌ | — |
| JUP | ❌ | — |

**Result: Futures universe is 7 coins (BTC, ETH, SOL, ADA, LINK, XRP, AVAX).** This is a more conservative but still viable set for momentum-based shorting.

**CRITICAL DECISION: python-binance Futures API vs ccxt?**

| Option | Pros | Cons |
|--------|------|------|
| Use python-binance Futures API (`Client` with futures methods) | Same library as spot, no new dependency, same auth patterns, supports USDC-M | python-binance 1.0.27 is old — must upgrade to 1.0.19+ for full futures support |
| Use ccxt `binance` with USDC-M config | Actively maintained, unified API | ccxt's USDC-M support needs verification — may not distinguish USDC-M from USDT-M correctly |
| Use raw Binance REST API for futures | Full control, no library limitations | More code to write, must handle signing/nonce ourselves |

**Recommendation: Upgrade python-binance to latest (1.0.19+) and use its Futures API for everything.** This is the safest path because:
1. Same library = same auth, same error handling patterns, shared retry logic
2. python-binance's `Client` supports both spot and futures with a single instance
3. No dependency conflicts (no ccxt needed at all)
4. USDC-M futures are supported in newer python-binance versions
5. Single API key works for both spot and futures (no cross-contamination risk with separate libraries)

**Upgrade path:** `pip install python-binance>=1.0.19` — verify spot still works, then add futures methods.

### Task 2.1: Upgrade python-binance
**Objective:** Upgrade python-binance from 1.0.27 to latest (1.0.19+) to enable USDC-M Futures API support.

**Files:**
- Modify: `requirements.txt`
- Modify: `binance_trade_bot/binance_api_manager.py` (add futures client)

**Step 1: Upgrade in requirements.txt**
```
python-binance>=1.0.19
```

**Step 2: Test spot still works after upgrade**
```bash
docker exec crypto-trading python -c "
from binance.client import Client
c = Client('test','test')
print('python-binance version:', c.session.headers.get('client'))
# Verify all existing spot methods still exist
assert hasattr(c, 'get_symbol_ticker')
assert hasattr(c, 'order_limit_buy')
assert hasattr(c, 'order_limit_sell')
print('Spot API OK')
# Verify futures methods exist
assert hasattr(c, 'futures_position_information')
assert hasattr(c, 'futures_create_order')
print('Futures API OK')
"
```

**Step 3: Verify USDC-M futures endpoint works**
```python
# python-binance futures uses the same Client but with futures-specific methods
# USDC-M pairs are accessed via symbol format: BTCUSDC (same as spot)
# The Futures endpoint URL differs: https://fapi.binance.com (USDT-M) vs https://dapi.binance.com (COIN-M)
# USDC-M uses the DAPI (Delivery/Options) endpoint or may be accessible via a specific parameter
# MUST VERIFY: Check if python-binance supports USDC-M futures specifically
```

**⚠️ VERIFICATION REQUIRED:** Before proceeding, confirm that python-binance can access Binance's USDC-M Perpetual endpoint. The endpoint may differ from standard futures. If python-binance cannot access USDC-M, fall back to raw REST API calls to `https://fapi.binance.com` with USDC pair symbols, or use `ccxt.binance` with manual market discovery for USDC-M pairs.

**If python-binance USDC-M support is insufficient, the fallback is:**
```python
# Direct REST API calls wrapped in a thin manager class
import hmac, hashlib, time, requests

class BinanceUSDCMFutures:
    BASE_URL = "https://fapi.binance.com"  # or /dapi/ — verify which handles USDC-M
    
    def _sign(self, params):
        params['timestamp'] = int(time.time() * 1000)
        query = '&'.join(f'{k}={v}' for k, v in sorted(params.items()))
        signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        return query + '&signature=' + signature
    
    def create_order(self, symbol, side, type, quantity, **kwargs):
        # symbol format: "BTCUSDC" for USDC-M
        params = {'symbol': symbol, 'side': side, 'type': type, 'quantity': quantity}
        params.update(kwargs)
        signed = self._sign(params)
        return requests.post(f'{self.BASE_URL}/fapi/v1/order', params=signed, headers={'X-MBX-APIKEY': self.api_key})
```

**Files:**
- Modify: `requirements.txt`
- Create: `binance_trade_bot/futures_api_manager.py`


---

### Task 2.2: Create Futures API Manager

**Objective:** Build a dedicated API manager for Binance USDC-M Perpetual Futures.

**Files:**
- Create: `binance_trade_bot/futures_api_manager.py`
- Create: `binance_trade_bot/futures_stream_manager.py`

**`futures_api_manager.py` should implement:**

```python
class FuturesAPIManager:
    """Binance USDC-M Perpetual Futures API."""
    
    def __init__(self, config, logger):
        # Use python-binance futures client (upgraded version)
        self.client = Client(
            config.BINANCE_API_KEY,
            config.BINANCE_API_SECRET_KEY,
        )
        self.client.FUTURES_URL = config.FUTURES_API_URL  # USDC-M endpoint
        self.leverage = config.FUTURES_LEVERAGE  # default 1
        self.margin_type = config.FUTURES_MARGIN_TYPE  # default "isolated"
    
    def set_leverage(self, symbol, leverage):
        """Set leverage for a futures pair."""
        self.client.futures_change_leverage(
            symbol=symbol,
            leverage=leverage
        )
    
    def set_margin_mode(self, symbol, margin_type):
        """Set ISOLATED or CROSS margin."""
        self.client.futures_change_margin_type(
            symbol=symbol,
            marginType=margin_type
        )
    
    def open_long(self, symbol, usd_amount):
        """Open a long position. Returns position info."""
        price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
        quantity = usd_amount / price  # simplified
        # Round to futures lot size
        quantity = self._round_to_lot_size(symbol, quantity)
        order = self.client.futures_create_order(
            symbol=symbol,  # e.g., "BTCUSDC" for USDC-M
            side='BUY',
            type='MARKET',
            quantity=quantity
        )
        return order
    
    def open_short(self, symbol, usd_amount):
        """Open a SHORT position. The key new capability."""
        price = float(self.client.get_symbol_ticker(symbol=symbol)['price'])
        quantity = usd_amount / price
        quantity = self._round_to_lot_size(symbol, quantity)
        order = self.client.futures_create_order(
            symbol=symbol,
            side='SELL',
            type='MARKET',
            quantity=quantity
        )
        return order
    
    def close_long(self, symbol, quantity=None):
        """Close a long position (sell to exit)."""
        if quantity is None:
            pos = self.get_position(symbol)
            quantity = abs(pos['positionAmt'])
        order = self.client.futures_create_order(
            symbol=symbol, side='SELL', type='MARKET', quantity=quantity
        )
        return order
    
    def close_short(self, symbol, quantity=None):
        """Close a short position (buy to exit)."""
        if quantity is None:
            pos = self.get_position(symbol)
            quantity = abs(pos['positionAmt'])
        order = self.client.futures_create_order(
            symbol=symbol, side='BUY', type='MARKET', quantity=quantity
        )
        return order
    
    def get_position(self, symbol):
        """Get current position for a symbol. Returns dict with size, entry, unrealizedPnl."""
        positions = self.client.futures_position_information(symbol=symbol)
        for p in positions:
            if abs(float(p['positionAmt'])) > 0:
                return p
        return {'positionAmt': 0, 'entryPrice': 0, 'unrealizedPnl': 0}
    
    def get_balance(self):
        """Get available USDC margin balance."""
        account = self.client.futures_account_balance()
        for asset in account:
            if asset['asset'] == 'USDC':
                return float(asset['availableBalance'])
        return 0.0
    
    def get_ticker(self, symbol):
        """Get current price."""
        return self.client.get_symbol_ticker(symbol=symbol)
    
    def get_funding_rate(self, symbol):
        """Get current funding rate (important for short holding costs)."""
        return self.client.futures_funding_rate(symbol=symbol)
    
    def _round_to_lot_size(self, symbol, quantity):
        """Round quantity to futures lot size requirements."""
        info = self.client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                step_size = float([f for f in s['filters'] if f['filterType'] == 'LOT_SIZE'][0]['stepSize'])
                precision = max(int(round(-math.log10(step_size))), 0)
                return round(quantity, precision)
        return round(quantity, 2)
```

**`futures_stream_manager.py` should implement:**
- Subscribe to futures WebSocket streams (`wss://fstream.binance.com/ws`)
- Track unrealized PnL for open positions
- Liquidation price monitoring (CRITICAL for safety)
- Position update events

---

### Task 2.3: Futures Position Data Model

**Objective:** Create database tables to track futures positions separately from spot holdings.

**Files:**
- Create: `binance_trade_bot/models/futures_position.py`
- Modify: `binance_trade_bot/database.py` (register new model)

**Model:**
```python
class FuturesPosition(Base):
    __tablename__ = 'futures_positions'
    
    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)      # e.g., "SOL/USDC:USDC"
    side = Column(String(10), nullable=False)        # "LONG" or "SHORT"
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    leverage = Column(Integer, nullable=False)       # 1, 2, 3, 5, 10
    margin_type = Column(String(10), nullable=False) # "ISOLATED" or "CROSS"
    margin_used = Column(Float, nullable=False)       # USDC margin allocated
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    exit_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    fee_paid = Column(Float, default=0.0)
    status = Column(String(10), default="OPEN")       # "OPEN" or "CLOSED"
    close_reason = Column(String(30), nullable=True) # "SIGNAL", "STOP_LOSS", "TAKE_PROFIT", "LIQUIDATION", "MANUAL"
```

**Migration:** Add the table to existing database without breaking anything. SQLAlchemy `create_all()` handles this.

---

### Task 2.4: Futures Config Values

**Objective:** Add futures-related configuration to the config system.

**Files:**
- Modify: `binance_trade_bot/config.py` (add ~10 new settings)
- Modify: `user.cfg` (add futures block)

**New config values:**
```python
# In config.py, add after existing settings:
self.TRADE_MODE = os.environ.get("TRADE_MODE") or config.get(SECTION, "trade_mode", fallback="spot")
# "spot" (default, current behavior) or "futures" or "hybrid" (both spot + USDC-M futures)

self.FUTURES_ENABLED = self.TRADE_MODE in ("futures", "hybrid")
self.FUTURES_LEVERAGE = int(os.environ.get("FUTURES_LEVERAGE") or config.get(SECTION, "futures_leverage", fallback="1"))
self.FUTURES_MARGIN_TYPE = os.environ.get("FUTURES_MARGIN_TYPE") or config.get(SECTION, "futures_margin_type", fallback="isolated")
self.FUTURES_MAX_POSITION_PCT = float(os.environ.get("FUTURES_MAX_POSITION_PCT") or config.get(SECTION, "futures_max_position_pct", fallback="0.25"))
self.FUTURES_MAX_TOTAL_EXPOSURE = float(os.environ.get("FUTURES_MAX_TOTAL_EXPOSURE") or config.get(SECTION, "futures_max_total_exposure", fallback="0.5"))
self.FUTURES_STOP_LOSS_PCT = float(os.environ.get("FUTURES_STOP_LOSS_PCT") or config.get(SECTION, "futures_stop_loss_pct", fallback="10"))
self.FUTURES_FUNDING_RATE_THRESHOLD = float(os.environ.get("FUTURES_FUNDING_RATE_THRESHOLD") or config.get(SECTION, "futures_funding_rate_threshold", fallback="0.01"))
self.FUTURES_TESTNET = os.environ.get("FUTURES_TESTNET", "false").lower() == "true"
self.FUTURES_ALLOWED_COINS = os.environ.get("FUTURES_ALLOWED_COINS") or config.get(SECTION, "futures_allowed_coins", fallback="BTC,ETH,SOL")
self.FUTURES_API_URL = os.environ.get("FUTURES_API_URL") or config.get(SECTION, "futures_api_url", fallback="https://fapi.binance.com")
# NOTE: USDC-M futures endpoint may differ from USDT-M. Verify before deployment.
```

**In `user.cfg`:**
```ini
# ── Futures Settings (Phase 2) ─────────────────────────────────────────────
# Trade mode: spot (current), futures, hybrid (both)
trade_mode=spot

# Futures leverage (1 = no leverage, recommended starting point)
futures_leverage=1

# Margin type: isolated (per-position risk) or cross (portfolio margin)
futures_margin_type=isolated

# Max % of portfolio per single futures position
futures_max_position_pct=25

# Max total futures exposure as % of portfolio
futures_max_total_exposure=50

# Stop loss % for futures positions
futures_stop_loss_pct=10

# Max funding rate to pay (avoid expensive shorts during bullish funding)
futures_funding_rate_threshold=0.01

# Start on testnet first!
futures_testnet=true

# Coins allowed for futures trading (USDC-M perpetuals ONLY — USDT not available in IE)
# Restricted to coins with USDC-M futures: BTC, ETH, SOL, ADA, LINK, XRP, AVAX
futures_allowed_coins=BTC,ETH,SOL,ADA,LINK,XRP,AVAX
```

---

### Task 2.5: Futures Momentum Strategy

**Objective:** Create a momentum strategy that can SHORT in bear markets. This is the core value proposition.

**Files:**
- Create: `binance_trade_bot/strategies/momentum_futures_strategy.py`

**Design:**
```
Regime Detection (same as spot, but on futures pairs):
  ┌─ BULL: Open LONG on strongest momentum coin
  ├─ BEAR: Open SHORT on weakest momentum coin  ← THE KEY DIFFERENCE
  └─ SIDEWAYS: Close all positions, sit in USDC margin
```

**Key behavior differences from spot strategy:**
- **Bull:** Same as current — open long on highest momentum score
- **Bear:** NEW — open short on lowest momentum score (most negative momentum = most likely to keep falling)
- **Sideways:** Close everything, do nothing
- **Position sizing:** `usd_amount = portfolio_balance * FUTURES_MAX_POSITION_PCT / 100`
- **Risk management:** Hard stop-loss at -FUTURES_STOP_LOSS_PCT%, check liquidation price, monitor funding rates
- **Entry logic:** Only short when momentum score < -threshold AND regime == BEAR for >2 checks (confirmation)

**Skeleton:**
```python
class Strategy(AutoTrader):
    def __init__(self, *args):
        super().__init__(*args)
        self.futures_manager = FuturesAPIManager(self.config, self.logger)
        self._market_regime = SIDEWAYS
        self._open_position = None  # track current futures position
        self._regime_checks = []    # for confirmation
    
    def scout(self):
        self._update_market_regime()
        
        # Check existing position first
        if self._open_position:
            if self._should_close_position():
                self._close_position()
                return
        
        # Sideways: do nothing
        if self._market_regime == SIDEWAYS:
            return
        
        # Calculate momentum scores
        scores = self._get_momentum_scores()
        if not scores:
            return
        
        if self._market_regime == BULL:
            best = max(scores, key=lambda x: x['score'])
            if best['score'] > self.config.FUTURES_MIN_EDGE:
                self._open_long(best['symbol'])
        
        elif self._market_regime == BEAR:
            worst = min(scores, key=lambda x: x['score'])
            if worst['score'] < -self.config.FUTURES_MIN_EDGE:
                self._open_short(worst['symbol'])
    
    def _open_long(self, symbol):
        amount = self.futures_manager.get_balance() * (self.config.FUTURES_MAX_POSITION_PCT / 100)
        self.futures_manager.set_leverage(symbol, self.config.FUTURES_LEVERAGE)
        order = self.futures_manager.open_long(symbol, amount)
        self._open_position = {'symbol': symbol, 'side': 'LONG', 'entry': ...}
        self.db.log_futures_position(...)
    
    def _open_short(self, symbol):
        # Check funding rate first — don't short when funding is very positive
        funding = self.futures_manager.get_funding_rate(symbol)
        if funding['rate'] > self.config.FUTURES_FUNDING_RATE_THRESHOLD:
            return  # Too expensive to short
        amount = self.futures_manager.get_balance() * (self.config.FUTURES_MAX_POSITION_PCT / 100)
        self.futures_manager.set_leverage(symbol, self.config.FUTURES_LEVERAGE)
        order = self.futures_manager.open_short(symbol, amount)
        self._open_position = {'symbol': symbol, 'side': 'SHORT', 'entry': ...}
```

---

### Task 2.6: Futures Safety Guardrails

**Objective:** Implement critical safety measures for futures trading. Futures can blow up an account fast — this is NON-NEGOTIABLE.

**Files:**
- Create: `binance_trade_bot/futures_risk_manager.py`

**Guardrails to implement:**

```python
class FuturesRiskManager:
    MAX_DAILY_LOSS_PCT = 5       # Kill switch if daily P&L drops 5%
    MAX_DRAWDOWN_PCT = 15        # Kill switch if total drawdown hits 15%
    MAX_POSITION_AGE_HOURS = 168 # Force close positions older than 7 days
    MAX_CONSECUTIVE_LOSSES = 3   # Cooldown after 3 losing trades in a row
    MIN_BALANCE_USD = 10         # Stop trading if balance drops below $10
    LIQUIDATION_BUFFER_PCT = 20  # Alert when within 20% of liquidation price
    
    def check_before_open(self, symbol, side, amount):
        """Run all pre-trade checks. Return (allowed: bool, reason: str)."""
        checks = [
            self._check_balance(),
            self._check_daily_loss(),
            self._check_drawdown(),
            self._check_consecutive_losses(),
            self._check_position_limit(),
            self._check_exposure_limit(),
        ]
        failed = [r for allowed, r in checks if not allowed]
        if failed:
            return False, "; ".join(failed)
        return True, "OK"
    
    def check_existing_position(self, position):
        """Run position health checks. Return (action: str, reason: str)."""
        checks = [
            self._check_stop_loss(position),
            self._check_liquidation_buffer(position),
            self._check_position_age(position),
            self._check_funding_cost(position),
        ]
        # Return most urgent action
        actions = [(action, reason) for action, reason in checks if action != "HOLD"]
        if actions:
            return actions[0]
        return "HOLD", "Position healthy"
```

**Safety features:**
1. **Daily loss limit:** If futures P&L drops 5% in 24h, close all positions and pause trading for 24h
2. **Max drawdown:** If total futures drawdown hits 15%, full shutdown — requires manual restart
3. **Liquidation buffer:** Monitor distance to liquidation price. If within 20%, force-close the position
4. **Funding rate guard:** If funding rate flips very positive while you're short, close the short (it's costing too much to hold)
5. **Position age limit:** Force close after 7 days to prevent stale positions
6. **Balance floor:** If total balance drops below $10, stop all futures trading

---

### Task 2.7: Paper Trading / Testnet First

**Objective:** Deploy futures strategy on Binance Futures Testnet BEFORE any real money. Run for minimum 2 weeks to validate.

**Files:**
- Modify: `user.cfg` (futures_testnet=true)
- Modify: `binance_trade_bot/futures_api_manager.py` (testnet URL)

**Steps:**
1. Set `futures_testnet=true` in config
2. ccxt testnet connection: `exchange.set_sandbox_mode(True)`
3. Deploy to Docker, monitor for 2 weeks
4. Track: number of trades, P&L (paper), max drawdown, regime switches, stop-loss triggers
5. Only proceed to mainnet if:
   - No liquidations or near-liquidations
   - Max drawdown < 10%
   - Strategy correctly identifies and shorts in bear conditions
   - No unexpected errors or orphaned positions

**This is the most important task in Phase 2. Do NOT skip it.**

---

### Task 2.8: Gradual Mainnet Rollout

**Objective:** Move from testnet to mainnet with minimal capital at risk.

**Steps:**
1. Transfer only $15-20 USDC to Binance Futures wallet
2. Set `futures_leverage=1` (no leverage — just direction)
3. Set `futures_max_position_pct=20` (max $3-4 per trade)
4. Enable `TRADE_MODE=hybrid` (spot continues as normal, futures runs alongside)
5. Monitor daily for 2 weeks
6. Gradually increase: $30 balance → 25% position → 2x leverage if results are positive

---

## Phase 3: Passive Yield on Idle Capital

**Goal:** Earn yield when the bot is sitting in USDC during bear/sideways markets. Currently earns 0% on idle capital.

### Task 3.1: Binance Savings Integration

**Objective:** Auto-deposit idle USDC into Binance Flexible Savings (3-8% APY) when the bot isn't actively trading.

**Files:**
- Create: `binance_trade_bot/yield_manager.py`

**Design:**
```python
class YieldManager:
    """Manages Binance savings/deposit products for idle capital."""
    
    def __init__(self, spot_manager, config, logger):
        self.manager = spot_manager
        self.config = config
        self.logger = logger
    
    def deposit_savings(self, amount_usdc):
        """Deposit idle USDC into Binance flexible savings."""
        # Binance API: POST /sapi/v1/lending/daily/deposit
        # Product: "USDC" flexible, auto-subscribe
        product_id = self._get_usdc_savings_product_id()
        result = self.manager.client.post(
            '/sapi/v1/lending/daily/deposit',
            params={'productId': product_id, 'amount': amount_usdc}
        )
        return result
    
    def redeem_savings(self, amount_usdc=None):
        """Redeem USDC from savings when needed for trading."""
        # Binance API: POST /sapi/v1/lending/daily/redeem
        # "FAST" redeem available for flexible products
        result = self.manager.client.post(
            '/sapi/v1/lending/daily/redeem',
            params={'productId': product_id, 'amount': amount_usdc, 'type': 'FAST'}
        )
        return result
    
    def get_savings_balance(self):
        """Check how much USDC is in savings."""
        result = self.manager.client.get(
            '/sapi/v1/lending/union/account',
            params={'type': 'DAILY'}
        )
        return result
```

**Integration logic in strategy:**
- When regime = BEAR or SIDEWAYS, and balance > $10, deposit 80% into savings
- Keep 20% as dry powder for potential trades
- When regime switches to BULL, redeem from savings and start trading
- Expected return: ~$3-5/year on $62 balance — not huge, but it's free money

---

### Task 3.2: Dual Investment (Optional Enhancement)

**Objective:** Use Binance Dual Investment to sell "puts" — earn yield while waiting to buy coins cheaper in bear markets.

**Files:**
- Extend: `binance_trade_bot/yield_manager.py`

**Design:**
- When regime = BEAR: deposit USDC into Dual Investment product
- Strike price: current BTC/SOL price - 10%
- Duration: 7-14 days
- Outcome: Either keep USDC + earn yield (if price stays above strike) OR buy BTC/SOL at 10% discount (if price drops below strike)
- This is *bear-market-friendly income*: you either earn yield or buy the dip automatically
- Max allocation: 20% of portfolio per Dual Investment product

**API:** `POST /sapi/v1/lending/auto-invest/plan/editPosition` — need to verify exact endpoint.

---

## Phase 4: Unified All-Weather Engine

**Goal:** Wire everything together into a single system that automatically switches strategies based on market conditions.

### Task 4.1: Multi-Strategy Orchestrator

**Objective:** Create a strategy orchestrator that runs multiple strategies simultaneously and switches between them based on regime.

**Files:**
- Create: `binance_trade_bot/strategy_orchestrator.py`
- Modify: `binance_trade_bot/crypto_trading.py` (use orchestrator instead of single strategy)

**Design:**
```python
class StrategyOrchestrator:
    """Runs the appropriate strategy based on market regime."""
    
    def __init__(self, config, db, logger, spot_manager, futures_manager=None):
        self.strategies = {
            'bull': MomentumSpotStrategy(spot_manager, db, logger, config),
            'bear': MomentumFuturesStrategy(futures_manager, db, logger, config),
            'sideways': HoldStrategy(spot_manager, db, logger, config),  # USDC + yield
        }
        self.current_regime = SIDEWAYS
        self.yield_manager = YieldManager(spot_manager, config, logger)
    
    def scout(self):
        regime = self._detect_regime()
        
        if regime != self.current_regime:
            self._handle_regime_switch(self.current_regime, regime)
            self.current_regime = regime
        
        self.strategies[regime].scout()
        self.yield_manager.manage()  # deposit/redeem as needed
```

**Regime switch logic:**
```
BULL → BEAR:
  1. Close all spot positions → USDC
  2. Activate futures short strategy
  3. Deposit idle USDC into savings

BEAR → BULL:
  1. Close all futures short positions
  2. Redeem from savings
  3. Activate spot momentum strategy

ANY → SIDEWAYS:
  1. Close all active positions
  2. Deposit into savings
  3. Wait

SIDEWAYS → ANY:
  1. Redeem from savings
  2. Activate appropriate strategy
```

---

### Task 4.2: Unified Dashboard & Monitoring

**Objective:** Extend the existing Flask dashboard to show futures positions, yield balances, regime state, and combined P&L.

**Files:**
- Modify: `dashboard/` (existing Flask app)

**New dashboard elements:**
- Regime indicator (BULL/BEAR/SIDEWAYS) with confidence score
- Spot holdings value
- Futures positions (side, size, entry, P&L)
- Savings balance + APY earned
- Combined portfolio value (spot + futures + savings)
- Trade history (spot + futures combined)
- Risk metrics (daily P&L, drawdown, liquidation distance)

---

### Task 4.3: Telegram Bot Enhancement

**Objective:** Update the companion Telegram bot (`scripts/telegram_bot.py`) to show futures and yield info.

**Files:**
- Modify: `scripts/telegram_bot.py`

**New commands:**
- `/futures` — show open futures positions, P&L, funding rates
- `/yield` — show savings balance, interest earned
- `/regime` — show current regime, ADX, confidence score
- `/risk` — show daily loss, drawdown, liquidation distance
- `/all` — combined view of everything

---

### Task 4.4: Full System Backtest

**Objective:** Backtest the entire all-weather system across a full market cycle (bull → bear → sideways → bull).

**Files:**
- Create: `backtest_all_weather.py`

**Backtest design:**
- Fetch 365 days of hourly data for BTC, ETH, SOL (futures candidates) + all 15 spot coins
- Split into 4 quarters with known regime labels
- Simulate:
  - Q1 (bull): Spot momentum rotation
  - Q2 (bear): Futures shorting + USDC savings yield
  - Q3 (sideways): USDC savings yield only
  - Q4 (recovery): Spot momentum rotation
- Calculate: total P&L, per-regime P&L, max drawdown, Sharpe ratio, fees paid, yield earned
- Compare against: buy-and-hold TIA, buy-and-hold BTC, current spot-only bot

---

### Task 4.5: Safety Kill Switch

**Objective:** Implement a master kill switch that can shut down all trading (spot + futures) and move everything to USDC with one command.

**Files:**
- Create: `binance_trade_bot/emergency_stop.py`
- Modify: `scripts/telegram_bot.py` (add /emergency command)

**Behavior:**
1. Close all spot positions → USDC
2. Close all futures positions (long and short) → USDC margin
3. Redeem all savings → USDC spot
4. Stop the scheduler (no new trades)
5. Send Telegram alert
6. Log the emergency event to DB
7. Require manual restart to resume

**Telegram command:** `/emergency` or `/stop` — requires confirmation via `/confirm`

---

## Deployment Plan

### Phase 1 Deployment (Immediate)
- Changes are backwards-compatible
- Deploy to Docker: rebuild image, restart container
- Monitor: trade frequency, P&L, regime log
- Rollback: revert to `strategy=momentum` in user.cfg if issues

### Phase 2 Deployment (Week 2-3)
- Start on **testnet only** (`futures_testnet=true`)
- Deploy alongside existing spot bot (`trade_mode=hybrid`)
- Monitor paper trading for 2 weeks minimum
- Mainnet: start with $15-20, 1x leverage, 20% position size
- Gradual scale-up over 4 weeks

### Phase 3 Deployment (Week 3-4)
- Yield manager works independently of futures
- Can deploy immediately after Phase 1
- Monitor savings deposits/redemptions

### Phase 4 Deployment (Week 4-5)
- Orchestrator replaces single-strategy model
- Deploy all components together
- Full monitoring via dashboard + Telegram

---

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Futures liquidation wipes account | **CRITICAL** | Start 1x leverage, testnet first, liquidation buffer monitoring, $15 max initial capital |
| python-binance upgrade breaks spot | HIGH | Upgrade python-binance in Docker first, run spot-only for 48h to verify stability before adding futures code |
| Regime detection fails (misses bear switch) | HIGH | Hysteresis + BTC confirmation + manual override via Telegram |
| Funding rate costs eat short profits | MEDIUM | Funding rate threshold check before opening shorts |
| SQLite can't handle concurrent strategies | LOW | Single-writer pattern already works, strategies serialize through OrderGuard |
| USDC-M futures liquidity insufficient | MEDIUM | Stick to BTC/ETH/SOL (highest liquidity USDC-M pairs). Monitor order book depth before every trade. |
| Binance API rate limits | MEDIUM | python-binance handles rate limiting natively; futures has separate rate limit pool |
| Coolify deployment issues | LOW | Same Docker setup, just rebuild image with new requirements |
| **Binance EU blocks USDT — account restrictions** | **CRITICAL** | NEVER use USDT pairs. All bridge/futures code must use USDC only. Verify Binance account allows derivatives before any futures work. |

---

## Open Questions

1. **~~Binance EU restrictions~~ RESOLVED:** Ireland does NOT support USDT. All trading must use USDC bridge and USDC-M futures only. This is a hard constraint, not a question.
2. **Binance EU derivatives access:** Verify this specific Irish account is allowed to trade USDC-M Perpetual Futures. Some EU jurisdictions restrict all derivatives. **This must be verified on Binance before any futures development.** If derivatives are blocked, Phase 2 is entirely impossible and the plan must be revised to accept 0% bear returns.
3. **USDC-M perpetual liquidity:** USDC-M futures have lower liquidity than USDT-M. During high volatility, bid-ask spreads may widen significantly. Must verify that BTCUSDC, ETHUSDC, SOLUSDC perpetuals have sufficient depth for the bot's position sizes ($15-30 per trade).
4. **Minimum futures order sizes:** Binance USDC-M may have different minimum notional values than USDT-M. Must check exchange_info for USDC-M pairs specifically. At $62 total with 25% per position ($15), this is close to the minimum for some pairs.
5. **Tax implications:** Futures trades in Ireland — same CGT rules (33%)? Futures may count as derivatives with different tax treatment (potentially income tax rates instead of CGT). **Consult a tax professional before live futures trading.**
6. **Funding rate economics:** In a strong bull market, funding rates on USDC-M pairs can be 0.05-0.1% per 8h = 15-30% annualized cost to short. USDC-M funding rates may differ from USDT-M. This can wipe out short profits. The strategy must check funding CONTINUOUSLY during open shorts, not just on entry.
7. **python-binance USDC-M support:** The current version (1.0.27) is too old for futures. Upgrading to 1.0.19+ should enable futures methods, but USDC-M perpetuals specifically may need a different API endpoint (FAPI vs DAPI). Must verify the correct endpoint URL and symbol format before coding.

---

## Success Metrics

| Metric | Current | Phase 1 Target | Phase 4 Target |
|--------|---------|----------------|-----------------|
| Bear market return | 0% (sit in USDC) | 0% (sit in USDC) | **+5-15%** (short profits) |
| Bull market return | +50-150% (backtest) | +40-120% (slightly lower, fewer trades) | +40-120% (same) |
| Sideways return | -5% (whipsaw fees) | **-1%** (skip sideways) | **+1-3%** (savings yield) |
| Max drawdown | 48% | **<25%** | **<20%** |
| Annual fee drag | ~35% of portfolio | **<20%** | **<15%** |
| Yield on idle capital | 0% | 0% | **3-8% APY** |
| All-weather viability | ❌ No | ❌ No | ✅ **Yes** |
