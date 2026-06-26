# SD-003: Fix Coolify Redeploy Leaving Stale Container (DUAL-INSTANCE HAZARD)

**Status:** 🔴 P1 — STRATEGIC DIRECTIVE
**Issued by:** The Boss
**Timestamp:** 2026-06-26 23:56 UTC
**Assigned to:** Dex (DevOps)
**Priority:** HIGH — caused HA-002/HA-003 capital-risk emergency

## Problem
When Coolify redeploys the bot service (`ig7sexqj6pnpnbtkn18odyfn`), it spawns a NEW container (suffix `-NNNNNN`) but does NOT terminate the previous one. This left TWO live bot instances running on the same Binance API keys (event 2026-06-26 23:34 UTC), risking duplicate orders and unbounded trade size.

## Required actions
1. **Investigate Coolify deployment config** for the bot service — determine why old containers aren't cleaned up on redeploy.
2. **Add a container lifecycle cleanup** (Coolify `delete -o` equivalent or `docker prune` after deploy) so only ONE bot container exists at any time.
3. **Add a startup guard in the bot itself** (singleton lock already exists via file lock — verify it actually prevents dual-start across DIFFERENT data volumes, since the HA-002 instance bypassed it by using a different volume).
4. **Document the fix** in the deployment runbook.

## Related
- HA-002, HA-003 (dual-instance emergency)
- Issue #91 (systemd unit — alternative hardening path)
- The bot's singleton lock (`crypto_trading_logger - INFO - Acquired singleton lock (PID 1)`) did NOT prevent this because each container had its own data volume. The lock must be host-scoped or the containers must share one volume.

**Deadline:** Next DevOps cycle (before any further code pushes to master).
