# Strategic Directive #001 — Initial Project Mobilization
**Issued by:** The Boss (Human Approval Authority)
**Date:** 2026-06-26
**Priority:** MAXIMUM
**Status:** ACTIVE

## SITUATION
The Binance trading bot is LIVE with ~$62 capital. Initial audit identified critical safety gaps.

## DIRECTIVE

### PHASE 0 — STABILIZE (0-24h) — NO STRATEGY CHANGES
| Issue | Assigned To | Action |
|-------|-------------|--------|
| #88 | risk-agent | Enable circuit breaker (3% daily / 8% weekly) |
| #89 | devops + execution | Fix empty database |
| #90 | qa-agent | Fix broken test suite |
| #91 | devops-monitoring | Move off root, add logging |

### PHASE 1 — VALIDATE (24-72h)
| Issue | Assigned To | Action |
|-------|-------------|--------|
| #92 | backtest-agent | Revalidate backtest |
| #93 | bot-lead + qa | Merge Snyk fixes |

### RULES
1. All work via GitHub issues and PRs
2. No agent changes live trading until Phase 0 complete
3. HIGH RISK PRs need risk-agent + final-reviewer approval
4. Boss may halt all trading at any time

*This directive supersedes all prior plans until Phase 0 is complete.*