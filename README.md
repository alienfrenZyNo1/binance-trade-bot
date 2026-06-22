# Adaptive Crypto Trade Bot

> An autonomous cryptocurrency trading bot for Binance with multi-regime market detection, technical indicator filters, and ROI-optimized execution.

Forked from [edeng23/binance-trade-bot](https://github.com/edeng23/binance-trade-bot) and extensively rebuilt with a custom adaptive strategy, 6 technical indicators, fee optimization, and a Telegram companion bot.

---

## What Makes This Fork Different

The original bot used simple ratio-based coin hopping — "trade the strong coin for the weak one." That works in theory, but in practice it **churns** (rapidly cycles between coins), bleeds fees on noise trades, and doesn't adapt to market conditions.

This fork adds:

| Feature | Purpose |
|---|---|
| **Adaptive Multi-Regime Strategy** | Detects BULL / BEAR / SIDEWAYS / STORMY markets and switches tactics per regime |
| **ADX + EMA Trend Detection** | Classifies trend strength (ADX) and direction (EMA crossover) every 5 minutes |
| **BTC Correlation** | Computes Pearson correlation between held coin and BTC for regime accuracy |
| **Z-Score Volatility Filter** | Trades only when ratio divergence exceeds N standard deviations from the rolling mean |
| **RSI(14) Overbought Filter** | Skips buying coins with RSI > 68 (avoids buying at the top) |
| **Momentum Filter** | Blocks coins crashing >5% in the last hour (catches falling knives) |
| **Anti-Churn Rule** | Won't re-buy a coin sold within the last 6 hours (kills TIA→ENA→TIA cycling) |
| **Minimum Profit Threshold** | Only trades when expected edge > 1.5% (well above 0.15% round-trip fee) |
| **Maker Limit Orders** | Places orders at best bid/ask for 0.025% maker fee (67% savings vs 0.075% taker) |
| **Dynamic Position Sizing** | In BEAR mode deploys 70%, keeps 30% as dry powder for dip-buying |
| **Correlation-Based Selection** | Penalizes candidates >85% correlated with current holding (avoids buying "same" coin) |
| **Bollinger Band Squeeze** | Detects volatility compression (bottom 20th percentile) and boosts score up to 1.3x |
| **Trailing Stop-Loss** | Auto-sells to USDC if a coin drops 8% from its peak since purchase |
| **Telegram Companion Bot** | Interactive commands: `/status` `/profit` `/hop` `/regime` `/coins` `/addcoin` `/removecoin` `/swap` |
| **Persistent Volume Config** | `user.cfg` and `supported_coin_list` survive container restarts and image rebuilds |

---

## Strategy Architecture

The bot runs a single strategy (`improved`) that contains the full adaptive multi-regime engine:

```
┌─────────────────────────────────────────────────────────────┐
│                    SCOUT LOOP (every 1s)                      │
│                                                              │
│  1. Update Market Regime (every 5 min)                       │
│     └─ ADX(14) + EMA(20/50) + Volatility + BTC Correlation   │
│        → BULL / BEAR / SIDEWAYS / STORMY                     │
│                                                              │
│  2. Re-entry Check (if holding USDC)                         │
│     └─ Find best coin to buy back                            │
│                                                              │
│  3. Trailing Stop-Loss                                       │
│     └─ Sell to USDC if -8% from peak                         │
│                                                              │
│  4. Profit-Taking (optional, disabled by default)            │
│                                                              │
│  5. Trade Cooldown (regime-aware)                            │
│     └─ BEAR: 2h / SIDEWAYS: 30m / BULL: 15m                 │
│                                                              │
│  6. Jump to Best Coin                                        │
│     ├─ Phase 1: Score all pairs (% gain - fee hurdle)        │
│     ├─ Phase 2: Filter by minimum profit threshold (1.5%)    │
│     ├─ Anti-Churn: Remove recently held coins                │
│     ├─ Phase 3: Z-score filter (regime-scaled)               │
│     ├─ Phase 4: Momentum + RSI filter                       │
│     ├─ Feature 4: Correlation penalty on remaining           │
│     ├─ Feature 5: BB squeeze bonus on remaining              │
│     └─ Execute: Maker limit order with position sizing       │
└─────────────────────────────────────────────────────────────┘
```

### Regime-Specific Behavior

| Regime | ADX | Detection | Strategy | Z-Score | Cooldown |
|---|---|---|---|---|---|
| **BULL** 🟢 | >25 | EMA20 > EMA50, +DI > -DI | Momentum following — buy strength | 1.0 (loose) | 15 min |
| **BEAR** 🔴 | >25 | EMA20 < EMA50, -DI > +DI | Defense — preserve capital | 3.75 (tight) | 2 hours |
| **SIDEWAYS** 🟡 | <25 | No clear trend | Mean reversion — buy dips | 1.5 (normal) | 30 min |
| **STORMY** 🟠 | Any | Volatility > 8% | Conservative — extreme only | 3.0 (very tight) | 30 min |

---

## Fee Optimization

The fee structure is critical at small account sizes. This fork attacks fees from multiple angles:

| Layer | Mechanism | Savings |
|---|---|---|
| **BNB Fee Discount** | Pay fees in BNB for 25% off | 0.075% per side (vs 0.1%) |
| **Maker Orders** | Place at best bid/ask, not ticker | 0.025% per side (67% off taker) |
| **Combined** | BNB discount + maker | **0.025% per side, 0.05% round-trip** |
| **Anti-Churn** | 6-hour block on re-buying | Prevents rapid cycling that multiplies fees |
| **Min Profit Threshold** | Require >1.5% edge | No trades where fees eat the profit |
| **Longer Cooldowns** | BEAR: 2h, SIDEWAYS: 30m | Fewer trades = less fee leakage |

**Before optimization:** ~$168/year in fees on a $62 account (270% of balance)
**After optimization:** ~$15-25/year estimated (quality trades only)

---

## Quick Start

### Prerequisites
- Binance account with API keys (spot trading enabled)
- BNB balance for fee discount (optional but recommended)
- Docker (or Python 3.11+)

### 1. Clone
```bash
git clone https://github.com/alienfrenZyNo1/binance-trade-bot.git
cd binance-trade-bot
```

### 2. Configure
Edit `user.cfg`:
```ini
[binance_user_config]
api_key=              # Or set API_KEY env var
api_secret_key=       # Or set API_SECRET_KEY env var
current_coin=TIA      # Your starting coin (or leave empty for random)
bridge=USDC           # Bridge currency
scout_multiplier=6    # Higher = fewer, better trades
strategy=improved     # The adaptive strategy
```

Edit `supported_coin_list` — one coin per line, comments with `#`.

### 3. Run

**With Docker:**
```bash
docker build -t trade-bot .
docker run -d \
  -v ./data:/app/data \
  -e API_KEY=your_key \
  -e API_SECRET_KEY=your_secret \
  trade-bot
```

**With Python:**
```bash
pip install -r requirements.txt
python -m binance_trade_bot
```

---

## Configuration Reference

All settings live in `user.cfg`. Environment variables override config file values.

### Core Settings
| Setting | Default | Description |
|---|---|---|
| `bridge` | USDC | Bridge currency (USDC recommended) |
| `current_coin` | TIA | Starting coin (empty = random from list) |
| `scout_multiplier` | 6 | Fee hurdle multiplier (higher = fewer trades) |
| `scout_sleep_time` | 1 | Seconds between scout cycles |
| `strategy` | improved | Strategy module to use |
| `buy_timeout` | 20 | Minutes before cancelling unfilled buy |
| `sell_timeout` | 20 | Minutes before cancelling unfilled sell |

### Adaptive Strategy Settings
| Setting | Default | Description |
|---|---|---|
| `z_score_threshold` | 1.5 | Std devs from mean required to trade |
| `trade_cooldown_seconds` | 1800 | Seconds between trades (sideways) |
| `ratio_sample_interval` | 10 | Minutes between ratio samples |
| `ratio_sample_retention_days` | 7 | How long to keep ratio history |
| `momentum_filter_enabled` | yes | Skip coins crashing > N% in 1h |
| `momentum_max_drop_1h` | 5.0 | Max acceptable 1h drop (%) |

### Regime Detection
| Setting | Default | Description |
|---|---|---|
| `regime_check_enabled` | yes | Enable market regime detection |
| `regime_check_interval` | 300 | Seconds between regime checks |
| `adx_period` | 14 | ADX calculation period |
| `adx_trend_threshold` | 25.0 | ADX above this = trending |
| `ema_short` | 20 | Short EMA period |
| `ema_long` | 50 | Long EMA period |
| `btc_correlation_enabled` | yes | Compute BTC correlation |
| `regime_high_vol_threshold` | 8.0 | Volatility % above this = STORMY |

### Bear Mode
| Setting | Default | Description |
|---|---|---|
| `bear_cooldown` | 7200 | Seconds between trades in bear (2h) |
| `bear_zscore_mult` | 2.5 | Z-score multiplier in bear (very selective) |
| `bear_profit_take_interval` | 999 | Profit-taking interval (disabled) |
| `bear_momentum_max_drop` | 2.0 | Stricter momentum threshold in bear |

### Bull Mode
| Setting | Default | Description |
|---|---|---|
| `bull_zscore_mult` | 0.67 | Looser z-score to ride trends |
| `bull_cooldown` | 900 | Seconds between trades (15m) |
| `bull_profit_take_interval` | 30 | Profit-taking every 30 trades |

### ROI Optimization Features
| Setting | Default | Description |
|---|---|---|
| `min_profit_threshold` | 0.015 | Minimum expected edge to trade (1.5%) |
| `churn_block_seconds` | 21600 | Don't re-buy coins sold within this window (6h) |
| `rsi_filter_enabled` | yes | Skip overbought coins |
| `rsi_overbought` | 68 | RSI threshold above which buys are blocked |
| `use_maker_orders` | yes | Place at bid/ask for maker fills |
| `dynamic_position_enabled` | yes | Keep dry powder in bear/sideways |
| `bear_position_size` | 0.7 | Deploy 70% in bear, keep 30% reserve |
| `sideways_position_size` | 0.9 | Deploy 90% in sideways |
| `correlation_filter_enabled` | yes | Penalize correlated coins |
| `correlation_threshold` | 0.85 | Correlation above this triggers penalty |
| `bb_squeeze_enabled` | yes | Detect volatility compression |
| `bb_period` | 20 | Bollinger Band period |
| `bb_squeeze_lookback` | 50 | Lookback for squeeze percentile |

### Trailing Stop-Loss
| Setting | Default | Description |
|---|---|---|
| `trailing_stop_enabled` | yes | Auto-sell on drop from peak |
| `trailing_stop_pct` | 8.0 | Sell if coin drops this % from peak |

---

## Telegram Companion Bot

The companion bot runs as a systemd service and connects directly to the trading database — no redeploy needed for coin changes.

### Commands

| Command | Description |
|---|---|
| `/status` | Bot status, current coin, balance, regime |
| `/profit` | P&L breakdown, win rate, per-trade analysis, fees paid |
| `/hop` | Show all strategy filters for each candidate (score, z-score, momentum, cooldown) |
| `/regime` | Current market regime, ADX, volatility, BTC correlation |
| `/coins` | List all enabled/disabled coins |
| `/price` | Current price of held coin |
| `/addcoin SOL` | Enable a coin (live, ~3 sec to take effect) |
| `/removecoin TIA` | Disable a coin (live) |
| `/swap TIA SOL` | Disable TIA, enable SOL (live) |
| `/trades` | Recent trade history |
| `/help` | Show all commands |

### Setup
1. Create a Telegram bot via [@BotFather](https://t.me/BotFather)
2. Set the bot token in `scripts/telegram_bot.py`
3. Run as systemd service:
```bash
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

---

## Persistent Volume Configuration

This fork uses a `docker-entrypoint.sh` that loads config from a persistent volume, so settings survive image rebuilds and container restarts.

```
/app/data/config/user.cfg           ← Persistent (survives rebuilds)
/app/data/config/supported_coin_list ← Persistent
/app/data/crypto_trading.db          ← Persistent trade history
```

**First boot:** Seeds the volume from the image.
**Subsequent boots:** Volume copy wins (preserves any runtime changes).

### `set_coins()` Safe Mode
The coin sync logic was modified to **never re-enable a coin that was manually disabled**. This prevents the `SUPPORTED_COIN_LIST` env var from undoing Telegram `/removecoin` commands on restart.

---

## Testing

74 tests covering all indicators, strategy logic, and integration points:

```bash
# Install test dependencies
pip install pytest

# Run all tests
python -m pytest tests/ -v

# Run only ROI optimization tests
python -m pytest tests/test_roi_optimization.py -v

# Run only adaptive strategy tests
python -m pytest tests/test_adaptive_strategy.py -v
```

### Test Coverage
- **Indicators**: EMA, SMA, std dev, RSI (5 scenarios), Bollinger Bands (4), squeeze detection (4), Pearson correlation (4), correlation matrix (2), ADX (3)
- **Strategy logic**: anti-churn (4), config validation, position sizing math (4), correlation penalty (3), BB bonus (4), maker order logic (4)
- **Integration**: strategy imports, method existence, `buy_alt` signature

---

## Indicators Module

Standalone technical indicators with zero external dependencies (`binance_trade_bot/indicators.py`):

| Function | Description |
|---|---|
| `compute_ema(values, period)` | Exponential Moving Average |
| `compute_sma(values, period)` | Simple Moving Average |
| `compute_std(values, period)` | Standard Deviation |
| `compute_adx(highs, lows, closes, period)` | ADX + DI (trend strength) |
| `compute_rsi(closes, period)` | RSI (momentum oscillator) |
| `compute_bollinger_bands(closes, period, num_std)` | Bollinger Bands (middle/upper/lower/bandwidth) |
| `detect_bollinger_squeeze(closes, period, lookback)` | Squeeze detection (is_squeeze, bandwidth, percentile) |
| `compute_correlation(series_a, series_b)` | Pearson correlation |
| `compute_returns(prices)` | Period-over-period returns |
| `compute_correlation_matrix(price_dict)` | Full correlation matrix between assets |

---

## Deployment with Coolify

This bot is deployed via [Coolify](https://coolify.io) (self-hosted PaaS) on a VPS:

1. Fork this repo
2. Create a new application in Coolify pointing to your fork
3. Set environment variables in Coolify:
   - `API_KEY`, `API_SECRET_KEY` — Binance API keys
   - `BRIDGE_SYMBOL=USDC`
   - `STRATEGY=improved`
   - `SUPPORTED_COIN_LIST=SOL SUI XRP ADA DOGE NEAR LINK AAVE AVAX APT INJ TIA ENA PEPE JUP`
4. Add a persistent volume: `REDACTED` → `/app/data`
5. Deploy

---

## Project Structure

```
binance-trade-bot/
├── binance_trade_bot/
│   ├── auto_trader.py              # Base trading logic
│   ├── binance_api_manager.py      # Binance API + maker order support
│   ├── config.py                   # All configuration (40+ params)
│   ├── crypto_trading.py           # Main entry point + scheduler
│   ├── database.py                 # DB layer + safe set_coins()
│   ├── indicators.py               # 10 technical indicators (standalone)
│   ├── logger.py
│   ├── scheduler.py
│   ├── strategies/
│   │   ├── improved_strategy.py    # Adaptive multi-regime strategy (950+ lines)
│   │   ├── default_strategy.py     # Original simple strategy
│   │   └── multiple_coins_strategy.py
│   ├── database/
│   │   ├── models.py               # Coin, Pair, Trade, RatioSample, PairStats, MarketRegimeLog
│   │   └── database.py             # Extended DB methods
│   └── models/                     # SQLAlchemy models
├── scripts/
│   ├── telegram_bot.py             # Interactive Telegram companion bot
│   └── monitor_coins.py            # Coin health monitor (delisting/volume)
├── tests/
│   ├── test_roi_optimization.py    # 57 tests (indicators + features)
│   └── test_adaptive_strategy.py   # 17 tests (ADX, EMA, trailing stop)
├── docker-entrypoint.sh            # Persistent volume config loader
├── user.cfg                        # Main configuration
├── supported_coin_list             # Coin list
├── Dockerfile
└── requirements.txt
```

---

## Backtesting

```bash
python backtest.py
```

Modify `backtest.py` to test different settings, time periods, and coin lists against historical data.

---

## Credits

- **Original bot**: [Eden Gaon](https://github.com/edeng23/binance-trade-bot) — the foundation this fork builds upon
- **Strategy + features**: Custom adaptive multi-regime implementation with technical indicators, fee optimization, and risk management

---

## Disclaimer

This project is for informational purposes only. You should not construe any information as legal, tax, investment, financial, or other advice. Nothing here constitutes a solicitation, recommendation, or offer to buy or sell any securities or financial instruments.

**If you plan to use real money, USE AT YOUR OWN RISK.**

Cryptocurrency trading carries significant risk of loss. Past performance does not guarantee future results. Never invest more than you can afford to lose.
