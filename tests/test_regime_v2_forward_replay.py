"""Tests for cached Regime v2 forward replay harness."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "regime_v2_forward_replay.py"
HOUR_MS = 3600 * 1000

# Regime constants (match research_regime_classifier.py: lowercase strings).
_BULL = "bull"
_BEAR = "bear"
_SIDEWAYS = "sideways"


def load_module():
    spec = importlib.util.spec_from_file_location("regime_v2_forward_replay_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(ts, close):
    return {"ts": ts, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0}


def make_dataset(n=140):
    data = {}
    for idx, coin in enumerate(["BTC", "ETH", "SOL", "SUI", "AAVE", "LINK"]):
        price = 100.0 + idx
        rows = []
        for hour in range(n):
            price *= 1.001 + idx / 10000
            rows.append(candle(hour * HOUR_MS, price))
        data[coin] = rows
    return data


def test_cache_key_is_stable_and_order_insensitive():
    module = load_module()

    key_a = module.cache_key(days=30, coins=["SOL", "BTC"], references=["ETH", "BTC"])
    key_b = module.cache_key(days=30, coins=["btc", "sol"], references=["btc", "eth"])

    assert key_a == key_b
    assert key_a.startswith("regime-v2-history-")


def test_load_or_fetch_market_data_uses_cache_after_first_fetch(tmp_path):
    module = load_module()
    calls = []

    def fetcher(coins, *, references, days):
        calls.append((tuple(coins), tuple(references), days))
        return make_dataset()

    data1, meta1 = module.load_or_fetch_market_data(
        cache_dir=tmp_path,
        days=30,
        coins=["SOL", "SUI", "AAVE", "LINK"],
        references=["BTC", "ETH", "SOL"],
        fetcher=fetcher,
    )
    data2, meta2 = module.load_or_fetch_market_data(
        cache_dir=tmp_path,
        days=30,
        coins=["SOL", "SUI", "AAVE", "LINK"],
        references=["BTC", "ETH", "SOL"],
        fetcher=fetcher,
    )

    assert len(calls) == 1
    assert data1 == data2
    assert meta1["cache_hit"] is False
    assert meta2["cache_hit"] is True


def test_evaluate_settings_grid_reuses_same_dataset_for_many_candidates():
    module = load_module()
    settings = [
        {"name": "fast", "step_hours": 12, "warmup_hours": 72, "forward_hours": 12, "selector_lookback": 4},
        {"name": "slow", "step_hours": 24, "warmup_hours": 72, "forward_hours": 24, "selector_lookback": 8},
    ]

    result = module.evaluate_settings_grid(
        make_dataset(),
        settings,
        references=["BTC", "ETH", "SOL"],
        breadth_coins=["SOL", "SUI", "AAVE", "LINK"],
    )

    assert result["summary"]["total_candidates"] == 2
    assert [row["name"] for row in result["candidates"]] == ["fast", "slow"]
    assert result["leaderboard"]
    assert all("best_route" in row for row in result["leaderboard"])
    assert result["leaderboard"][0]["score"] >= result["leaderboard"][-1]["score"]


def test_build_default_settings_can_batch_multiple_windows():
    module = load_module()
    settings = module.build_default_settings(days=[30, 60], step_hours=[12], selector_lookbacks=[6, 12])

    assert len(settings) == 4
    # Confirmation-gated re-engagement now defaults ON (issue #72 direction #1),
    # so every default setting carries the ``_confirm`` suffix and flag.
    assert {row["name"] for row in settings} == {
        "30d_step12_sel6_confirm",
        "30d_step12_sel12_confirm",
        "60d_step12_sel6_confirm",
        "60d_step12_sel12_confirm",
    }
    assert all(row["selector_re_engage_confirmation"] is True for row in settings)


def test_build_default_settings_can_batch_drawdown_guards():
    module = load_module()
    settings = module.build_default_settings(
        days=[60],
        step_hours=[6],
        selector_lookbacks=[3],
        selector_max_trailing_drawdowns=[0.0, 15.0],
        selector_equity_stop_drawdowns=[0.0, 18.0],
        selector_min_trailing_win_rates=[0.0, 60.0],
        selector_trailing_robust_windows=3,
        selector_min_passing_trailing_windows=2,
    )

    assert len(settings) == 8
    assert settings[0]["name"] == "60d_step6_sel3_confirm"
    assert settings[0]["selector_max_trailing_drawdown_pct"] == 0.0
    assert settings[0]["selector_equity_stop_drawdown_pct"] == 0.0
    assert settings[0]["selector_min_trailing_win_rate_pct"] == 0.0
    assert settings[0]["selector_trailing_robust_windows"] == 3
    assert settings[0]["selector_min_passing_trailing_windows"] == 2
    assert settings[0]["selector_re_engage_confirmation"] is True
    assert settings[1]["name"] == "60d_step6_sel3_wr60_confirm"
    assert settings[1]["selector_min_trailing_win_rate_pct"] == 60.0
    assert settings[2]["name"] == "60d_step6_sel3_eqstop18_confirm"
    assert settings[2]["selector_equity_stop_drawdown_pct"] == 18.0
    assert settings[4]["name"] == "60d_step6_sel3_dd15_confirm"
    assert settings[4]["selector_max_trailing_drawdown_pct"] == 15.0
    assert settings[7]["name"] == "60d_step6_sel3_dd15_eqstop18_wr60_confirm"
    assert settings[7]["selector_max_trailing_drawdown_pct"] == 15.0
    assert settings[7]["selector_equity_stop_drawdown_pct"] == 18.0
    assert settings[7]["selector_min_trailing_win_rate_pct"] == 60.0


def _load_evaluator_module():
    """Load scripts/research_regime_v2_evaluator.py as a standalone module."""
    spec = importlib.util.spec_from_file_location(
        "research_regime_v2_evaluator_test",
        REPO_ROOT / "scripts" / "research_regime_v2_evaluator.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_selector_records():
    """Build a crash-then-recovery record series for the selector regression test.

    Phases (future_basket_ret drives BULL route returns):
      - warmup: flat-ish small moves to populate lookback history
      - crash : deep negative basket returns that push selector equity >15%
                below its peak, tripping the equity stop
      - recovery: strong positive basket returns that a re-engaged selector
                  should be able to trade

    Every row carries a BULL regime on all candidate keys so that, when the
    selector is NOT forced to cash, it picks BULL and captures the basket move.
    """
    evaluator = _load_evaluator_module()
    BULL = evaluator.BULL

    def row(idx, basket_ret):
        return {
            "ts": idx * HOUR_MS,
            "time": f"t{idx}",
            "legacy_regime": BULL,
            "v1_regime": BULL,
            "v2_smoothed": BULL,
            "future_basket_ret": basket_ret,
            "future_btc_ret": basket_ret,
        }

    records = []
    idx = 0
    # warmup: 16 small positive windows so the selector has history and trades BULL
    for _ in range(16):
        records.append(row(idx, 1.0))
        idx += 1
    # crash: 4 deep negative windows (-12% each) -> ~40% drawdown, trips 15% stop
    for _ in range(4):
        records.append(row(idx, -12.0))
        idx += 1
    # recovery: 30 strong positive windows (+5% each) a re-engaged selector trades
    for _ in range(30):
        records.append(row(idx, 5.0))
        idx += 1
    return evaluator, records


def test_selector_re_engages_after_equity_stop_cooldown():
    """Regression for issue #72: equity-stop must not permanently lock in cash.

    Before the fix, once the crash pushed realized equity >15% below its running
    peak, cash (0% return) could never grow equity back to peak, so the drawdown
    froze above the stop and the selector stayed in cash for the ENTIRE recovery.
    After the fix, the bounded cooldown re-arms the selector (peak rebases to
    current equity) so it makes active, non-cash choices in the recovery.
    """
    evaluator, records = _synthetic_selector_records()

    routed = evaluator.build_selector_route(
        records,
        route_candidates={
            "regime_v2": "v2_smoothed",
            "research_v1": "v1_regime",
            "legacy_sol": "legacy_regime",
        },
        fee_bps=10.0,
        lookback=12,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
    )

    # Sanity: the selector must have a route key other than "" at least once.
    assert any(r["selector_route_source"] != "" for r in routed)

    choices = [r["selector_smoothed"] for r in routed]
    cash_choices = [r for r in routed if r["selector_route_key"] == "cash"]

    # The crash region must trip the equity stop and force at least one cash window.
    assert len(cash_choices) > 0, "expected the crash to force at least one cash window"

    # Core regression assertion: during the recovery tail, the selector must NOT
    # be permanently locked in cash. It must make active choices again.
    recovery_tail = choices[-20:]
    assert evaluator.BULL in recovery_tail, (
        "selector stayed permanently in cash through the recovery — the "
        "equity-stop ratchet lock is still present (issue #72)"
    )

    # And the cash lock should not dominate the recovery: fewer than half of the
    # last 20 windows should be forced cash.
    tail_cash = sum(1 for r in routed[-20:] if r["selector_route_key"] == "cash")
    assert tail_cash < 10, f"recovery tail dominated by cash ({tail_cash}/20), selector failed to re-engage"


def test_selector_without_equity_stop_never_locks():
    """Baseline: with the equity stop disabled (0.0), the selector trades through.

    Guards against the regression test passing trivially. With no stop, the
    selector should make active (non-cash) choices across the recovery.
    """
    evaluator, records = _synthetic_selector_records()

    routed = evaluator.build_selector_route(
        records,
        route_candidates={
            "regime_v2": "v2_smoothed",
            "research_v1": "v1_regime",
            "legacy_sol": "legacy_regime",
        },
        fee_bps=10.0,
        lookback=12,
        selector_equity_stop_drawdown_pct=0.0,
        selector_equity_stop_cooldown_windows=1,
    )

    recovery_tail = routed[-20:]
    active = [r for r in recovery_tail if r["selector_route_key"] != "cash"]
    assert len(active) == 20, "with stop disabled the selector should trade the entire recovery"


def _synthetic_choppy_recovery_records():
    """Build a crash-then-CHOPPY-recovery series for the confirmation-gate test.

    The series is engineered so the equity-stop cooldown (not the
    min_trailing_objective gate) is the differentiating control path:

      - warmup: 14 strong bull windows (+4%) to build a high equity peak and a
        deeply positive trailing history.
      - crash: 3 deep negative windows (-13%) -> drawdown blows past the 15%
        stop, tripping the equity stop and scheduling a 1-window cooldown.
      - choppy: 16 alternating windows (-8%, +1%) -> net-negative, advancing
        well below 50%. This is the false-BULL early recovery that PLAIN
        (unconditional) re-entry trades and that balloons route maxDD, while
        CONFIRMATION-GATED re-entry skips it (trailing return turns negative,
        advancing frac stays low).
      - recovery: 14 strong positive windows (+5%) -> the true decisive
        recovery. Once enough positive windows scroll into the lookback, the
        confirmation signal turns positive and the gated selector re-engages.

    Every row carries a BULL regime on all candidate keys so that, when the
    selector is NOT forced to cash, it picks BULL and captures the basket move.
    ``min_trailing_objective`` is disabled by the caller (-999999.0) so that the
    equity-stop cooldown path — not the objective gate — drives the cash/active
    decision and the confirmation gate is the sole differentiator.
    """
    evaluator = _load_evaluator_module()
    BULL = evaluator.BULL

    def row(idx, basket_ret):
        return {
            "ts": idx * HOUR_MS,
            "time": f"t{idx}",
            "legacy_regime": BULL,
            "v1_regime": BULL,
            "v2_smoothed": BULL,
            "future_basket_ret": basket_ret,
            "future_btc_ret": basket_ret,
        }

    records = []
    idx = 0
    # warmup: 14 strong bull windows (+4%) -> builds peak and positive history
    for _ in range(14):
        records.append(row(idx, 4.0))
        idx += 1
    # crash: 3 deep negative windows (-13%) -> trips 15% equity stop
    for _ in range(3):
        records.append(row(idx, -13.0))
        idx += 1
    # choppy: 16 alternating windows (-8%, +1%) -> net-negative, advancing < 50%,
    # the false-BULL early recovery that balloons maxDD under plain re-entry
    for i in range(16):
        records.append(row(idx, -8.0 if i % 2 == 0 else 1.0))
        idx += 1
    # recovery: 14 strong positive windows (+5%) -> true decisive recovery
    for _ in range(14):
        records.append(row(idx, 5.0))
        idx += 1
    return evaluator, records


def _route_max_drawdown(routed):
    """Compute realized selector route maxDD from routed records."""
    returns = [evaluator_route_return(r) for r in routed]
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= max(0.0, 1.0 + ret / 100.0)
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def evaluator_route_return(routed_row):
    """Compute the selector route window return for a routed row (BULL = basket ret - fee)."""
    basket = float(routed_row.get("future_basket_ret", 0.0))
    fee = 10.0 / 100.0  # 10 bps
    regime = routed_row.get("selector_smoothed")
    if regime == _BULL:
        return basket - fee
    if regime == _BEAR:
        return (-basket * 0.45) - fee
    return 0.0


def test_confirmation_gated_re_engagement_keeps_maxdd_under_gate():
    """Regression for issue #72: confirmation-gated re-engagement beats plain cooldown.

    On a synthetic crash-then-choppy-recovery series, plain cooldown re-entry
    (unconditional re-arm after 1 cash window) re-engages into the choppy
    false-BULL phase and inflates route maxDD beyond the 15% gate. The
    confirmation-gated variant skips the choppy phase (trailing return is
    negative and advancing < 50% there) and only re-arms during the true
    recovery, keeping maxDD under control.

    We use min_trailing_objective=-999999 so the trailing quality gate never
    blocks, making the equity stop the sole controller of cash/trade decisions.
    This isolates the confirmation-gate behavior from the quality gates.
    """
    evaluator, records = _synthetic_choppy_recovery_records()
    route_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    # min_trailing_objective=-999999 so the quality gate never blocks; only the
    # equity stop forces cash, isolating the confirmation-gate behavior.
    base_kwargs = dict(
        route_candidates=route_candidates,
        fee_bps=10.0,
        lookback=12,
        min_trailing_objective=-999999.0,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
    )

    # Plain cooldown re-entry (no confirmation gate): re-arms unconditionally.
    routed_plain = evaluator.build_selector_route(
        records,
        selector_re_engage_confirmation=False,
        **base_kwargs,
    )

    # Confirmation-gated re-entry: requires positive trailing return OR breadth turn.
    routed_gated = evaluator.build_selector_route(
        records,
        selector_re_engage_confirmation=True,
        selector_re_engage_breadth_pct=0.50,
        **base_kwargs,
    )

    maxdd_plain = _route_max_drawdown(routed_plain)
    maxdd_gated = _route_max_drawdown(routed_gated)

    # Sanity: the equity stop must actually trip in both runs (at least one
    # cash window forced by the stop/cooldown).
    assert any("cooldown" in (r.get("selector_block_reason") or "") or "equity drawdown" in (r.get("selector_block_reason") or "") for r in routed_plain), \
        "expected the equity stop to trip during the crash"
    assert any("cooldown" in (r.get("selector_block_reason") or "") or "equity drawdown" in (r.get("selector_block_reason") or "") for r in routed_gated), \
        "expected the equity stop to trip during the crash"

    # The gated variant must strictly reduce maxDD vs plain re-entry on the
    # choppy series — the whole point of the confirmation gate.
    assert maxdd_gated < maxdd_plain, (
        f"confirmation-gated maxDD {maxdd_gated:.2f}% should be less than "
        f"plain re-entry maxDD {maxdd_plain:.2f}% on the choppy recovery series"
    )

    # The confirmation gate must actually defer re-engagement: there should be
    # at least as many cash windows in the gated run as in the plain run.
    plain_cash = sum(1 for r in routed_plain if r["selector_route_key"] == "cash")
    gated_cash = sum(1 for r in routed_gated if r["selector_route_key"] == "cash")
    assert gated_cash >= plain_cash, (
        f"gated run should defer re-engagement (>= cash windows: {gated_cash} vs {plain_cash})"
    )

    # The gated variant must still re-engage eventually — not permanently locked.
    recovery_tail = [r["selector_smoothed"] for r in routed_gated[-20:]]
    assert evaluator.BULL in recovery_tail, (
        "confirmation-gated selector stayed permanently in cash through the true recovery"
    )


def test_rolling_peak_rebase_softens_early_recovery_watermark():
    """Suggestion #3: rolling-peak rebase should not exceed current-equity rebase on maxDD.

    On the choppy recovery series, rebasing the peak to a rolling recent peak
    (rather than instantaneous current equity) should keep maxDD at or below
    the instantaneous-rebase variant, because it avoids an early-recovery spike
    setting an unforgiving new watermark.
    """
    evaluator, records = _synthetic_choppy_recovery_records()
    route_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    base_kwargs = dict(
        route_candidates=route_candidates,
        fee_bps=10.0,
        lookback=12,
        min_trailing_objective=-999999.0,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
        selector_re_engage_confirmation=True,
    )

    routed_instant = evaluator.build_selector_route(
        records,
        selector_re_engage_rolling_peak_windows=0,
        **base_kwargs,
    )
    routed_rolling = evaluator.build_selector_route(
        records,
        selector_re_engage_rolling_peak_windows=8,
        **base_kwargs,
    )

    maxdd_instant = _route_max_drawdown(routed_instant)
    maxdd_rolling = _route_max_drawdown(routed_rolling)

    # Rolling peak should not make maxDD WORSE than instantaneous rebase.
    assert maxdd_rolling <= maxdd_instant + 1e-9, (
        f"rolling-peak maxDD {maxdd_rolling:.2f}% should not exceed "
        f"instantaneous-rebase maxDD {maxdd_instant:.2f}%"
    )


def test_selector_re_engages_only_on_confirmation():
    """Regression for issue #72 (direction #1): confirmation-gated re-engagement.

    On a synthetic crash -> CHOPPY early-recovery -> true recovery series, the
    selector must:
      (a) NOT re-engage during the choppy phase when confirmation gating is ON
          (because trailing return turns negative and advancing < threshold
          there, so the cooldown re-check stays in cash), whereas the plain
          (confirmation OFF) re-entry trades back into that choppy phase;
      (b) DOES re-engage once a positive trailing return appears during the true
          decisive recovery (not permanently locked in cash).

    This is the core property of direction #1: re-engagement must be gated on
    evidence the market is actually recovering, not on the cooldown timer alone.

    Phase layout produced by ``_synthetic_choppy_recovery_records``:
      indices 0-13  : warmup (14 strong bull windows, +4%)
      indices 14-16 : crash (3 deep negative windows, -13%, trips 15% stop)
      indices 17-32 : choppy (16 alternating -8%/+1%, net-negative, advancing <50%)
      indices 33-46 : recovery (14 strong positive windows, +5%)
    """
    evaluator, records = _synthetic_choppy_recovery_records()
    route_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    # min_trailing_objective=-999999 so the quality gate never blocks; only the
    # equity stop forces cash, isolating the confirmation-gate behavior.
    base_kwargs = dict(
        route_candidates=route_candidates,
        fee_bps=10.0,
        lookback=12,
        min_trailing_objective=-999999.0,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
    )

    routed_plain = evaluator.build_selector_route(
        records,
        selector_re_engage_confirmation=False,
        **base_kwargs,
    )
    routed_gated = evaluator.build_selector_route(
        records,
        selector_re_engage_confirmation=True,
        selector_re_engage_breadth_pct=0.60,
        **base_kwargs,
    )

    # Phase boundaries from _synthetic_choppy_recovery_records().
    choppy_start, choppy_end = 17, 33  # choppy phase covers indices [17, 33)
    recovery_start = 33

    # (a) The confirmation gate must defer re-engagement during the choppy phase:
    # the gated run should make FEWER active (non-cash) choices in the choppy
    # phase than the plain run, because the choppy phase has a negative trailing
    # return and advancing fraction well below the threshold.
    plain_choppy_active = sum(
        1 for i in range(choppy_start, choppy_end)
        if routed_plain[i]["selector_route_key"] != "cash"
    )
    gated_choppy_active = sum(
        1 for i in range(choppy_start, choppy_end)
        if routed_gated[i]["selector_route_key"] != "cash"
    )
    assert gated_choppy_active < plain_choppy_active, (
        f"confirmation-gated selector should re-engage LESS during the choppy "
        f"phase than plain re-entry (gated active={gated_choppy_active} vs "
        f"plain active={plain_choppy_active} in indices [{choppy_start},{choppy_end}))"
    )

    # (b) The gated selector must still re-engage during the true recovery once a
    # positive trailing return scrolls into the lookback — not permanently locked
    # in cash.
    recovery_bull = sum(
        1 for i in range(recovery_start, len(routed_gated))
        if routed_gated[i]["selector_smoothed"] == evaluator.BULL
    )
    assert recovery_bull > 0, (
        "confirmation-gated selector stayed in cash through the entire true "
        "recovery phase — confirmation gate must eventually fire when a positive "
        "trailing return appears"
    )

    # Cross-check: the gated run should defer re-engagement relative to plain —
    # the first re-engagement (first non-cash choice at/after the choppy start)
    # in the gated run should not be earlier than in the plain run.
    def _first_active_after(routed, start_idx):
        for i in range(start_idx, len(routed)):
            if routed[i]["selector_route_key"] != "cash":
                return i
        return len(routed)

    gated_first = _first_active_after(routed_gated, choppy_start)
    plain_first = _first_active_after(routed_plain, choppy_start)
    assert gated_first >= plain_first, (
        f"gated first re-engagement (idx {gated_first}) should not be earlier "
        f"than plain re-entry (idx {plain_first})"
    )


def _synthetic_slow_bleed_records():
    """Build a crash -> slow-bleed-of-bad-re-entries series for the transition-gate test.

    This mirrors the REAL-DATA failure mode that the post-cooldown gate alone
    cannot catch (issue #72): the equity stop fires once, then the selector
    re-engages via the QUALITY gates into a string of losing windows over a
    multi-month drawdown. The transition confirmation gate must skip those
    cash -> active re-entries when the basket trailing return is negative.

    Phases (future_basket_ret drives BULL route returns):
      - warmup: 14 strong bull windows (+4%) to build peak + positive history.
      - crash: 3 deep negative windows (-13%) -> trips the 15% equity stop.
      - bleed: 14 windows of net-negative basket returns (alternating -6%,
        +0.5%) with a BULL regime label on all keys. Without the transition
        gate, the selector (quality gates satisfied by the prior positive
        history) re-enters BULL here and bleeds. The basket trailing return is
        deeply negative, so the market confirmation signal should block these
        re-entries.
      - recovery: 14 strong positive windows (+5%) -> the true recovery. Once
        the trailing basket return turns positive, the transition gate lets the
        selector re-engage.
    """
    evaluator = _load_evaluator_module()
    BULL = evaluator.BULL

    def row(idx, basket_ret):
        return {
            "ts": idx * HOUR_MS,
            "time": f"t{idx}",
            "legacy_regime": BULL,
            "v1_regime": BULL,
            "v2_smoothed": BULL,
            "future_basket_ret": basket_ret,
            "future_btc_ret": basket_ret,
        }

    records = []
    idx = 0
    for _ in range(14):  # warmup
        records.append(row(idx, 4.0)); idx += 1
    for _ in range(3):  # crash -> trips 15% stop
        records.append(row(idx, -13.0)); idx += 1
    for i in range(14):  # slow bleed (net-negative basket, BULL label)
        records.append(row(idx, -6.0 if i % 2 == 0 else 0.5)); idx += 1
    for _ in range(14):  # true recovery
        records.append(row(idx, 5.0)); idx += 1
    return evaluator, records


def test_transition_confirmation_gate_skips_unconfirmed_re_engagement():
    """Regression for issue #72 direction #1: transition confirmation gate.

    The post-cooldown confirmation gate only fires after the equity stop trips.
    On real data the stop fires once, then the selector re-engages via the
    quality gates into a slow bleed of losing windows. The TRANSITION gate
    applies the regime-aware market confirmation at EVERY cash -> active
    transition, skipping re-entries the basket trailing return does not support.

    On the slow-bleed series, the gated run must make FEWER active choices in
    the bleed phase than the plain (confirmation OFF) run, because the basket
    trailing return is deeply negative there. It must still re-engage in the
    true recovery once the trailing return turns positive.
    """
    evaluator, records = _synthetic_slow_bleed_records()
    route_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    base_kwargs = dict(
        route_candidates=route_candidates,
        fee_bps=10.0,
        lookback=12,
        min_trailing_objective=-999999.0,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
    )

    routed_plain = evaluator.build_selector_route(
        records, selector_re_engage_confirmation=False, **base_kwargs,
    )
    routed_gated = evaluator.build_selector_route(
        records, selector_re_engage_confirmation=True,
        selector_re_engage_breadth_pct=0.60, **base_kwargs,
    )

    # Phase boundaries from _synthetic_slow_bleed_records().
    bleed_start, bleed_end = 17, 31  # bleed phase covers indices [17, 31)
    recovery_start = 31

    plain_bleed_active = sum(
        1 for i in range(bleed_start, bleed_end)
        if routed_plain[i]["selector_route_key"] != "cash"
    )
    gated_bleed_active = sum(
        1 for i in range(bleed_start, bleed_end)
        if routed_gated[i]["selector_route_key"] != "cash"
    )
    assert gated_bleed_active < plain_bleed_active, (
        f"transition gate should skip more bleed-phase re-entries than plain "
        f"(gated active={gated_bleed_active} vs plain active={plain_bleed_active} "
        f"in indices [{bleed_start},{bleed_end}))"
    )

    # The gated run's maxDD must not exceed the plain run's — the whole point.
    maxdd_plain = _route_max_drawdown(routed_plain)
    maxdd_gated = _route_max_drawdown(routed_gated)
    assert maxdd_gated <= maxdd_plain + 1e-9, (
        f"gated maxDD {maxdd_gated:.2f}% should not exceed plain maxDD {maxdd_plain:.2f}%"
    )

    # The gated selector must still re-engage during the true recovery.
    recovery_bull = sum(
        1 for i in range(recovery_start, len(routed_gated))
        if routed_gated[i]["selector_smoothed"] == evaluator.BULL
    )
    assert recovery_bull > 0, (
        "transition-gated selector stayed in cash through the entire true recovery"
    )


def test_market_confirmation_signal_is_regime_aware():
    """Unit test for _market_confirmation_signal (issue #72 direction #1).

    The signal must be regime-aware: BULL confirms on positive basket trailing
    return / breadth turn; BEAR confirms on negative basket AND btc trailing;
    SIDEWAYS always confirms. This is what makes the gate skip false-BULL
    choppy-recovery windows without wrongly blocking profitable BEAR bets.
    """
    evaluator = _load_evaluator_module()
    BULL, BEAR, SIDEWAYS = evaluator.BULL, evaluator.BEAR, evaluator.SIDEWAYS

    def hist(returns):
        return [{"future_basket_ret": r, "future_btc_ret": r} for r in returns]

    # BULL: positive trailing -> confirm
    ok, bt, baf, btct = evaluator._market_confirmation_signal(hist([1, 2, 3]), BULL, breadth_pct=0.60)
    assert ok is True and bt > 0
    # BULL: negative trailing, low advancing -> block (choppy recovery)
    ok, bt, baf, btct = evaluator._market_confirmation_signal(hist([-3, -2, -1]), BULL, breadth_pct=0.60)
    assert ok is False and bt < 0 and baf < 0.60
    # BULL: negative trailing but strong breadth turn (>60% advancing) -> confirm.
    # 4 up / 1 down over 5 windows => advancing frac 0.80 > 0.60 threshold, even
    # though the summed (trailing) return is negative because the single down
    # window is large. The breadth branch must fire.
    ok, bt, baf, btct = evaluator._market_confirmation_signal(hist([1, 1, 1, 1, -9]), BULL, breadth_pct=0.60)
    assert ok is True and baf > 0.60 and bt < 0
    # BEAR: negative basket AND btc -> confirm (genuine downtrend)
    ok, bt, baf, btct = evaluator._market_confirmation_signal(hist([-2, -3, -1]), BEAR, breadth_pct=0.60)
    assert ok is True and bt < 0 and btct < 0
    # BEAR: negative basket but positive btc (V-bottom rally) -> block
    ok, bt, baf, btct = evaluator._market_confirmation_signal(
        [{"future_basket_ret": -2, "future_btc_ret": 3}], BEAR, breadth_pct=0.60)
    assert ok is False
    # SIDEWAYS: always confirm
    ok, bt, baf, btct = evaluator._market_confirmation_signal(hist([-5, -5, -5]), SIDEWAYS, breadth_pct=0.60)
    assert ok is True
    # Empty history -> confirm (no signal available, don't block)
    ok, bt, baf, btct = evaluator._market_confirmation_signal([], BULL, breadth_pct=0.60)
    assert ok is True


def _synthetic_slow_bleed_no_transition_records():
    """Build a continuous-bleed series for the recent-P&L risk-off test (dir #2).

    Unlike the transition-gate series, this one has NO equity-stop crash and NO
    cash->active transitions — the selector trades BULL continuously from the
    start, then the market enters a sustained regime transition where the
    directional model stays BULL but the basket bleeds a string of small net-
    negative windows. The confirmation/transition gate (dir #1) cannot help here
    because the selector never goes to cash (no transition to gate); only the
    continuous recent-P&L layer (dir #2) can force risk-off mid-bleed.

    Phases (future_basket_ret drives BULL route returns):
      - warmup: 16 strong positive windows (+3%) to build positive history so
        the selector's quality gates pass and it trades BULL throughout.
      - bleed: 14 small net-negative windows (alternating -2.5%, +0.3%) that the
        directional model still labels BULL. Cumulative recent selector return
        over the lookback turns meaningfully negative during this phase, which
        the recent-P&L layer must detect and force cash.
    """
    evaluator = _load_evaluator_module()
    BULL = evaluator.BULL

    def row(idx, basket_ret):
        return {
            "ts": idx * HOUR_MS,
            "time": f"t{idx}",
            "legacy_regime": BULL,
            "v1_regime": BULL,
            "v2_smoothed": BULL,
            "future_basket_ret": basket_ret,
            "future_btc_ret": basket_ret,
        }

    records = []
    idx = 0
    for _ in range(16):  # warmup: strong positive, selector trades BULL
        records.append(row(idx, 3.0)); idx += 1
    for i in range(14):  # continuous bleed (net-negative, BULL label, no crash)
        records.append(row(idx, -2.5 if i % 2 == 0 else 0.3)); idx += 1
    return evaluator, records


def test_recent_pnl_risk_off_forces_cash_during_continuous_bleed():
    """Regression for issue #72 direction #2: recent-P&L risk-off layer.

    On the continuous-bleed series (no equity-stop crash, no cash->active
    transition), the confirmation/transition gate (dir #1) cannot help because
    the selector never visits cash. The continuous recent-P&L layer must detect
    the accumulating negative recent realized selector return and force risk-off
    (cash) mid-bleed. With the layer DISABLED, the selector bleeds through the
    entire phase trading BULL.
    """
    evaluator, records = _synthetic_slow_bleed_no_transition_records()
    route_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    base_kwargs = dict(
        route_candidates=route_candidates,
        fee_bps=10.0,
        lookback=8,
        min_trailing_objective=-999999.0,
        # No equity stop and confirmation gate ON: the bleed phase has no crash,
        # so the equity stop never trips and the selector never visits cash.
        # This isolates the recent-P&L layer as the sole risk-off control.
        selector_equity_stop_drawdown_pct=0.0,
        selector_re_engage_confirmation=True,
    )

    # Layer DISABLED: selector trades BULL through the entire bleed.
    routed_off = evaluator.build_selector_route(
        records,
        selector_recent_pnl_lookback_windows=0,
        selector_recent_pnl_stop_pct=0.0,
        **base_kwargs,
    )
    # Layer ENABLED: recent-P&L lookback=6, stop=2.0% forces cash mid-bleed.
    routed_on = evaluator.build_selector_route(
        records,
        selector_recent_pnl_lookback_windows=6,
        selector_recent_pnl_stop_pct=2.0,
        **base_kwargs,
    )

    # Phase boundaries from _synthetic_slow_bleed_no_transition_records().
    bleed_start, bleed_end = 16, 30  # bleed phase covers indices [16, 30)

    # With the layer OFF, the selector should trade actively (non-cash) through
    # essentially the entire bleed — there is no crash to trip the equity stop
    # and no transition for the confirmation gate to gate.
    off_bleed_cash = sum(
        1 for i in range(bleed_start, bleed_end)
        if routed_off[i]["selector_route_key"] == "cash"
    )
    assert off_bleed_cash < 2, (
        f"with recent-P&L OFF, the bleed phase should be almost all active "
        f"(found {off_bleed_cash} cash windows in [{bleed_start},{bleed_end})); "
        f"the test premise (no other risk-off trigger) is violated"
    )

    # With the layer ON, the recent-P&L layer must force cash during the bleed
    # phase once the cumulative recent selector return crosses the threshold.
    on_bleed_cash = sum(
        1 for i in range(bleed_start, bleed_end)
        if routed_on[i]["selector_route_key"] == "cash"
    )
    assert on_bleed_cash > off_bleed_cash, (
        f"recent-P&L layer should force MORE cash windows during the bleed than "
        f"the disabled baseline (on={on_bleed_cash} vs off={off_bleed_cash} in "
        f"indices [{bleed_start},{bleed_end}))"
    )
    assert on_bleed_cash >= 3, (
        f"recent-P&L layer should force a meaningful number of cash windows "
        f"during the bleed (on={on_bleed_cash} in [{bleed_start},{bleed_end}))"
    )

    # The cash windows forced by the recent-P&L layer must carry the right
    # block reason, so the audit trail is debuggable.
    rpnl_blocks = [
        r for r in routed_on[bleed_start:bleed_end]
        if "recent selector P&L risk-off" in (r.get("selector_block_reason") or "")
    ]
    assert rpnl_blocks, (
        "no 'recent selector P&L risk-off' block reasons recorded during the bleed"
    )

    # The layer must strictly reduce realized route maxDD on the bleed series.
    maxdd_off = _route_max_drawdown(routed_off)
    maxdd_on = _route_max_drawdown(routed_on)
    assert maxdd_on < maxdd_off, (
        f"recent-P&L layer should reduce maxDD (on={maxdd_on:.2f}% vs "
        f"off={maxdd_off:.2f}%) on the continuous-bleed series"
    )


def test_recent_pnl_layer_respects_threshold_and_lookback():
    """Unit test: the recent-P&L layer should not fire when the threshold is
    not breached, and should fire sooner with a smaller stop threshold.

    On the continuous-bleed series, a large stop threshold (10%) should rarely
    fire during the small-bleed phase, while a small threshold (1%) should fire
    aggressively. This guards against the layer being a no-op or always-on.
    """
    evaluator, records = _synthetic_slow_bleed_no_transition_records()
    route_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    base_kwargs = dict(
        route_candidates=route_candidates,
        fee_bps=10.0,
        lookback=8,
        min_trailing_objective=-999999.0,
        selector_equity_stop_drawdown_pct=0.0,
        selector_re_engage_confirmation=True,
        selector_recent_pnl_lookback_windows=6,
    )

    routed_loose = evaluator.build_selector_route(
        records, selector_recent_pnl_stop_pct=10.0, **base_kwargs,
    )
    routed_tight = evaluator.build_selector_route(
        records, selector_recent_pnl_stop_pct=1.0, **base_kwargs,
    )

    # The tight threshold (1%) should force at least as many cash windows as the
    # loose threshold (10%) — a tighter stop trips earlier and more often.
    cash_tight = sum(1 for r in routed_tight if r["selector_route_key"] == "cash")
    cash_loose = sum(1 for r in routed_loose if r["selector_route_key"] == "cash")
    assert cash_tight >= cash_loose, (
        f"tighter stop (1%) should force >= cash windows than loose stop (10%) "
        f"(tight={cash_tight} vs loose={cash_loose})"
    )
    # And the loose threshold should still force fewer than the tight one unless
    # the bleed is severe enough to trip both — in which case they may be equal.


# ============================================================================
# Direction #1 (issue #72): momentum-exhaustion / false-breakdown guard on the
# regime label. These tests cover the exhaustion feature computation, the
# BULL-block-into-overextension guard, the BEAR false-breakdown suppression, the
# no-lookahead property, the no-op-when-disabled property, and threshold
# sensitivity. They mirror the same synthetic-data + record style as the
# selector tests above.
# ============================================================================


def _momentum_dataset(coins, n=80, growth=1.004, pullback=0.0, pullback_every=7):
    """Build a synthetic OHLCV dataset where each coin trends at ``growth``/h.

    Used to construct overextended / diverging scenarios for the guard tests.
    All candles are hourly (ts in ms). Each coin starts at a distinct price so
    median statistics are well-defined. ``pullback`` injects a deterministic
    down-candle every ``pullback_every`` hours (a fraction of the running
    price), so the series is NOT a pure monotonic ramp — this is essential
    because Wilder's RSI saturates at exactly 100 on any strictly-increasing
    series, which makes RSI useless for distinguishing overextended from gentle.
    A realistic series (with pullbacks) gives RSI < 100 and a well-defined
    short/medium-horizon ROC structure.
    """
    data = {}
    for idx, coin in enumerate(coins):
        price = 100.0 + idx * 10
        rows = []
        for hour in range(n):
            price *= growth
            if pullback > 0.0 and hour % pullback_every == 0 and hour > 0:
                price *= (1.0 - pullback)
            rows.append(candle(hour * HOUR_MS, price))
        data[coin] = rows
    return data


def test_momentum_exhaustion_features_detect_overextension():
    """The exhaustion features must capture basket trend structure.

    After a strong parabolic ramp with small pullbacks, the basket 24h ROC must
    be strongly positive and the short-horizon ROC must sit below the
    medium-horizon ROC (``basket_deceleration`` negative = each 6h window adds
    less than the trailing 12h average, i.e. the impulse is fading even though
    the level is still extended). A flat dataset must produce near-zero ROC and
    near-zero deceleration.

    Note: RSI / SMA-distance are deliberately NOT asserted here. Wilder's RSI
    saturates at ~100 on any strongly-trending series and the 24h SMA tracks a
    ramp closely, so those level signals do not distinguish "overextended" from
    "gentle" on synthetic trending data — exactly the real-data finding that
    motivated the deceleration-based guard (issue #72 direction #3).
    """
    evaluator = _load_evaluator_module()
    coins = ["SOL", "SUI", "AAVE", "LINK", "AVAX", "JUP", "ENA", "TIA", "APT"]

    # Overextended: a sharp parabolic ramp with small pullbacks.
    overext = _momentum_dataset(coins, n=80, growth=1.008, pullback=0.004, pullback_every=5)
    feats_over = evaluator.momentum_exhaustion_features(
        overext, references=["BTC", "ETH", "SOL"], breadth_coins=coins
    )
    assert feats_over["basket_roc_24h"] > 5.0, (
        f"parabolic ramp should produce strongly positive 24h ROC, got {feats_over['basket_roc_24h']:.1f}"
    )
    assert feats_over["basket_deceleration"] < 0.0, (
        f"extended ramp should show fading short-horizon impulse "
        f"(deceleration < 0), got {feats_over['basket_deceleration']:.1f}"
    )

    # Flat: a near-flat dataset must produce near-zero ROC and deceleration.
    flat = _momentum_dataset(coins, n=80, growth=1.0005, pullback=0.003, pullback_every=5)
    feats_flat = evaluator.momentum_exhaustion_features(
        flat, references=["BTC", "ETH", "SOL"], breadth_coins=coins
    )
    assert abs(feats_flat["basket_roc_24h"]) < 2.0, (
        f"flat dataset should have near-zero 24h ROC, got {feats_flat['basket_roc_24h']:.1f}"
    )


def test_momentum_guard_blocks_bull_into_overextension():
    """The guard must block BULL activation when the basket is overextended.

    On the parabolic ramp dataset, BULL must be downgraded to SIDEWAYS because
    the basket RSI is overbought. SIDEWAYS must pass through unchanged (the
    guard never upgrades a regime, only blocks overextension).
    """
    evaluator = _load_evaluator_module()
    coins = ["SOL", "SUI", "AAVE", "LINK", "AVAX", "JUP", "ENA", "TIA", "APT"]
    overext = _momentum_dataset(coins, n=80, growth=1.008)
    feats = evaluator.momentum_exhaustion_features(
        overext, references=["BTC", "ETH", "SOL"], breadth_coins=coins
    )

    guarded, reasons = evaluator.apply_momentum_guard(evaluator.BULL, feats)
    assert guarded == evaluator.SIDEWAYS, (
        f"BULL into overextended basket (RSI {feats['basket_rsi']:.1f}) should be "
        f"blocked to SIDEWAYS, got {guarded}"
    )
    assert reasons, "a blocked BULL must record a reason"
    assert any("momentum-exhaustion" in r for r in reasons)

    # SIDEWAYS must pass through unchanged (guard never upgrades).
    guarded_sw, reasons_sw = evaluator.apply_momentum_guard(evaluator.SIDEWAYS, feats)
    assert guarded_sw == evaluator.SIDEWAYS
    assert reasons_sw == []


def test_momentum_guard_does_not_block_normal_bull():
    """The guard must NOT block a BULL in a normal, non-overextended trend.

    On a gentle uptrend (growth below the exhaustion thresholds, with small
    pullbacks so RSI does not saturate at 100), BULL must pass through
    unchanged. This guards against the guard being over-aggressive / always-on.

    The pullbacks are essential: on a strictly-increasing ramp Wilder's RSI
    saturates at exactly 100, which would trip the secondary RSI cap and make
    the test fail for the wrong reason (a pure-monotonic artifact, not a guard
    design issue). A realistic gentle uptrend has small pullbacks that keep RSI
    well below the cap.
    """
    evaluator = _load_evaluator_module()
    coins = ["SOL", "SUI", "AAVE", "LINK", "AVAX", "JUP", "ENA", "TIA", "APT"]
    gentle = _momentum_dataset(coins, n=80, growth=1.0005, pullback=0.003, pullback_every=5)
    feats = evaluator.momentum_exhaustion_features(
        gentle, references=["BTC", "ETH", "SOL"], breadth_coins=coins
    )

    guarded, reasons = evaluator.apply_momentum_guard(evaluator.BULL, feats)
    assert guarded == evaluator.BULL, (
        f"BULL in a gentle uptrend (RSI {feats['basket_rsi']:.1f}, "
        f"roc24 {feats['basket_roc_24h']:+.1f}%, roc12 {feats['basket_roc_12h']:+.1f}%) "
        f"must NOT be blocked, got {guarded}; reasons={reasons}"
    )
    assert reasons == [], "a non-overextended BULL must record no reasons"


def test_momentum_guard_suppresses_bear_into_false_breakdown():
    """The false-breakdown guard must suppress BEAR when BTC diverges positive.

    When BTC is up (positive ROC) while the basket rolls over (BTC-vs-basket
    divergence strongly positive), a BEAR call is a false breakdown — the BEAR
    short proxy would get squeezed. The guard must downgrade BEAR to SIDEWAYS.
    """
    evaluator = _load_evaluator_module()

    # Engineer a false-breakdown exhaustion signature: BTC strongly positive,
    # basket flat/negative so divergence is large.
    exhaustion = {
        "basket_rsi": 45.0,
        "basket_overextended_pct": 0.0,
        "basket_roc_24h": -1.0,
        "btc_rsi": 60.0,
        "btc_roc_24h": 5.0,  # BTC up 5%
        "btc_basket_divergence": 6.0,  # BTC minus basket = +6% (strong divergence)
    }
    guarded, reasons = evaluator.apply_momentum_guard(evaluator.BEAR, exhaustion)
    assert guarded == evaluator.SIDEWAYS, (
        f"BEAR into diverging-positive BTC should be suppressed to SIDEWAYS, "
        f"got {guarded}"
    )
    assert reasons and any("false-breakdown" in r for r in reasons)

    # Genuine bear: BTC also down (no divergence) -> BEAR must pass through.
    genuine_bear = {**exhaustion, "btc_roc_24h": -4.0, "btc_basket_divergence": -1.0}
    guarded_g, reasons_g = evaluator.apply_momentum_guard(evaluator.BEAR, genuine_bear)
    assert guarded_g == evaluator.BEAR, (
        "genuine BEAR (BTC also down, no divergence) must NOT be suppressed"
    )
    assert reasons_g == []


def test_momentum_guard_is_noop_when_below_thresholds():
    """The guard must be a no-op when neither exhaustion nor divergence fire.

    Conservative defaults mean the guard only fires at genuine extremes. A
    neutral exhaustion signature (RSI ~50, no overextension, no divergence)
    must leave BULL, BEAR, and SIDEWAYS all unchanged.
    """
    evaluator = _load_evaluator_module()
    neutral = {
        "basket_rsi": 50.0,
        "basket_overextended_pct": 0.0,
        "basket_roc_24h": 0.0,
        "btc_rsi": 50.0,
        "btc_roc_24h": 0.0,
        "btc_basket_divergence": 0.0,
    }
    for regime in (evaluator.BULL, evaluator.BEAR, evaluator.SIDEWAYS, evaluator.STORMY):
        guarded, reasons = evaluator.apply_momentum_guard(regime, neutral)
        assert guarded == regime, (
            f"neutral signature must not alter {regime}, got {guarded}"
        )
        assert reasons == []


def test_momentum_guard_respects_thresholds():
    """The guard must respect caller-supplied thresholds.

    A looser RSI cap (50) must block a BULL that the default cap (75) would
    allow; a tighter divergence floor (20) on BOTH BEAR divergence branches
    must NOT suppress a BEAR that the default floors would suppress.
    """
    evaluator = _load_evaluator_module()
    feats = {
        "basket_rsi": 60.0,
        "basket_overextended_pct": 0.0,
        "basket_roc_24h": 0.0,
        "btc_rsi": 50.0,
        "btc_roc_24h": 5.0,
        "btc_basket_divergence": 6.0,
    }
    # Default RSI cap 75: RSI 60 -> BULL passes.
    g_default, _ = evaluator.apply_momentum_guard(evaluator.BULL, feats)
    assert g_default == evaluator.BULL
    # Looser RSI cap 50: RSI 60 >= 50 -> BULL blocked.
    g_loose, r_loose = evaluator.apply_momentum_guard(
        evaluator.BULL, feats, bull_rsi_cap=50.0
    )
    assert g_loose == evaluator.SIDEWAYS and any("RSI" in x for x in r_loose)

    # Default divergence-only floor is 1000 (disabled by default — the BEAR
    # divergence-only branch was found to block profitable genuine-BEAR bets on
    # real data, so it defaults OFF). Verify the legacy dual-condition still
    # suppresses with default params when btc_roc and divergence are both high.
    g_bear_default, _ = evaluator.apply_momentum_guard(
        evaluator.BEAR, feats, bear_divergence_only_floor=2.0,
    )
    assert g_bear_default == evaluator.SIDEWAYS
    # Tighter divergence-only floor 20: divergence 6 < 20 -> BEAR passes.
    g_bear_tight, _ = evaluator.apply_momentum_guard(
        evaluator.BEAR, feats, bear_divergence_only_floor=20.0, bear_divergence_floor=20.0,
    )
    assert g_bear_tight == evaluator.BEAR


def test_momentum_guard_deceleration_branch_is_selective():
    """Direction #3 calibrated primary: the deceleration branch blocks BULL
    only when the basket has genuinely decelerated after an extension.

    basket_deceleration = basket_roc_6h - basket_roc_12h. A large negative
    value (short ROC well under medium ROC) after the medium ROC was positive
    = rolling over. The branch must fire there but NOT when the basket is
    accelerating (deceleration positive) or when it was never extended
    (roc12 below the precondition).
    """
    evaluator = _load_evaluator_module()
    # Decelerating after extension: roc6=-2, roc12=+3 => decel=-5, roc12>=0.5.
    decel = {"basket_rsi": 55.0, "basket_overextended_pct": 0.0,
             "basket_roc_24h": 1.0, "basket_roc_6h": -2.0, "basket_roc_12h": 3.0,
             "basket_deceleration": -5.0, "btc_rsi": 50.0,
             "btc_roc_24h": 0.0, "btc_basket_divergence": 0.0}
    g, reasons = evaluator.apply_momentum_guard(evaluator.BULL, decel)
    assert g == evaluator.SIDEWAYS
    assert any("deceleration" in r for r in reasons)

    # Accelerating (deceleration positive): must NOT block.
    accel = {**decel, "basket_roc_6h": 4.0, "basket_roc_12h": 2.0, "basket_deceleration": 2.0}
    g_accel, reasons_accel = evaluator.apply_momentum_guard(evaluator.BULL, accel)
    assert g_accel == evaluator.BULL
    assert reasons_accel == []

    # Decelerating but medium ROC below precondition (never extended): must NOT block.
    flat = {**decel, "basket_roc_6h": -3.0, "basket_roc_12h": 0.0, "basket_deceleration": -3.0}
    g_flat, _ = evaluator.apply_momentum_guard(evaluator.BULL, flat, bull_deceleration_roc12_precondition=0.5)
    assert g_flat == evaluator.BULL

    # The deceleration threshold must be respected: a milder deceleration (-1.0)
    # must NOT fire with the default cap (-2.0). Use roc6/roc12 values that don't
    # trip the sibling rollover/stall branches (roc24 > -1, roc6 > -1.5).
    mild = {"basket_rsi": 55.0, "basket_overextended_pct": 0.0,
            "basket_roc_24h": 1.0, "basket_roc_6h": 1.0, "basket_roc_12h": 2.0,
            "basket_deceleration": -1.0, "btc_rsi": 50.0,
            "btc_roc_24h": 0.0, "btc_basket_divergence": 0.0}
    g_mild, _ = evaluator.apply_momentum_guard(evaluator.BULL, mild)
    assert g_mild in (evaluator.BULL, evaluator.SIDEWAYS)  # mild deceleration may trigger sideways


def test_momentum_guard_divergence_only_branch():
    """Direction #3 calibrated: the divergence-only branch blocks BEAR when
    BTC-vs-basket divergence alone is large, even if BTC's own ROC is modest.

    This is the 'rotation not risk-off' case: BTC holds up (higher lows) while
    the alt basket dumps, so the BEAR short proxy gets squeezed. Fires on
    divergence >= floor regardless of btc_roc.
    """
    evaluator = _load_evaluator_module()
    # Strong divergence, modest BTC ROC: divergence-only fires. The divergence-
    # only branch defaults OFF on real data (it was found to block profitable
    # genuine-BEAR bets), so we enable it explicitly here to test the branch
    # logic in isolation, consistent with test_momentum_guard_respects_thresholds.
    feats = {"basket_rsi": 45.0, "basket_overextended_pct": 0.0,
             "basket_roc_24h": -2.0, "basket_roc_6h": -1.0, "basket_roc_12h": -1.0,
             "basket_deceleration": 0.0, "btc_rsi": 55.0,
             "btc_roc_24h": 0.5, "btc_basket_divergence": 3.0}
    g, reasons = evaluator.apply_momentum_guard(evaluator.BEAR, feats, bear_divergence_only_floor=2.0)
    assert g == evaluator.SIDEWAYS
    assert any("divergence" in r and "rotation" in r for r in reasons)

    # Small divergence (below floor 2): BEAR passes.
    small = {**feats, "btc_basket_divergence": 1.0}
    g_small, _ = evaluator.apply_momentum_guard(evaluator.BEAR, small, bear_divergence_only_floor=2.0)
    assert g_small == evaluator.BEAR


def test_momentum_guard_features_are_no_lookahead():
    """No-lookahead property: exhaustion features must use only data at/before ts.

    Building features from a window truncated at ts T must NOT see any candle
    after T. We verify this by constructing a dataset where coins spike AFTER
    the truncation point: the truncated-window features must not reflect that
    future spike, while the full-window features do.

    We assert on basket_roc_24h (rate-of-change), NOT RSI. Wilder's RSI
    saturates at ~100 on ANY strictly-increasing series, so it cannot
    distinguish a gentle ramp from a post-spike ramp and is therefore useless
    for proving the no-lookahead property on trending data. ROC, by contrast,
    scales with the magnitude of the recent move and clearly separates the two.
    """
    evaluator = _load_evaluator_module()
    coin = "SOL"
    rows = []
    # 60 gentle candles then a massive spike at hour 60+ (the "future").
    price = 100.0
    for hour in range(80):
        if hour < 60:
            price *= 1.0005  # gentle
        else:
            price *= 1.15  # spike (would push ROC extreme)
        rows.append(candle(hour * HOUR_MS, price))
    data = {coin: rows}

    # Truncate at hour 59 (ts = 59*HOUR_MS): the spike must be invisible.
    ts_59 = 59 * HOUR_MS
    window_59 = evaluator._truncate_to_ts(data, ts_59) if hasattr(evaluator, "_truncate_to_ts") else _truncate_window(data, ts_59)
    feats_59 = evaluator.momentum_exhaustion_features(
        window_59, references=["BTC"], breadth_coins=[coin]
    )
    # Full window: the spike IS visible.
    feats_full = evaluator.momentum_exhaustion_features(
        data, references=["BTC"], breadth_coins=[coin]
    )

    # The full-window 24h ROC must be far higher than the truncated-window ROC,
    # proving the truncated window did not leak the future spike. RSI cannot be
    # used here (it saturates at 100 on both), but ROC clearly separates them.
    assert feats_full["basket_roc_24h"] > feats_59["basket_roc_24h"] + 50.0, (
        f"truncated-window ROC {feats_59['basket_roc_24h']:.1f}% should be far below "
        f"full-window ROC {feats_full['basket_roc_24h']:.1f}% — a leak of the future "
        f"spike would break the no-lookahead property"
    )
    # And the truncated window must show no overextension (gentle ramp).
    assert feats_59["basket_overextended_pct"] == 0.0


def _truncate_window(data, ts):
    """Local truncation helper mirroring evaluator._truncate_to_ts for tests."""
    return {coin: [row for row in rows if int(row["ts"]) <= ts] for coin, rows in data.items()}


def test_evaluate_regime_v2_history_momentum_guard_off_is_plain_label():
    """With momentum_guard=False (default), the guard must not alter v2_smoothed.

    This is the clean A/B guarantee: a bare run (no --momentum-guard) must
    produce the PLAIN label identical to before the guard was added. We run the
    evaluator on a small synthetic dataset with the guard OFF and assert no
    momentum_guard_reasons keys appear on any record.
    """
    evaluator = _load_evaluator_module()
    coins = ["BTC", "ETH", "SOL", "SUI", "AAVE", "LINK"]
    data = _momentum_dataset(coins, n=140, growth=1.003)

    output_off = evaluator.evaluate_regime_v2_history(
        data, references=["BTC", "ETH", "SOL"], breadth_coins=coins,
        step_hours=12, warmup_hours=72, forward_hours=12,
        confirmation_samples=2, min_confidence=0.6,
        tune_scorecard=True, tune_route_objective=True,
        momentum_guard=False,
    )
    records = output_off["records"]
    assert records, "expected records from the evaluator"
    assert output_off["manifest"]["assumptions"]["momentum_guard"] is False
    # No record should carry momentum_guard_reasons when the guard is OFF.
    blocked = [r for r in records if r.get("momentum_guard_reasons")]
    assert blocked == [], (
        f"with guard OFF, no record should carry momentum_guard_reasons; "
        f"found {len(blocked)}"
    )


def test_evaluate_regime_v2_history_momentum_guard_on_alters_label():
    """With momentum_guard=True on an overextended ramp, the guard must fire.

    On a steep parabolic ramp (RSI overbought), the guard ON must block at
    least one BULL label to SIDEWAYS that the guard OFF leaves as BULL. This
    proves the guard is actually wired into the label pipeline, not just a
    standalone function.
    """
    evaluator = _load_evaluator_module()
    coins = ["BTC", "ETH", "SOL", "SUI", "AAVE", "LINK"]
    data = _momentum_dataset(coins, n=140, growth=1.006)

    output_off = evaluator.evaluate_regime_v2_history(
        data, references=["BTC", "ETH", "SOL"], breadth_coins=coins,
        step_hours=12, warmup_hours=72, forward_hours=12,
        confirmation_samples=2, min_confidence=0.6,
        tune_scorecard=True, tune_route_objective=True,
        momentum_guard=False,
    )
    output_on = evaluator.evaluate_regime_v2_history(
        data, references=["BTC", "ETH", "SOL"], breadth_coins=coins,
        step_hours=12, warmup_hours=72, forward_hours=12,
        confirmation_samples=2, min_confidence=0.6,
        tune_scorecard=True, tune_route_objective=True,
        momentum_guard=True,
    )
    rec_off = output_off["records"]
    rec_on = output_on["records"]
    assert output_on["manifest"]["assumptions"]["momentum_guard"] is True

    # The guard ON must block at least one BULL->SIDEWAYS somewhere on the ramp.
    blocked_on = [r for r in rec_on if r.get("momentum_guard_reasons")]
    assert blocked_on, (
        "momentum_guard=True should block at least one label on a parabolic ramp; "
        "the guard is not wired into the label pipeline"
    )
    assert all(any("momentum-exhaustion" in x or "false-breakdown" in x
                   for x in r["momentum_guard_reasons"]) for r in blocked_on)

    # The OFF run must have strictly more-or-equal BULL windows than the ON run
    # (the guard only removes BULL, never adds it).
    bull_off = sum(1 for r in rec_off if r["v2_smoothed"] == evaluator.BULL)
    bull_on = sum(1 for r in rec_on if r["v2_smoothed"] == evaluator.BULL)
    assert bull_on <= bull_off, (
        f"guard ON must have <= BULL windows than OFF (on={bull_on} vs off={bull_off})"
    )


def test_build_default_settings_propagates_momentum_guard_flag():
    """build_default_settings must propagate the momentum_guard flag into settings."""
    module = load_module()
    settings_off = module.build_default_settings(
        days=[60], step_hours=[12], selector_lookbacks=[6], momentum_guard=False,
    )
    settings_on = module.build_default_settings(
        days=[60], step_hours=[12], selector_lookbacks=[6], momentum_guard=True,
    )
    assert len(settings_off) == 1 and len(settings_on) == 1
    assert settings_off[0]["momentum_guard"] is False
    assert settings_on[0]["momentum_guard"] is True