# Strategic Directive 001: Alpha Profit Generation — URGENT

**Date:** 2026-06-27
**Issued by:** The Boss (Human Approval Authority)
**Status:** ACTIVE

## Directive

**Profit generation is now the #1 priority.** The team has spent sufficient time on infrastructure — 492 tests, circuit breakers, idempotent orders, monitoring. That phase is complete.

The bot exists to make money. It is currently not doing so. Fix that.

## Capital Constraints — REMOVED

- Capital is NOT the constraint. Additional capital ($500+) is available if a strategy proves itself.
- Do NOT factor "we only have $62" into research decisions. Work in percentages.
- Think in terms of: % return, Sharpe ratio, max drawdown %, profit factor.
- If the math works and risk is managed, capital will follow.

## Futures — AUTHORIZED FOR RESEARCH

- Futures are authorized for **research, backtesting, and paper trading** immediately.
- Live futures deployment still requires The Boss's explicit sign-off (safety rule).
- The two futures strategies showing promise (funding rate carry, mean reversion shorts) should be researched AGGRESSIVELY.
- Build proper simulations with holding costs, funding payments, liquidation risk.

## What "Don't Cut Corners" Means

- Walk-forward validation is mandatory (not just in-sample backtests)
- Fee assumptions must be realistic (0.1% spot, 0.04% futures maker, slippage)
- Risk metrics: max drawdown, Sharpe, Sortino, Calmar — not just win rate
- Every strategy needs a failure mode analysis (what breaks it?)
- Compare to buy-and-hold baseline — if you can't beat holding, don't ship it
- Multiple timeframes, multiple regimes, stress tests

## Research Targets (prioritized)

1. **Funding rate carry (delta-neutral)** — already shows 3.7-5.9% annualized edge. Can we improve it? Multi-pair? Dynamic allocation?
2. **Mean reversion shorts (futures)** — 60-82% win rate on overbought signals. Needs proper simulation with funding costs and holding periods.
3. **Combined strategy** — momentum rotation for regime detection + funding carry for steady income. Can they coexist?
4. **Novel alpha sources** — correlation breakout, cross-pair momentum transfer, volume profile, order flow signals. Think beyond basic indicators.
5. **Regime v2 (#72)** — still relevant as the detection backbone, but it's a means to an end (profit), not the end itself.

## Success Criteria

A strategy is ready for The Boss's review when:
- Walk-forward backtested across 90+ days of data
- Positive expectancy after ALL costs (fees, slippage, funding, spread)
- Max drawdown < 15% in worst historical scenario
- Sharpe ratio > 1.0 (annualized)
- Profit factor > 1.5
- Beaten buy-and-hold over the same period
- Failure mode documented with mitigation plan

## Timeline

Bot-lead's 5-minute loop should be spending **80%+ of its cycles on research** until a candidate strategy meets the success criteria above. This is not a suggestion.

## Escalation

When a strategy meets the success criteria, post a full research package to GitHub (issue or PR) and tag it for The Boss's review. Include:
- Backtest code (reproducible)
- Results tables (walk-forward, not in-sample)
- Risk analysis
- Recommended position sizing
- Capital requirement estimate (in %, not $)

The Boss will review within 24 hours of submission.
