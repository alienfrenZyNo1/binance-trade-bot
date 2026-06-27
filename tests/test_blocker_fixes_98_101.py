"""Regression tests for the BLOCKING defects flagged in #98 and #101.

BLOCKER B — futures idempotency coverage:
    Every ``futures_create_order`` / ``futures_create_algo_order`` call site
    must carry a deterministic ``newClientOrderId`` so that a network timeout
    on retry is rejected as a duplicate instead of placing a second order.

BLOCKER C — circuit breaker baselines seeded eagerly:
    ``Strategy.initialize()`` must call ``_ensure_circuit_breaker_baselines``
    on startup when the breaker is enabled, so the breaker protects capital
    immediately rather than being dormant until the next entry.

These tests assert the contracts structurally by inspecting the live source,
the same proven technique used in ``test_circuit_breaker_integration.py``.
"""
import inspect


# ---------------------------------------------------------------------------
# BLOCKER B — every futures order site must be idempotent
# ---------------------------------------------------------------------------

def test_all_futures_create_order_sites_have_new_client_order_id():
    """Every futures_create_order call site must pass a newClientOrderId.

    A missing id means a timeout+retry can place a duplicate order, which is
    exactly the money-loss scenario this session was meant to eliminate.
    We parse the AST to find every call and assert the keyword is present.
    """
    import ast

    from binance_trade_bot import futures_manager as fm_mod

    src = inspect.getsource(fm_mod)
    # Parse only the module's own calls (not nested imports).
    tree = ast.parse(src)

    targets = {"futures_create_order", "futures_create_algo_order"}
    violations = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match self.client.futures_create_order(...) or .futures_create_algo_order(...)
        if isinstance(func, ast.Attribute) and func.attr in targets:
            kwargs = {kw.arg for kw in node.keywords}
            if "newClientOrderId" not in kwargs:
                violations.append(
                    f"{func.attr} at line {node.lineno}: missing newClientOrderId"
                )

    assert not violations, (
        "BLOCKER B regression: futures order sites without idempotency: "
        + "; ".join(violations)
    )


def test_futures_client_order_id_helper_is_deterministic():
    """The _generate_futures_client_order_id helper must be deterministic so
    retries of the same logical order collide on Binance."""
    from binance_trade_bot.futures_manager import FuturesManager

    a = FuturesManager._generate_futures_client_order_id("CLOSE", "SOLUSDC", 1.5, extra="42")
    b = FuturesManager._generate_futures_client_order_id("CLOSE", "SOLUSDC", 1.5, extra="42")
    assert a == b, "same inputs must produce the same client order id"

    # Different scope / symbol / qty / extra must change the id.
    assert a != FuturesManager._generate_futures_client_order_id("STOP", "SOLUSDC", 1.5, extra="42")
    assert a != FuturesManager._generate_futures_client_order_id("CLOSE", "XRPUSDC", 1.5, extra="42")
    assert a != FuturesManager._generate_futures_client_order_id("CLOSE", "SOLUSDC", 2.0, extra="42")
    assert a != FuturesManager._generate_futures_client_order_id("CLOSE", "SOLUSDC", 1.5, extra="99")


def test_futures_client_order_id_respects_36_char_limit():
    """Binance rejects clientOrderIds longer than 36 characters."""
    from binance_trade_bot.futures_manager import FuturesManager

    cid = FuturesManager._generate_futures_client_order_id(
        "ENTRY", "VERYLONGSYMBOLUSDC", 999999.999, extra="x" * 50
    )
    assert len(cid) <= 36


# ---------------------------------------------------------------------------
# BLOCKER C — circuit breaker baselines seeded eagerly on startup
# ---------------------------------------------------------------------------

def test_initialize_seeds_circuit_breaker_baselines_when_enabled():
    """Strategy.initialize() must call _ensure_circuit_breaker_baselines
    on startup when the breaker is enabled, so protection starts immediately
    instead of being dormant until the next entry (F2 from risk-audit #98)."""
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    src = inspect.getsource(Strategy.initialize)
    assert "_ensure_circuit_breaker_baselines" in src, (
        "REGRESSION: initialize() must eagerly seed circuit breaker baselines "
        "so the breaker is active from startup, not lazily on first entry."
    )
    # Must be gated on the breaker being enabled (don't seed when disabled).
    assert "PORTFOLIO_CIRCUIT_BREAKER_ENABLED" in src, (
        "REGRESSION: eager seeding must be gated on the breaker being enabled."
    )


def test_initialize_seeding_does_not_change_thresholds():
    """The eager-seeding fix must NOT alter the breaker thresholds (3% daily /
    8% weekly). Only the SEEDING timing should change. Threshold changes
    require Boss approval. Assert no threshold constants appear in the
    initialize() seeding block."""
    from binance_trade_bot.strategies.momentum_strategy import Strategy

    src = inspect.getsource(Strategy.initialize)
    # The seeding block must not reference threshold values directly.
    assert "PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT" not in src
    assert "PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT" not in src
