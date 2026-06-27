# Adaptive Crypto Trade Bot

> An autonomous cryptocurrency trading bot for Binance with **momentum rotation in bull markets**, **USDC-M futures shorting in bear markets**, regime detection, and a feature-rich Telegram companion bot.

Forked from [edeng23/binance-trade-bot](https://github.com/edeng23/binance-trade-bot) and extensively rebuilt.

---

## How It Works

The bot runs a single-position rotation strategy that adapts to market conditions:

```
┌──────────────────────────────────────────────────────────────┐
│                      SCOUT LOOP (every ~1s)                    │
│                                                               │
│  1. Market Regime Detection (every 5 min)                     │
│     └─ ADX(14) + EMA(12/26) on SOL/USDC 1h klines             │
│        → BULL / BEAR / SIDEWAYS                               │
│                                                               │
│  2. If BULL or SIDEWAYS → Spot Momentum Rotation              │
│     ├─ Measure 18h performance for all coins                  │
│     ├─ Find coin outperforming current holding by ≥8%         │
│     ├─ RSI(14) filter: skip overbought coins (RSI > 75)      │
│     ├─ 3-cycle confirmation delay before executing            │
│     ├─ Anti-churn: don't re-buy coins sold in last 24h        │
│     ├─ Trailing stop: auto-sell if -15% from peak             │
│     └─ Execute: coin → USDC → new coin                        │
│                                                               │
│  3. If BEAR → Futures Short Selling                           │
│     ├─ Sell spot holdings to USDC                             │
│     ├─ Transfer USDC to futures wallet                        │
│     ├─ Find worst-performing coin (most negative momentum)    │
│     ├─ Open 1x short with 50% max margin                      │
│     ├─ Manage: 15% hard stop, 10% trailing after +3% profit   │
│     ├─ Funding rate guard: close if funding > 0.03%           │
│     └─ On regime change → close shorts, return to spot        │
│                                                               │
│  4. State Persistence (survives container restarts)           │
│     └─ last_trade_time, awaiting_reentry, churn list → DB     │
└──────────────────────────────────────────────────────────────┘
```

### Why Momentum Rotation?

The original bot used mean-reversion (buy the dip, sell the pump). In crypto, trends **persist** — when a coin starts outperforming, it tends to keep going for hours or days. The momentum strategy rotates into whichever coin is winning, requiring an 8% performance edge over the current holding before switching. This makes trades selective (~1 per 4 days in backtest) instead of churning on noise.

### Why Futures Shorting in Bear?

Long-only spot cannot profit in a bear market. This fork integrates Binance **USDC-M perpetual futures** to open short positions when the regime turns bearish. The bot shorts the worst-performing eligible coin with conservative risk management (1x leverage, 15% stop-loss, 10% trailing, funding rate guard).

---

## Key Features

| Feature | Description |
|---|---|
| **Momentum Rotation** | Rotate into coins outperforming current holding by ≥8% over 18 hours |
| **Market Regime Detection** | ADX + EMA on SOL/USDC classifies BULL / BEAR / SIDEWAYS every 5 minutes |
| **USDC-M Futures Shorting** | Opens 1x short positions during bear regime on eligible coins |
| **State Persistence** | Trade cooldown, re-entry state, and churn blocklist survive container restarts via `bot_state` DB table |
| **3-Cycle Confirmation Delay** | Rotation signal must persist 3 consecutive scout cycles before executing — eliminates noise-driven trades |
| **Anti-Churn Filter** | Won't re-buy a coin sold within the last 24 hours |
| **Trailing Stop-Loss** | Auto-sells to USDC if a coin drops 15% from its peak |
| **RSI Filter** | Skips buying coins with RSI > 75 (overbought) |
| **Futures Risk Management** | 1x leverage, 50% max margin, 15% hard stop, 10% trailing after +3% profit, funding rate guard |
| **Telegram Companion Bot** | 15 commands: `/status` `/futures` `/health` `/profit` `/config` `/kill` `/regime` `/coins` `/trades` `/hop` `/price` `/addcoin` `/removecoin` `/swap` `/help` |
| **Autonomous Coin Manager** | Daily cron job that auto-removes dead coins, discovers replacements, and scores candidates by regime fit using multi-timeframe momentum, RSI, volatility, and correlation |
| **SQLite WAL Mode** | Write-Ahead Logging for concurrent read/write (bot + Telegram bot + dashboard) |
| **Daily DB Backups** | Automatic VACUUM INTO backup every 24 hours |
| **Position Reconciliation** | On restart, reconciles DB state with actual Binance balances and futures positions |
| **Persistent Volume Config** | `user.cfg` and `supported_coin_list` survive container restarts and image rebuilds |

---

## Futures Shorting Details

During **BEAR** regime, the bot transitions from spot trading to futures:

**Entry:**
- Sells spot holdings → USDC → transfers to futures wallet
- Finds worst-performing coin among futures-eligible set
- Eligible coins: SOL, XRP, ADA, DOGE, NEAR, LINK, AAVE, AVAX, SUI, TIA, ENA
- Only shorts coins with negative 18h momentum
- Opens SELL market order at 1x leverage, max 50% of wallet as margin

**Management (checked every 60 seconds):**
- Hard stop-loss at -15% (closes position)
- Trailing stop: after +3% profit, closes if profit gives back 10%
- Funding rate guard: closes if funding rate exceeds 0.03% per 8h

**Exit:**
- Automatic on regime change (BEAR → BULL/SIDEWAYS)
- Closes position, transfers USDC back to spot wallet

---

## Autonomous Coin Management

The bot includes a regime-aware coin manager (`scripts/monitor_coins.py`) that runs daily as a background cron job. It automatically maintains the optimal coin list in the database — no manual editing, no Docker restart required.

### How It Works

1. **Health check** — Scans all enabled coins for delisted pairs, dead volume (<$50K), or sustained low volume (<$500K for 3 days). Dead coins are disabled in the DB instantly.

2. **Dynamic candidate discovery** — Scans Binance's top USDC spot pairs by 24h volume to find replacement candidates (excludes stablecoins, majors like BTC/ETH/BNB, leveraged tokens, and non-standard symbols).

3. **Regime-aware scoring** — Each candidate is scored against the current market regime using multi-timeframe metrics:

| Metric | Source | Purpose |
|---|---|---|
| **7d / 14d / 30d momentum** | Daily OHLCV | Is this coin trending or flat? |
| **RSI(14)** | Hourly closes (Wilder's smoothing) | Overbought / oversold detection |
| **Hourly volatility** | Std dev of 48h returns | Enough movement for rotation signals? |
| **24h quote volume** | Binance ticker | Liquidity for clean execution |
| **USDC perp availability** | Futures market scan | Can the bot short it in BEAR? |
| **Correlation** | 30d daily log returns vs current list | Diversification — rejects >0.88 correlated |

4. **Upgrade pass** — Beyond filling dead-coin slots, it swaps the weakest current coin if a candidate scores **1.8x better**. Conservative guards prevent churn (max 1 swap/day, 5-day settle period, held coins and open futures positions protected).

### Scoring Philosophy by Regime

```
BULL      → Momentum leaders with room to run
            Rewards: strong 7d/14d trend, RSI 55-72 sweet spot
            Penalizes: RSI > 82 (exhausted), flat performers

SIDEWAYS  → Oscillation quality for rotation
            Rewards: moderate volatility (1-4%), neutral momentum
            Penalizes: strong trends either direction (mean reverts)

BEAR      → Short-ready targets
            Requires: USDC perpetual future
            Rewards: negative momentum, RSI 30-55 (room to fall)
            Penalizes: RSI < 22 (oversold squeeze risk)

STORMY    → Defensive
            Rewards: highest volume, lowest volatility only
```

### Safety Rails

- **Circuit breaker**: Aborts if Binance API returns <500 markets (failure suspected)
- **Max removal cap**: Never disables more than 30% of coins in a single run
- **Protected coins**: Won't touch the currently held coin or coins with open futures positions
- **Major exclusion**: Never auto-promotes BTC/ETH/BNB (reference coins, not momentum targets)
- **Settle period**: Won't upgrade-swap coins added within the last 5 days

### Running

The script runs as a `no_agent` cron job (zero LLM cost, fully deterministic):

```bash
# Manual run (verbose shows all scores)
DB_PATH=/data/binance-bot-data/crypto_trading.db python3 scripts/monitor_coins.py --verbose

# Output when healthy (silent — no Telegram notification):
✅ All 15 coins healthy for SIDEWAYS regime.

# Output when changes are made (notifies via Telegram):
🤖 3 regime-aware action(s) [SIDEWAYS]:
  🔄 REPLACE TIA: low vol $430K for 3d
  ➕ ADD BCH (score 32.7): 7d=-0.1% RSI=60 vol=$2.1M corr=0.70
  ⬆️ UPGRADE AAVE (score 7.6) → SEI (score 31.5)
```

---

## Quick Start

### Prerequisites
- Binance account with API keys (spot + futures trading enabled)
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
current_coin=TIA      # Starting coin (or leave empty for random)
bridge=USDC           # Bridge currency
scout_multiplier=6    # Fee hurdle multiplier
strategy=momentum     # The momentum + futures strategy
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
| `bridge` | USDC | Bridge currency (USDC required for futures) |
| `current_coin` | TIA | Starting coin (empty = random) |
| `scout_multiplier` | 6 | Fee hurdle multiplier |
| `scout_sleep_time` | 1 | Seconds between scout cycles |
| `strategy` | momentum | Strategy module to use |
| `buy_timeout` | 20 | Minutes before cancelling unfilled buy |
| `sell_timeout` | 20 | Minutes before cancelling unfilled sell |

### Momentum Strategy
| Setting | Default | Description |
|---|---|---|
| `momentum_lookback_hours` | 18 | Hours of price history for momentum calculation |
| `momentum_min_edge` | 8.0 | Minimum performance edge (%) to trigger rotation |
| `trade_cooldown_seconds` | 7200 | Minimum seconds between trades (2 hours) |
| `churn_block_seconds` | 86400 | Don't re-buy coins sold within this window (24h) |
| `confirmation_cycles` | 3 | Consecutive scout cycles a signal must persist before executing |
| `trailing_stop_enabled` | yes | Auto-sell on drop from peak |
| `trailing_stop_pct` | 15.0 | Sell if coin drops this % from peak |
| `z_score_threshold` | 1.5 | Std devs from mean (legacy filter) |
| `momentum_filter_enabled` | yes | Skip coins crashing in last hour |
| `momentum_max_drop_1h` | 5.0 | Max acceptable 1h drop (%) |

### Regime Detection
| Setting | Default | Description |
|---|---|---|
| `regime_check_enabled` | yes | Enable market regime detection |
| `regime_check_interval` | 300 | Seconds between regime checks (5 min) |
| `adx_period` | 14 | ADX calculation period |
| `adx_trend_threshold` | 25.0 | ADX above this = trending market |
| `ema_short` | 12 | Short EMA period |
| `ema_long` | 26 | Long EMA period |

### Futures Settings
| Setting | Default | Description |
|---|---|---|
| `futures_leverage` | 1 | Leverage multiplier (1x = no leverage) |
| `futures_max_margin_pct` | 0.5 | Max % of wallet to use as margin (50%) |
| `futures_stop_loss_pct` | 15.0 | Hard stop-loss on short positions |
| `futures_trailing_stop_pct` | 10.0 | Trailing stop after +3% profit |
| `futures_max_funding_rate` | 0.0001 | Max funding rate to hold position (0.01%) |
| `futures_check_interval` | 60 | Seconds between position management checks |

---

## Telegram Companion Bot

The companion bot runs as a systemd service and connects directly to the trading database + Binance API — no redeploy needed for coin changes.

### Commands

| Command | Description |
|---|---|
| `/status` | Bot status, current coin, balance, regime, ADX |
| `/futures` | Open futures positions, P&L, margin, entry/exit prices |
| `/health` | Container health, DB integrity, API connectivity, backup status |
| `/profit` | P&L breakdown, win rate, per-trade analysis, fees paid |
| `/config` | Current strategy configuration |
| `/kill` | Emergency stop — closes all futures positions |
| `/regime` | Current market regime, ADX, EMA values |
| `/coins` | List all enabled/disabled coins |
| `/trades` | Recent trade history |
| `/hop` | Show all strategy filters for each candidate |
| `/price` | Current price of held coin |
| `/addcoin SOL` | Enable a coin (live, ~3 sec to take effect) |
| `/removecoin TIA` | Disable a coin (live) |
| `/swap TIA SOL` | Disable TIA, enable SOL (live) |
| `/help` | Show all commands |

### Setup
1. Create a Telegram bot via [@BotFather](https://t.me/BotFather)
2. Set the token in `.env.telegram`:
```ini
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_IDS=your_chat_id
DB_PATH=data/crypto_trading.db
BRIDGE_SYMBOL=USDC
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
```
3. Run as systemd service:
```bash
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

---

## Foundation Hardening

Phase 0 reliability fixes applied:

- **Restart policy**: `--restart unless-stopped` on Docker container
- **PID lock**: `flock()` prevents concurrent instances from corrupting the DB
- **SQLite WAL mode**: Concurrent read/write safety for bot + Telegram bot + dashboard
- **Position reconciliation**: On restart, syncs DB state with actual Binance balances
- **Thread safety**: `RLock` on order mutex prevents race conditions
- **FAILED trade state**: Orders that fail mid-rotation are tracked, not lost
- **Graceful shutdown**: Stream manager threads terminate cleanly on SIGTERM
- **Retry on API errors**: Transient Binance API failures retry with backoff
- **Daily DB backups**: Automatic VACUUM INTO every 24 hours

---

## Status & Research

### Strategy backtest — honest assessment
The strategy is **under active revalidation**; treat earlier "+79% / Sharpe 3.85" headline
figures as **artifacts** of methodological flaws (same-bar-close lookahead, understated
slippage, inverted train/OOS split). A corrected revalidation
([`docs/audits/backtest-revalidation-report.md`](docs/audits/backtest-revalidation-report.md))
puts the full-period result at **+36.5% / Sharpe ~1.1 / ~62% max drawdown** — *probably real
but small, risky, and under-sampled* (5 OOS windows). **Defensive timing (trailing stop +
regime→USDC rotation + anti-churn filter) is the real edge; momentum coin-selection is
unproven.** Do not scale capital on the current evidence. The 62% max drawdown is the binding
constraint. See also [`docs/audits/backtest-audit.md`](docs/audits/backtest-audit.md).

### Regime v2 candidate (non-live)
A multi-signal **regime v2 candidate module** exists at
[`binance_trade_bot/regime_v2_signals.py`](binance_trade_bot/regime_v2_signals.py)
(issue #102). It implements five detectors (multi-coin breadth, BTC confirmation,
realized-volatility, funding-rate, composite score) recommended in the strategy hypotheses.
It is **intentionally NOT wired into the live trading path** — it is offline-testable
research code (26 tests pass) feeding the Regime v2 promotion pipeline (issue #72), which is
**not yet promotion-ready**. The live bot still uses the SOL-only ADX+EMA classifier.

### Research hypotheses
[`research/strategy-hypotheses-2026-06.md`](research/strategy-hypotheses-2026-06.md)
decomposes where the edge actually comes from and proposes five candidate hypotheses
(defensive-timing overlay, funding-rate harvesting, volatility-targeted sizing, sideways
mean-reversion) plus regime-detection signal improvements. The single highest-value next
experiment is a stop/regime/anti-churn ablation to confirm the defensive-timing attribution.

### Live status snapshot
See [`CURRENT_STATE.md`](CURRENT_STATE.md) for a living summary of exactly what the deployed
bot runs, what's pending review on the fix branch, and open issues. The journal of research
sessions lives in [`research/JOURNAL.md`](research/JOURNAL.md).

---

## Deployment with Coolify

This bot is deployed via [Coolify](https://coolify.io) (self-hosted PaaS) on a VPS:

1. Fork this repo
2. Create a new application in Coolify pointing to your fork
3. Set environment variables in Coolify:
   - `API_KEY`, `API_SECRET_KEY` — Binance API keys
   - `BRIDGE_SYMBOL=USDC`
   - `STRATEGY=momentum`
   - `SUPPORTED_COIN_LIST=SOL SUI XRP ADA DOGE NEAR LINK AAVE AVAX APT INJ TIA ENA PEPE JUP`
4. Add a persistent volume: host path → `/app/data`
5. Deploy

> **Note:** Coolify auto-build may fail. If so, build manually:
> ```bash
> sudo docker build --no-cache -t trade-bot:latest .
> sudo docker run -d --name <container_name> --restart unless-stopped \
>   -v ./data:/app/data \
>   --env-file /path/to/your/env-file \
>   trade-bot:latest
> ```

---

## Project Structure

```
binance-trade-bot/
├── binance_trade_bot/
│   ├── __init__.py
│   ├── __main__.py                     # Entry point: python -m binance_trade_bot
│   ├── auto_trader.py                  # Base trading logic + retry/FAILED fixes
│   ├── binance_api_manager.py          # Binance API wrapper (spot + futures)
│   ├── binance_stream_manager.py       # WebSocket stream management
│   ├── backtest.py                     # Internal backtest harness
│   ├── config.py                       # All configuration (50+ params)
│   ├── config_validation.py            # Startup config checks + warnings
│   ├── crypto_trading.py               # Main entry point, scheduler, reconciliation
│   ├── database.py                     # DB layer, bot_state, regime logging, backups
│   ├── indicators.py                   # 10 technical indicators (standalone, zero deps)
│   ├── logger.py
│   ├── notifications.py                # Apprise notification handler + flood guard
│   ├── scheduler.py
│   ├── accounting.py                   # Deposit tracking, phantom hop detection
│   ├── canary_capital_guard.py         # Canary-mode spot/futures capital caps
│   ├── futures_manager.py              # USDC-M futures shorting engine
│   ├── futures_transfer_policy.py      # Futures→spot transfer dust/retry logic
│   ├── regime_hysteresis.py            # Consecutive-cycle regime confirmation
│   ├── regime_transition_planner.py    # Regime change planning (BEAR→spot, etc.)
│   ├── repositories.py                 # Database repository layer
│   ├── formatting/
│   │   ├── __init__.py
│   │   └── telegram_html.py            # HTML formatting helpers (tables, money, pct)
│   ├── strategies/
│   │   ├── momentum_strategy.py        # Momentum rotation + futures + state persistence
│   │   ├── improved_strategy.py        # Legacy adaptive multi-regime strategy
│   │   ├── default_strategy.py         # Original simple strategy
│   │   └── multiple_coins_strategy.py
│   └── models/                         # SQLAlchemy models
│       ├── bot_state.py                # Persistent key-value store for strategy state
│       ├── market_regime_log.py        # Regime classification log
│       ├── deposit.py                  # Account top-up tracking
│       ├── scout_history.py            # Scout cycle history
│       └── ... (coin, pair, trade, etc.)
├── scripts/
│   ├── telegram_bot.py                 # Interactive Telegram companion bot (15 commands)
│   ├── monitor_coins.py                # Regime-aware autonomous coin manager (daily cron)
│   ├── smoke_telegram_commands.py      # Telegram command render + HTML validation tests
│   ├── strategy_acceptance_gates.py    # Strategy promotion gate checks
│   ├── regime_promotion_readiness.py   # Daily regime v2 readiness report
│   ├── regime_v2_forward_replay.py     # Cached forward replay harness
│   ├── research_regime_v2_evaluator.py # Regime v2 scorecard + route outcomes
│   ├── research_regime_classifier.py   # Regime classification research
│   ├── research_bull_momentum_optimizer.py
│   ├── research_bear_futures_backtester.py
│   └── research_sideways_chop_backtester.py
├── tests/                              # 247 unit tests
├── research/                           # Quantitative research journal & backlog
│   ├── JOURNAL.md
│   ├── EXPERIMENTS.md
│   └── BACKLOG.md
├── docs/                               # Developer docs & refactor plans
├── config/                             # Apprise notification templates
├── .github/workflows/cicd.yaml         # CI: lint + test on push
├── docker-entrypoint.sh                # Persistent volume config loader
├── user.cfg                            # Main configuration
├── supported_coin_list                 # Coin list
├── Dockerfile
├── requirements.txt
└── dev-requirements.txt
```

---

## Backtesting

```bash
python backtest_strategy.py    # Momentum strategy backtest
python backtest_full.py        # Full backtest with all 14 filters
python optimize_momentum.py    # Grid search for optimal parameters
```

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
| `compute_bollinger_bands(closes, period, num_std)` | Bollinger Bands |
| `detect_bollinger_squeeze(closes, period, lookback)` | Squeeze detection |
| `compute_correlation(series_a, series_b)` | Pearson correlation |
| `compute_returns(prices)` | Period-over-period returns |
| `compute_correlation_matrix(price_dict)` | Full correlation matrix |

---

## Credits

- **Original bot**: [Eden Gaon](https://github.com/edeng23/binance-trade-bot) — the foundation this fork builds upon
- **Momentum strategy**: Custom rotation strategy with regime-aware filtering
- **Futures engine**: USDC-M perpetual shorting with risk management

---

## Disclaimer

This project is for informational purposes only. You should not construe any information as legal, tax, investment, financial, or other advice. Nothing here constitutes a solicitation, recommendation, or offer to buy or sell any securities or financial instruments.

**If you plan to use real money, USE AT YOUR OWN RISK.**

Cryptocurrency trading carries significant risk of loss. Past performance does not guarantee future results. Never invest more than you can afford to lose.
