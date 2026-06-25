"""Tests for pure regime transition planning."""

from binance_trade_bot.regime_transition_planner import plan_regime_transition


def test_bull_to_bear_requires_spot_exit_transfer_and_short_open():
    plan = plan_regime_transition(
        current_regime="bull",
        target_regime="bear",
        holding_coin="JUP",
        has_spot_position=True,
        has_futures_position=False,
        awaiting_reentry=False,
    )

    assert plan.current_regime == "bear" or plan.target_regime == "bear"
    assert plan.requires_spot_exit is True
    assert plan.requires_futures_transfer is True
    assert plan.requires_short_open is True
    assert plan.requires_short_close is False
    assert plan.requires_spot_reentry is False
    assert plan.clear_awaiting_reentry is False
    assert plan.has_actions is True


def test_entering_bear_clears_stale_reentry_state_without_forcing_spot_exit():
    plan = plan_regime_transition(
        current_regime="sideways",
        target_regime="bear",
        holding_coin=None,
        has_spot_position=False,
        has_futures_position=False,
        awaiting_reentry=True,
    )

    assert plan.requires_spot_exit is False
    assert plan.requires_futures_transfer is True
    assert plan.requires_short_open is True
    assert plan.clear_awaiting_reentry is True


def test_bear_to_sideways_requires_short_close_and_spot_reentry():
    plan = plan_regime_transition(
        current_regime="bear",
        target_regime="sideways",
        holding_coin=None,
        has_spot_position=False,
        has_futures_position=True,
        awaiting_reentry=False,
    )

    assert plan.requires_short_close is True
    assert plan.requires_spot_exit is False
    assert plan.requires_futures_transfer is False
    assert plan.requires_short_open is False
    assert plan.requires_spot_reentry is True
    assert plan.set_awaiting_reentry is True


def test_bear_to_bull_without_open_short_still_checks_futures_before_reentry():
    plan = plan_regime_transition(
        current_regime="bear",
        target_regime="bull",
        holding_coin=None,
        has_spot_position=False,
        has_futures_position=False,
        awaiting_reentry=True,
    )

    assert plan.requires_futures_exit_check is True
    assert plan.requires_short_close is False
    assert plan.requires_spot_reentry is True
    assert plan.set_awaiting_reentry is False


def test_same_regime_has_no_side_effects():
    plan = plan_regime_transition(
        current_regime="sideways",
        target_regime="sideways",
        holding_coin="JUP",
        has_spot_position=True,
        has_futures_position=False,
        awaiting_reentry=False,
    )

    assert plan.requires_spot_exit is False
    assert plan.requires_futures_transfer is False
    assert plan.requires_short_open is False
    assert plan.requires_short_close is False
    assert plan.requires_spot_reentry is False
    assert plan.set_awaiting_reentry is False
    assert plan.clear_awaiting_reentry is False
    assert plan.has_actions is False


def test_regime_names_are_normalized_to_lowercase():
    plan = plan_regime_transition(
        current_regime="BULL",
        target_regime="BEAR",
        holding_coin="JUP",
        has_spot_position=True,
        has_futures_position=False,
        awaiting_reentry=False,
    )

    assert plan.current_regime == "bull"
    assert plan.target_regime == "bear"
