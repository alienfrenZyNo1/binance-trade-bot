# Experiment Log
## Trading System Research

| ID | Date | Hypothesis | Status | Result | Action |
|----|------|-----------|--------|--------|--------|
| E001 | 2026-06-23 | Live trade frequency is 100x higher than backtest prediction | OBSERVED | 18 trades in 30h vs expected ~0.25/day | Investigate root cause |
| E002 | 2026-06-23 | Backtest +79% is inflated by favorable OOS period | OBSERVED | Train -30%, OOS +65% — suspicious inversion | Revalidate with proper splits |
| E003 | 2026-06-23 | Futures CROSS margin risks entire wallet | CONFIRMED | Binance rejects ISOLATED (error -4175) | Accept for now, add tighter stop |
| E004 | 2026-06-23 | Persisting trade state to DB fixes churning across restarts | DEPLOYED | bot_state table created, state loads on startup, saves on every trade | Monitor when regime turns BULL |
| E005 | 2026-06-23 | 3-cycle confirmation delay prevents noise-driven rotations | DEPLOYED | Edge must persist 3 consecutive scout cycles before executing | Monitor trade frequency |
