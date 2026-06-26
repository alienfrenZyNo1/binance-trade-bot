# HA-002: Dual Bot Instance — Emergency Escalation

**Status:** 🔴 ESCALATION — REQUIRES IMMEDIATE BOSS DECISION
**Timestamp:** 2026-06-26 23:50 UTC
**Requested by:** Bot-Lead (automated hourly check-in)
**Severity:** CRITICAL (capital-risk)

## What was detected
During the hourly health check, **TWO concurrent `python -m binance_trade_bot` processes** were found running, each at 100% CPU:

| PID | Container ID | Started | PPID |
|-----|-------------|---------|------|
| 3537111 | 368d43293d1a | 23:32:22 UTC | containerd-shim (moby) |
| 3544978 | 74af5d6d581e | 23:34:27 UTC | containerd-shim (moby) |

Both run in **separate Docker containers** and almost certainly share the same live database (`/data/binance-bot-data/crypto_trading.db`) and the same Binance API keys.

## Why this is dangerous
- **Duplicate-order hazard:** Two bot instances racing on the same account/DB can submit duplicate orders, double-position entries, or conflicting jumps (sell A→B in instance 1 while instance 2 sells B→A).
- **State corruption:** Concurrent writes to `current_coin_history`, `pairs`, `bot_state` can corrupt routing logic.
- **API rate-limit escalation:** Two instances double the request load (the very problem #96 was filed to mitigate).
- The live DB shows only 2 trades in 24h (last at 02:39 UTC), so no duplicate order has been observed *yet* — but the risk is live as long as both run.

## What I cannot do (needs Boss authority)
1. Kill one of the containers — this is a capital-risk / live-trading action requiring Boss approval. Bot-Lead has **no docker.sock access** (`permission denied`) and must not change live trading behavior without approval.
2. Determine which container is the "intended" live instance vs. a stale/rogue one.

## Decision requested from The Boss
**Kill ONE of the two instances immediately** and confirm a single-instance deployment going forward. Recommended:
- Identify the intended container via `docker ps` (needs root/docker group).
- Stop the newer/rogue container: `docker stop 74af5d6d581e` (PID 3544978, started 23:34) — **pending Boss confirmation of which is canonical.**
- Verify only one `python -m binance_trade_bot` remains.
- Cross-check the last 10 trades against Binance order history for any duplicates.

## Related context
- systemd service (#91) was developed (commit 6ee27ff) but is **NOT installed** (`binance-trade-bot.service` does not exist). Adopting systemd with `Restart=on-failure` would prevent accidental dual-launch. Boss approval needed to install + cut over from the current docker setup.
- This dual-instance condition likely arose from a manual restart at ~23:32–23:34 UTC without stopping the prior container.

## Risk parameters (unchanged, per risk-appetite.yaml)
- Max daily loss: 3% | Max drawdown: 8% | Max position: 5% | Instruments: spot only (futures OFF)

**Decision: DEFERRED — awaiting Boss action.**
