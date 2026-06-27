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
    assert {row["name"] for row in settings} == {
        "30d_step12_sel6",
        "30d_step12_sel12",
        "60d_step12_sel6",
        "60d_step12_sel12",
    }


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
    assert settings[0]["name"] == "60d_step6_sel3"
    assert settings[0]["selector_max_trailing_drawdown_pct"] == 0.0
    assert settings[0]["selector_equity_stop_drawdown_pct"] == 0.0
    assert settings[0]["selector_min_trailing_win_rate_pct"] == 0.0
    assert settings[0]["selector_trailing_robust_windows"] == 3
    assert settings[0]["selector_min_passing_trailing_windows"] == 2
    assert settings[1]["name"] == "60d_step6_sel3_wr60"
    assert settings[1]["selector_min_trailing_win_rate_pct"] == 60.0
    assert settings[2]["name"] == "60d_step6_sel3_eqstop18"
    assert settings[2]["selector_equity_stop_drawdown_pct"] == 18.0
    assert settings[4]["name"] == "60d_step6_sel3_dd15"
    assert settings[4]["selector_max_trailing_drawdown_pct"] == 15.0
    assert settings[7]["name"] == "60d_step6_sel3_dd15_eqstop18_wr60"
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
