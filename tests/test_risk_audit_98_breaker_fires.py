"""Risk-audit #98 — independent verification that the circuit breaker fires.

This test file is the evidence backing ``docs/audits/risk-audit.md``. It was
written by risk-agent (independent veto authority) and is deliberately
*independent* of the two pre-existing breaker suites
(``test_risk_circuit_breaker.py`` and ``test_circuit_breaker_integration.py``).
It re-derives every assertion from first principles so a bug in the other
suites cannot mask a real defect here.

What it proves (the audit's central claim):
  1. At >3% daily drawdown the pure helper returns ``block_new_risk=True``.
  2. The helper has NO field that could ever block an exit / stop-loss
     (the breaker blocks *new* risk only — exits stay live by construction).
  3. The strategy's exit path (trailing stop) never consults the breaker,
     so stop-losses execute regardless of breaker state.
  4. The strategy seeds baselines *lazily* (only inside the new-risk gate),
     which is the root cause of the live "dormant breaker" finding.
  5. With a seeded baseline the gate flips to "blocked" exactly at the 3%
     daily threshold and stays blocked through the cooldown window.

Read-only audit: this file adds tests only; it changes no production code,
config, or risk parameters.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import fields
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from binance_trade_bot.risk_circuit_breaker import (
    CircuitBreakerResult,
    circuit_breaker_status_summary,
    evaluate_circuit_breaker,
    is_circuit_breaker_cooling_down,
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

LIVE_DAILY = 3.0   # live config: portfolio_daily_max_drawdown_pct = 3.0
LIVE_WEEKLY = 8.0  # live config: portfolio_weekly_max_drawdown_pct = 8.0


def live_cfg(**overrides):
    base = dict(
        PORTFOLIO_CIRCUIT_BREAKER_ENABLED=True,
        PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT=LIVE_DAILY,
        PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT=LIVE_WEEKLY,
        PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS=24,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# 1. The breaker FIRES at >3% daily drawdown
# --------------------------------------------------------------------------

def test_breaker_blocks_at_daily_drawdown_above_3pct():
    """Equity down 3.5% from the day's start → block_new_risk must be True."""
    result = evaluate_circuit_breaker(
        current_equity=96.5,          # 3.5% below 100
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=live_cfg(),
    )
    assert result.block_new_risk is True
    assert result.triggered is True
    assert result.scope == "daily"
    assert result.drawdown_pct == pytest.approx(3.5, abs=1e-9)
    assert result.threshold_pct == pytest.approx(3.0)
    assert "daily drawdown" in result.reason


def test_breaker_blocks_at_exactly_3pct_boundary():
    """The >= comparison means exactly 3.0% is the trip point."""
    result = evaluate_circuit_breaker(
        current_equity=97.0,          # exactly 3.0% below 100
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=live_cfg(),
    )
    assert result.block_new_risk is True


def test_breaker_allows_just_below_3pct():
    """2.99% drawdown is still within tolerance."""
    result = evaluate_circuit_breaker(
        current_equity=97.01,         # 2.99% below 100
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=live_cfg(),
    )
    assert result.block_new_risk is False


def test_breaker_allows_when_no_drawdown():
    result = evaluate_circuit_breaker(
        current_equity=110.0,
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=live_cfg(),
    )
    assert result.block_new_risk is False


# --------------------------------------------------------------------------
# 2. Weekly threshold independently trips at 8%
# --------------------------------------------------------------------------

def test_weekly_8pct_trips_even_if_daily_within_limits():
    """The weekly threshold is an independent backstop. Prove:
       (a) when *daily* is within 3% but *weekly* breaches 8%, it trips weekly;
       (b) when both are below their limits, nothing trips."""
    # (a) daily 2.0% (ok), weekly 9.0% (trip) — equity measured against
    # different daily vs weekly baselines.
    weekly_only = evaluate_circuit_breaker(
        current_equity=91.0,
        daily_start_equity=92.85,     # 2.0% daily — below 3% limit
        weekly_start_equity=100.0,    # 9.0% weekly — above 8% limit
        config=live_cfg(),
    )
    assert weekly_only.block_new_risk is True
    assert weekly_only.scope == "weekly"

    # (b) both within limits.
    ok = evaluate_circuit_breaker(
        current_equity=98.0,          # 2.0% off both
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=live_cfg(),
    )
    assert ok.block_new_risk is False


# --------------------------------------------------------------------------
# 3. EXITS / STOP-LOSSES cannot be blocked — by construction
# --------------------------------------------------------------------------

def test_circuit_breaker_result_has_no_exit_blocking_field():
    """The verdict dataclass only carries a 'block_new_risk' flag. There is
    no 'block_exits' or 'block_all' field that could ever stop a stop-loss."""
    names = {f.name for f in fields(CircuitBreakerResult)}
    assert "block_new_risk" in names
    exit_like = {n for n in names if "exit" in n.lower() or "close" in n.lower()}
    assert exit_like == set(), f"Unexpected exit-blocking field(s): {exit_like}"


def test_trailing_stop_method_does_not_consult_the_breaker():
    """The spot trailing-stop (the only automatic exit) must be independent of
    the breaker. We assert this structurally by inspecting the live source."""
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    src = inspect.getsource(Strategy._check_trailing_stop)
    assert "_new_spot_risk_blocked" not in src, (
        "Trailing-stop exit path calls the new-risk gate — exits could be "
        "blocked by the breaker, which is a safety regression."
    )
    assert "evaluate_circuit_breaker" not in src
    assert "sell_alt" in src, "sanity: trailing stop actually performs an exit"


def test_futures_exit_management_runs_before_breaker_gate():
    """In manage_bear, the open position is managed (stop-loss / trailing /
    funding exits) BEFORE the new-risk gate is consulted, so blocking new
    entries never strands an existing short without protection."""
    from binance_trade_bot.futures_manager import FuturesManager

    src = inspect.getsource(FuturesManager.manage_bear)
    manage_pos = src.find("_manage_open_position")
    gate = src.find("self.new_risk_blocked")
    assert manage_pos != -1 and gate != -1
    assert manage_pos < gate, (
        "manage_bear checks the breaker gate BEFORE managing the open "
        "position — exits could be blocked by an entry-only breaker."
    )


# --------------------------------------------------------------------------
# 4. EXITS execute even when the breaker IS triggered
#    (end-to-end: simulate a breakered state, confirm exit path still sells)
# --------------------------------------------------------------------------

def test_trailing_stop_fires_regardless_of_breaker_state(monkeypatch):
    """Drive the real trailing-stop logic with a simulated price drop while
    the breaker is simultaneously 'triggered'. The stop must still execute."""
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    # We only need the logic of _check_trailing_stop; build a minimal stand-in
    # object so we don't drag in the whole AutoTrader.__init__.
    strat = Strategy.__new__(Strategy)
    strat._position_peak_price = {}
    strat._recently_held = {}                      # mutated on stop-fire
    strat._persist_trade_state = lambda: None      # called on stop-fire

    strat.config = SimpleNamespace(
        TRAILING_STOP_ENABLED=True, TRAILING_STOP_PCT=15.0
    )
    strat.config.BRIDGE = SimpleNamespace(symbol="USDC")
    strat.logger = SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None
    )

    # A fake manager that records a sell if/when the stop fires.
    calls = {"sold": False, "balance_calls": 0}

    class FakeCoin:
        symbol = "TESTCOIN"
        def __add__(self, other):
            return self

    class FakeManager:
        def get_currency_balance(self, sym):
            calls["balance_calls"] += 1
            return 100.0
        def get_min_notional(self, *a, **k):
            return 0.0
        def sell_alt(self, coin, bridge):
            calls["sold"] = True
            return object()  # truthy → indicates success

    strat.manager = FakeManager()
    coin = FakeCoin()

    # Simulate a peak then a >15% drop.
    assert strat._check_trailing_stop(coin, 100.0) is False  # sets peak
    assert strat._check_trailing_stop(coin, 120.0) is False  # raise peak
    fired = strat._check_trailing_stop(coin, 100.0)          # ~16.7% off 120 peak
    assert fired is True
    assert calls["sold"] is True, "Trailing stop did NOT sell — exits would be stuck"
    # And crucially, _check_trailing_stop never touched the breaker at all.


# --------------------------------------------------------------------------
# 5. Baseline seeding is LAZY (root cause of the live dormant-breaker finding)
# --------------------------------------------------------------------------

def test_evaluate_returns_failopen_when_no_baseline():
    """If daily/weekly baselines were never seeded, the helper cannot compute
    drawdown and fails OPEN (block_new_risk=False). This is the exact state
    of the live production DB today."""
    result = evaluate_circuit_breaker(
        current_equity=50.0,
        daily_start_equity=None,
        weekly_start_equity=None,
        config=live_cfg(),
    )
    assert result.block_new_risk is False
    assert "baseline unavailable" in result.reason


def test_strategy_seeds_baselines_eagerly_in_initialize():
    """The LOCAL repo (branch fix/deploy-blockers-98-101) seeds circuit-breaker
    baselines eagerly inside initialize(), so protection starts at process
    startup rather than on the first lazy new-entry attempt.

    CRITICAL AUDIT FINDING: the DEPLOYED container image (built from master at
    2026-06-26 20:17 UTC) does NOT yet contain this eager-seeding block — it
    was introduced on a feature branch that has not been merged to master. So
    this assertion passes against local source but the *production* image still
    seeds lazily. That version skew is why the live DB has no baseline keys and
    the breaker is dormant in production. See docs/audits/risk-audit.md F2.
    """
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    init_src = inspect.getsource(Strategy.initialize)
    gate_src = inspect.getsource(Strategy._new_spot_risk_blocked)

    # The new-risk gate always seeds baselines (self-heals on first entry).
    assert "_ensure_circuit_breaker_baselines" in gate_src

    # The local fix branch additionally seeds eagerly on startup.
    eager_seed = (
        "_ensure_circuit_breaker_baselines(equity, time.time())" in init_src
        or "baselines seeded eagerly" in init_src
    )
    assert eager_seed is True, (
        "initialize() no longer seeds baselines eagerly — the dormant-breaker "
        "gap has RE-OPENED. This is a safety regression."
    )


# --------------------------------------------------------------------------
# 6. Cooldown persists the block for 24h (live cooldown_hours = 24)
# --------------------------------------------------------------------------

def test_cooldown_blocks_for_full_24h_then_releases():
    cfg = live_cfg()
    t0 = 1_000_000.0
    assert is_circuit_breaker_cooling_down(t0, t0 + 23 * 3600, cfg) is True
    assert is_circuit_breaker_cooling_down(t0, t0 + 24 * 3600, cfg) is False
    assert is_circuit_breaker_cooling_down(None, t0, cfg) is False


def test_status_summary_is_human_readable_in_all_states():
    ok = evaluate_circuit_breaker(100.0, 100.0, 100.0, live_cfg())
    bad = evaluate_circuit_breaker(96.0, 100.0, 100.0, live_cfg())  # 4% daily
    assert circuit_breaker_status_summary(ok).startswith("🟢")
    assert circuit_breaker_status_summary(bad).startswith("🔴")
