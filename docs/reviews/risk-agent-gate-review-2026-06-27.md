# RISK-AGENT Binding Review — Regime v2 Robustness Gate (`maxdd-only` default)

**Date:** 2026-06-27
**Reviewer:** risk-agent (independent veto authority)
**Issue:** #72 "Regime v2: evidence-gated multi-signal activation model"
**Proposal:** Adopt `maxdd-only` as the default Regime v2 robustness gate (replacing the legacy `absolute` 3/3 gate).
**Scope:** RESEARCH-ONLY. All code reviewed is uncommitted on disk. No live config, DB, Docker, or order changes were made or are approved by this review.

---

## VERDICT: APPROVE `maxdd-only` (with conditions)

`maxdd-only` is a principled, well-guarded gate that **raises the effective safety floor** relative to the legacy `absolute` gate (which was provably *unsatisfiable-by-design* on crash-straddling segments — even pure cash failed it) while **rejecting every degenerate strategy** the legacy gate's safety relied on (cash, net-negative-but-low-drawdown, and high-drawdown legacy_sol). Approval is **conditional** on the three items in §4. None of those conditions touch live systems; they are documentation + a test-guarantee.

If the author/team prefer the strictly more conservative option, **`segment-aware` is also approvable** as an alternative (see §5). `relative` is **REJECTED** — it is degenerate without the backstop and is not recommended.

---

## 1. What I reviewed

| File | Lines | Role |
|---|---|---|
| `scripts/research_regime_v2_evaluator.py` | `build_route_robustness_gates()` L1387–1579; `_has_monotone_bleed_tail()` L1368; `route_window_return()` L778; `_compound_returns()` L807; `GATE_MODES` L1348 | Core gate logic + anti-cash backstop + diagnostics |
| `scripts/regime_v2_forward_replay.py` | `--window-gate` CLI L395–432; `build_default_settings()` L101 | Wire-up; backstop default preservation L469–473 |
| `scripts/regime_v2_gate_ab_comparison.py` | full file (246 L) | Consolidated A/B harness; pure-verdict re-application |
| `tests/test_regime_v2_forward_replay.py` | gate tests L1425–1719 (10 tests) | Anti-cash guarantee, legacy rejection, no-lookahead, bleed tail |
| `docs/research/regime-v2-scoping-note.md` | full | Background; confirms `absolute` is unsatisfiable-by-design |

## 2. Evidence — code findings (verified by reading)

### 2.1 Anti-cash backstop is real and not bypassable
`build_route_robustness_gates()` L1454–1461: when `require_positive_total_return is None` (the default path), it resolves to **ON** for `relative` and `maxdd-only`, **OFF** for `absolute` and `segment-aware` (which reject cash via the absolute per-window floor anyway). The backstop (`L1523–1524`) ANDs `route_passed` with `positive_total_ok` (route's full compound return > `positive_total_return_floor_pct`, default `0.0`). The only way to disable it is the explicit CLI flag `--window-gate-no-positive-backstop` (L416–420), which the harness docstring labels **"DIAGNOSTIC ONLY"** and which flips `require_positive_total_return=False` (forward_replay L473). There is no silent bypass.

### 2.2 `cash_also_passes` diagnostic is a defense-in-depth signal, not the guard
The diagnostic re-runs the identical gate verdict on a pure 0% route (L1529–1558) and reports `cash_also_passes`. The guard itself is the backstop; the diagnostic merely makes a cash-rewarding misconfiguration **visible**. Test `test_cash_also_passes_diagnostic_flags_a_cash_rewarding_gate` proves the diagnostic flips `True` when the backstop is disabled and `False` when it's on.

### 2.3 Net-positive floor is enforced under `maxdd-only`
Per-window verdict for `maxdd-only` (L1484–1485) is `max_drawdown_pct <= max_window_drawdown_pct` **only** — no absolute-return floor. This is intentional (the crash-straddling window has ~6% maxDD but slightly negative return). The net-positive backstop at the **route level** then requires the *full* compound return to exceed the floor. This is the correct structure: per-window tolerance for a protected-capital crash + route-level rejection of a strategy that loses money overall.

### 2.4 No-lookahead
Slicing is purely chronological contiguous non-overlapping chunks (`L1462–1506`); no window peeks across boundaries. `test_gate_is_no_lookahead_chronological_slicing` confirms chunk membership is invariant.

## 3. Evidence — independent execution (test suite + my own probes)

### 3.1 Test suite
```
$ .venv/bin/python -m pytest tests/test_regime_v2_forward_replay.py -q
......................................                                   [100%]
38 passed in 1.16s
```
The 10 gate-specific tests (L1425–1719) pass, including:
- `test_gate_modes_never_reward_degenerate_cash` — iterates **all 4 modes**, feeds pure cash, asserts `passed=False` for each.
- `test_maxdd_only_gate_clears_slightly_negative_window` — confirms the crash-segment tolerance.
- `test_segment_aware_rejects_monotone_bleed_tail` — confirms the bleed guard.
- `test_cash_also_passes_diagnostic_flags_a_cash_rewarding_gate` — proves the backstop is load-bearing.

### 3.2 My own edge-case probes (the task's specific concerns)

I ran an independent probe harness (`/tmp/risk_probe.py`) constructing synthetic record series. Results:

| Probe | Input | `maxdd-only` verdict | Correct? |
|---|---|---|---|
| Pure cash, 0%/record | 30 × SIDEWAYS | **REJECTED** (all 4 modes reject) | ✅ |
| **Near-zero + low maxDD** (the flagged edge case) | +0.01%/record (route total +0.30%) | **PASSED** 3/3, `pos_ok=True`, `cash_also_passes=False` | ✅ — it IS net-positive & protected; passing is right |
| **Net-NEGATIVE, all windows <15% maxDD** | −0.1%/record (route total −2.96%) | **REJECTED** (`pos_ok=False`) | ✅ — backstop catches "loses money, low drawdown" |
| Net-negative deeper, all windows <15% maxDD | −0.5%/record (route total −13.96%) | **REJECTED** (`pos_ok=False`) | ✅ |
| legacy_sol-style bad strategy | one window draws down 39% | **REJECTED** 2/3 (high-DD window fails) | ✅ matches author's claim |
| Default `require_positive_total_return` | +0.5%/record | maxdd-only→**True**; absolute→**False** | ✅ backstop auto-on for maxdd-only |

**Decisive finding for the flagged edge case:** the task asked *"what if a future selector has small maxDD but near-zero return? does the net-positive backstop cover that?"* — **Yes.** A net-negative strategy that never draws down past 15% is correctly rejected by the route-level `positive_total_ok` check. The backstop covers the case. The only thing that passes `maxdd-only` is a strategy that is (a) under 15% maxDD in *every* sub-window AND (b) net-positive over the full span — which is exactly the safety contract a drawdown-limited capital-protection strategy should be held to.

## 4. Conditions for approval

1. **No live promotion.** This review approves `maxdd-only` **as a research-track acceptance-criteria definition only.** It does **not** approve wiring Regime v2 into `momentum_strategy._update_market_regime`, changing live risk params, DB, Docker, or placing orders. Live promotion remains a separate, explicit, Boss-approved PR. (This is already the project's stated process; I'm restating it as a hard condition.)

2. **Document the chosen gate.** Before any further promotion attempt, the chosen gate mode must be recorded in `docs/promotion-pipeline.md` with the rationale in this review, so the bar is fixed and auditable — not re-picked per run. (Scoping note §4 item 3 already calls for this.)

3. **Keep the backstop non-default-disable.** The `require_positive_total_return` backstop must remain default-ON for `maxdd-only`, and the only disable path must stay behind a clearly-labeled diagnostic flag. The existing `test_gate_modes_never_reward_degenerate_cash` test guards this — it must not be weakened. (Recommend adding an assertion that `maxdd-only` defaults `require_positive_total_return=True` to lock condition 2.1's finding into a test; optional, not blocking.)

## 5. Comparison vs. `segment-aware` (conservative alternative) — which I'd pick

| Property | `maxdd-only` | `segment-aware` |
|---|---|---|
| Cash rejected? | ✅ (backstop) | ✅ (absolute per-window floor) |
| Net-negative-but-low-DD rejected? | ✅ (backstop) | ✅ (absolute per-window floor) |
| Tolerates one crash-straddling window? | ✅ (no per-window return floor) | ✅ (2/3 + no-bleed) |
| Tolerates a bleed-out tail? | ❌ **does NOT check** | ✅ (monotone-bleed-tail detector) |
| Reported selector result (per author) | passes **3/3** (240d & 300d) | passes **2/3** |
| Mechanism complexity | simple (maxDD + backstop) | more (absolute floor + frac + bleed detector) |

`segment-aware` is **strictly more conservative** in one specific way: it rejects a strategy that ends in a sustained monotone decline (≥6 consecutive end-of-window losses) even if 2/3 segments technically pass. `maxdd-only` does not check for a bleed tail at all. That bleed check is a genuine extra safety signal — a strategy can have low window maxDD and be net-positive while still dying at the end, and only `segment-aware` catches that.

**My pick: APPROVE `maxdd-only` as the default**, because:
- It clears the demonstrably-correct bar (net-positive + bounded drawdown in every segment), which is precisely the contract for a drawdown-limited selector.
- `segment-aware`'s extra bleed check is valuable but its 2/3 selector result (vs 3/3) means it is *more likely to reject a genuinely good strategy for incidental end-window noise* — a false-rejection risk on a small 3-window sample where one bad tail day flips the verdict.
- The bleed risk `segment-aware` guards against is **also caught downstream** by the selector's own recent-P&L risk-off layer and equity stop, so it is partially redundant with live risk controls (which this review does not approve activating, but which exist).

**However**, if the team values the extra end-of-window conservatism and is comfortable with the 2/3 selector verdict, `segment-aware` is a fully-approvable alternative — its anti-cash and anti-net-negative properties are sound (both via the absolute per-window floor). I would **not** pick `segment-aware` as the default only because it is more false-rejection-prone on small samples; it is not unsafe.

**`relative` is REJECTED** as a default: it is degenerate without the backstop (passes legacy_sol at ~45% maxDD per the A/B harness), and even with the backstop it conflates "beat a crashing benchmark" with "robust," which is exactly the cash-rewarding failure the backstop exists to paper over. Do not use.

## 6. Explicit statements of what is NOT approved

- ❌ **No live promotion of Regime v2** — `momentum_strategy.py` remains SOL-only.
- ❌ **No changes to live config, risk parameters, DB, Docker, or orders.**
- ❌ **No enabling of futures** (funding/OI signals remain untested on real data; see scoping note §2.5).
- ❌ **No git commit or push** of the uncommitted research code by this review. (Bot-lead handles staging/PR; this review is comment-ready markdown only.)
- ❌ **`relative` gate mode** is not approved as a default.
- ❌ This approval does **not** resolve the three known live-safety defects (BLOCKER A `-2010` misclassification, BLOCKER B idempotency, F2 dormant circuit breaker) noted in the scoping note — Regime v2 promotion must not proceed until those are fixed regardless of gate choice.

---

## 7. Provenance

- Review performed by: risk-agent (independent veto authority, assigned by bot-lead; verdict is binding).
- Method: read all four listed files in full (relevant sections), ran the 38-test suite (all pass), ran an independent 6-probe edge-case harness against `build_route_robustness_gates`.
- Git state at review: working tree has uncommitted research edits in `scripts/research_regime_v2_evaluator.py`, `scripts/regime_v2_forward_replay.py`, `tests/test_regime_v2_forward_replay.py`, new untracked `scripts/regime_v2_gate_ab_comparison.py`. **No files were modified by this review.**
- No live systems were touched. Research-only.

*End of risk-agent review. Binding verdict: APPROVE `maxdd-only` (conditional, per §4).*
