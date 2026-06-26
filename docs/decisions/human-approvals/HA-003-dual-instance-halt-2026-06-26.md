# HA-003: Dual Bot Instance — Emergency Halt Executed

**Status:** ✅ APPROVED & EXECUTED — Rogue instance halted by The Boss
**Timestamp:** 2026-06-26 23:56 UTC
**Decided by:** The Boss (Human Approval Authority)
**Severity:** CRITICAL (capital-risk emergency)
**Review trail:** HA-002 escalation (2026-06-26 23:50 UTC)

## What was requested
Bot-Lead escalated HA-002: two concurrent `python -m binance_trade_bot` processes detected, both sharing the same Binance API keys, racing on the same trading account. Capital-risk emergency requiring Boss action under emergency halt authority.

## Investigation findings
| Container | ID | Data mount | Holds | Canary | Verdict |
|-----------|----|-----------|-------|--------|---------|
| Canonical | `368d43293d1a` (`ig7sexqj6pnpnbtkn18odyfn`) | `/data/binance-bot-data` (live) | INJ ✅ | 🟡 ENABLED ✅ | CORRECT — keep |
| Rogue | `74af5d6d581e` (Coolify redeploy suffix) | anonymous Docker volume | TIA ❌ (stale) | ⚪ DISABLED ❌ | DANGEROUS — kill |

Both containers had identical API keys (`API_KEY`, `API_SECRET_KEY`). The rogue had a stale/wrong account view (believed it held TIA while the real account holds INJ) and NO canary safety caps. If the rogue had fired a trade, it would have placed orders against the wrong position with unlimited size.

## Damage assessment
- **Zero rogue trades executed.** Live DB shows 0 trades since 23:00 UTC (rogue started 23:34 UTC).
- **No duplicate orders, no state corruption.** The rogue was still in scouting/initialization phase when halted (regime confirmation pending, no position taken).
- **Total exposure window:** ~22 minutes (23:34–23:56 UTC).

## Decision: APPROVED — Emergency Halt
Acting under emergency halt authority (Safety Rule #8). Executed at 23:56 UTC:
- `docker stop 74af5d6d581e` → success (exit 0)
- Verified: single `python -m binance_trade_bot` process remains (PID 3537111 in canonical container)
- Verified: canonical container holds INJ, canary enabled, scouting normally

## Conditions
1. **Single-instance deployment going forward.** Coolify must not leave stale containers running after redeploy. This requires investigation by Dex (DevOps) — see directive SD-003.
2. **No capital was lost.** No follow-up trade reconciliation needed.
3. **The systemd adoption (#91) remains the recommended long-term fix** to prevent accidental dual-launch.

## Risk parameters (unchanged)
Per `config/risk-appetite.yaml`: max daily loss 3%, max drawdown 10%, spot + futures 1x, canary mode active (spot cap $75, futures margin cap $50).

**Decision: APPROVED & EXECUTED — capital risk neutralized.**
