"""Integration-level tests for the portfolio circuit breaker.

These tests exercise the *integration contract* required by issue #98:

  1. When daily drawdown exceeds the configured threshold, the breaker
     returns ``block_new_risk=True`` — which is the exact gate every new
     entry calls before placing a trade (spot rotation, bridge re-entry,
     bridge scout, and futures short entry).
  2. Exits / stop-losses are NEVER routed through the breaker. The breaker
     only guards *new* risk. We assert this at the contract level: the
     ``CircuitBreakerResult`` only carries ``block_new_risk``; there is no
     field that could block an exit. We additionally assert the structural
     ordering in ``FuturesManager.manage_bear`` / the spot trailing stop:
     existing-position management happens *before* the breaker gate.

These tests are deliberately independent of Binance / DB / network — they
use the same pure helpers the live strategy imports, so a pass here is a
pass of the real code path that runs in production.
"""

from types import SimpleNamespace

from binance_trade_bot.risk_circuit_breaker import (
    CircuitBreakerResult,
    evaluate_circuit_breaker,
    is_circuit_breaker_cooling_down,
)


def prod_cfg(**overrides):
    """Config matching the live deployment (3% daily / 8% weekly)."""
    base = {
        "PORTFOLIO_CIRCUIT_BREAKER_ENABLED": True,
        "PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT": 3.0,
        "PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT": 8.0,
        "PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS": 24,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# CONTRACT 1 — breaker blocks NEW entries when daily drawdown > 3%
# ---------------------------------------------------------------------------

def test_breaker_blocks_new_entries_at_3pct_daily_drawdown():
    """The exact production threshold (3% daily). At 3.0% drawdown the
    breaker MUST block new risk — this is the gate wired into every entry
    path (rotation, re-entry, bridge scout, futures short)."""
    result = evaluate_circuit_breaker(
        current_equity=97.0,        # 3.0% below the 100.0 daily start
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=prod_cfg(),
    )

    assert result.block_new_risk is True
    assert result.triggered is True
    assert result.scope == "daily"
    assert result.drawdown_pct >= 3.0


def test_breaker_just_below_threshold_allows_entries():
    """At 2.9% drawdown entries must still be allowed — confirms the gate
    is not overly tight and trips exactly at the configured line."""
    result = evaluate_circuit_breaker(
        current_equity=97.1,        # 2.9% drawdown
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=prod_cfg(),
    )

    assert result.block_new_risk is False
    assert result.triggered is False


def test_breaker_blocks_at_exactly_threshold_boundary():
    """Boundary: at exactly 3.0% the breaker fires (>= comparison)."""
    result = evaluate_circuit_breaker(
        current_equity=97.0,
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=prod_cfg(),
    )
    assert result.block_new_risk is True


# ---------------------------------------------------------------------------
# CONTRACT 2 — exits / stop-losses are NEVER governed by the breaker
# ---------------------------------------------------------------------------

def test_breaker_result_has_no_exit_blocking_field():
    """The CircuitBreakerResult dataclass is the ONLY object entry paths
    inspect. It exposes ``block_new_risk`` (new entries) and nothing that
    could suppress an exit/stop-loss. Assert the contract surface."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(CircuitBreakerResult)}
    assert "block_new_risk" in field_names
    # There is no field such as block_exits / block_stop_loss / block_all.
    forbidden = {"block_exits", "block_stop_loss", "block_all", "halt_trading"}
    assert not (field_names & forbidden), (
        f"Breaker result must not carry any exit-blocking field, got {field_names & forbidden}"
    )


def test_futures_manage_bear_checks_breaker_only_for_new_entries():
    """Structural assertion on FuturesManager.manage_bear ordering.

    Read directly from the source that:
      (a) existing-position management (_manage_open_position) runs FIRST,
      (b) the breaker gate (new_risk_blocked) runs AFTER, immediately
          before _attempt_entry.

    This is what guarantees stop-loss / trailing-stop / funding exits keep
    firing even while the breaker is active. We assert it via the live
    source text so the test breaks loudly if someone reorders the block.
    """
    import inspect

    from binance_trade_bot import futures_manager as fm_mod

    src = inspect.getsource(fm_mod.FuturesManager.manage_bear)
    # Management of an open position must appear before the breaker gate.
    pos_mgmt_idx = src.find("self._manage_open_position()")
    breaker_idx = src.find("self.new_risk_blocked()")
    entry_idx = src.find("self._attempt_entry(")

    assert pos_mgmt_idx != -1, "could not locate _manage_open_position() in manage_bear"
    assert breaker_idx != -1, "could not locate breaker gate in manage_bear"
    assert entry_idx != -1, "could not locate _attempt_entry() in manage_bear"

    assert pos_mgmt_idx < breaker_idx, (
        "REGRESSION: existing position management must run BEFORE the breaker "
        "gate so stops/exits stay live while new entries are blocked."
    )
    assert breaker_idx < entry_idx, (
        "REGRESSION: breaker gate must run BEFORE _attempt_entry."
    )


def test_spot_trailing_stop_does_not_call_breaker():
    """The spot trailing stop is an EXIT. It must never consult the breaker.
    Assert the trailing-stop method body contains no breaker call."""
    import inspect

    from binance_trade_bot.strategies.momentum_strategy import Strategy

    src = inspect.getsource(Strategy._check_trailing_stop)
    assert "circuit_breaker" not in src
    assert "_new_spot_risk_blocked" not in src
    assert "evaluate_circuit_breaker" not in src


def test_all_spot_entry_paths_call_breaker_before_buy():
    """Every NEW-risk path must call _new_spot_risk_blocked() before placing
    a buy. Assert the gate is present and precedes the entry action.

    Entry actions differ per method:
      - _reenter_from_bridge / bridge_scout: direct self.manager.buy_alt(...)
      - scout: self.transaction_through_bridge_pair(...) (which sells then buys)
    """
    import inspect

    from binance_trade_bot.strategies.momentum_strategy import Strategy

    entry_markers = {
        "_reenter_from_bridge": "self.manager.buy_alt(",
        "scout": "self.transaction_through_bridge_pair(",
        "bridge_scout": "self.manager.buy_alt(",
    }

    for method_name, entry_marker in entry_markers.items():
        src = inspect.getsource(getattr(Strategy, method_name))
        gate_idx = src.find("self._new_spot_risk_blocked()")
        entry_idx = src.find(entry_marker)
        assert gate_idx != -1, f"{method_name}: missing breaker gate"
        assert entry_idx != -1, f"{method_name}: missing entry call ({entry_marker})"
        assert gate_idx < entry_idx, (
            f"REGRESSION: {method_name} must check breaker BEFORE the entry action"
        )


# ---------------------------------------------------------------------------
# CONTRACT 3 — cooldown keeps blocking; re-seed after daily reset
# ---------------------------------------------------------------------------

def test_cooldown_persists_block_for_24h_after_trigger():
    """Production cooldown is 24h. A trigger at t0 must keep blocking new
    entries for the full window and release after."""
    cfg = prod_cfg()
    # 1h before expiry -> still blocking
    assert is_circuit_breaker_cooling_down(
        last_triggered_at=1_000_000.0,
        now=1_000_000.0 + 23 * 3600,
        config=cfg,
    ) is True
    # 1h after expiry -> released
    assert is_circuit_breaker_cooling_down(
        last_triggered_at=1_000_000.0,
        now=1_000_000.0 + 25 * 3600,
        config=cfg,
    ) is False


def test_daily_baseline_reset_re_enables_trading_after_new_day():
    """If the daily start equity is re-seeded at a new UTC day to the
    current (recovered) equity, the breaker stops blocking. This models the
    _ensure_circuit_breaker_baselines() rollover behaviour."""
    # Day 1: breaker triggered at 3% drawdown.
    triggered = evaluate_circuit_breaker(
        current_equity=97.0,
        daily_start_equity=100.0,
        weekly_start_equity=100.0,
        config=prod_cfg(),
    )
    assert triggered.block_new_risk is True

    # Day 2: daily baseline rolls over to current equity (97); drawdown 0%.
    recovered = evaluate_circuit_breaker(
        current_equity=97.0,
        daily_start_equity=97.0,   # re-seeded
        weekly_start_equity=100.0,  # weekly still down 3% but under 8%
        config=prod_cfg(),
    )
    # Weekly drawdown is 3% (< 8% threshold) so trading resumes.
    assert recovered.block_new_risk is False
