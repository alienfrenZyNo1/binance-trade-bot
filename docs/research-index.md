# Research Index

Research scripts are kept as executable script paths for compatibility. This index explains what each script does and how to run it safely.

## Safety rules

- Research scripts must not change live trading state.
- Use public/free Binance data only unless explicitly documented.
- Every profitability claim must include fees/slippage/funding assumptions and out-of-sample or walk-forward evidence.
- Prefer output artifacts with `manifest`, `records`, and `leaderboard` sections.
- Acceptance gates live in `scripts/strategy_acceptance_gates.py`.

## Scripts

| Script | Purpose | Typical smoke command |
|---|---|---|
| `scripts/research_regime_classifier.py` | Multi-signal BULL/SIDEWAYS/BEAR/STORMY classifier with optional futures public signals | `python3 scripts/research_regime_classifier.py --days 3 --coins SOL,SUI,AAVE,LINK --references BTC,ETH,SOL --include-futures --futures-symbols BTCUSDC,ETHUSDC,SOLUSDC --futures-limit 3` |
| `scripts/research_regime_v2_evaluator.py` | Research-only Regime v2 scorecard/evaluator using strategy-utility labels, route-outcome equity curves, walk-forward legacy/v1/v2 comparison, optional label/route-objective tuning, and failure diagnostics | `python3 scripts/research_regime_v2_evaluator.py --days 30 --coins SOL,SUI,AAVE,LINK,AVAX,JUP --step-hours 6 --forward-hours 24 --tune-scorecard --tune-route-objective --output /tmp/regime_v2.json` |
| `scripts/research_bear_futures_backtester.py` | Research-only BEAR futures short simulator with stops, funding, OI context, fees/slippage | `python3 scripts/research_bear_futures_backtester.py --days 3 --symbols ENAUSDC,SOLUSDC --lookback-hours 12 --output /tmp/bear_futures_backtest.json` |
| `scripts/research_bull_momentum_optimizer.py` | BULL momentum walk-forward robustness optimizer with acceptance gates | `python3 scripts/research_bull_momentum_optimizer.py --days 45 --train-days 10 --test-days 5 --windows 3 --max-combos 5 --top-n 3 --output /tmp/bull_momentum_robustness.json` |
| `scripts/research_sideways_chop_backtester.py` | SIDEWAYS/chop strategy research against cash/current-momentum baseline | `python3 scripts/research_sideways_chop_backtester.py --days 30 --symbols SOLUSDC,AVAXUSDC --output /tmp/sideways_chop.json` |
| `optimize_momentum.py` | Focused momentum optimization and forward validation | `python3 optimize_momentum.py --days 3 --max-combos 5 --no-output` |
| `strategy_optimizer.py` | Broader strategy optimization engine across multiple strategy classes | `python3 strategy_optimizer.py --days 30 --max-combos 5 --no-output` |

## Output conventions

Preferred artifact shape:

```json
{
  "manifest": {
    "script": "...",
    "assumptions": {},
    "generated_at": "..."
  },
  "records": [],
  "leaderboard": []
}
```

The leaderboard should be built through `build_research_output()` / acceptance gates where possible.

## Tests

| Area | Tests |
|---|---|
| Regime classifier | `tests/test_regime_classifier.py` |
| Regime v2 evaluator | `tests/test_regime_v2_evaluator.py` |
| BEAR futures | `tests/test_bear_futures_backtester.py` |
| BULL momentum | `tests/test_bull_momentum_optimizer.py` |
| SIDEWAYS/chop | `tests/test_sideways_chop_backtester.py` |
| Acceptance gates | `tests/test_strategy_acceptance_gates.py` |
| Optimizer artifacts | `tests/test_strategy_optimizer_output.py`, `tests/test_optimize_momentum_safety.py` |

Run:

```bash
python3 -m pytest \
  tests/test_regime_classifier.py \
  tests/test_regime_v2_evaluator.py \
  tests/test_bear_futures_backtester.py \
  tests/test_bull_momentum_optimizer.py \
  tests/test_sideways_chop_backtester.py \
  tests/test_strategy_acceptance_gates.py \
  tests/test_strategy_optimizer_output.py \
  tests/test_optimize_momentum_safety.py \
  -q
```

## Compatibility note

Do not move script paths without first adding wrappers or updating system docs/tests. Operators and future agents may rely on the current paths.
