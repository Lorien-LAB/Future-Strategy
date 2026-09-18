from dataclasses import replace
import math
import pytest
from backtest.allocation import FundingLeg, adaptive_cap, cash_sweep
from backtest.ledger import Ledger
from backtest.metrics import summarize
from conftest import configuration, market, replay, signal, specs


@pytest.mark.parametrize("count,expected", [(1, .5), (2, .5), (3, .6), (4, .7), (5, .8), (20, .8)])
def test_adaptive_cap(count, expected):
    assert adaptive_cap(count, .8) == pytest.approx(expected)


def test_configured_cap_is_consumed():
    assert adaptive_cap(8, .6) == .6


def test_pnl_does_not_change_quantity_and_ledger_telescopes():
    ledger = Ledger(10000)
    spec = specs().get("A", "2023-02-01")
    order = signal(direction=-1)
    ledger.enter(order, "2023-02-06", 2, 110, 100, 110, 100, spec, 2)
    ledger.mark("A", "2023-02-06", "close", 108, 100, spec)
    assert ledger.positions["A"].quantity == 2
    ledger.mark("A", "2023-02-07", "open", 105, 100, spec)
    ledger.reduce("A", "2023-02-07", 2, 105, 100, 2, "convergence")
    fees = 2 * 10 * (210 + 205) * 2 / 10000
    assert ledger.equity == pytest.approx(10000 + 100 - fees)
    ledger.assert_reconciled()
    assert not ledger.positions
    assert ledger.closed[0]["net_pnl"] == pytest.approx(100 - fees)


def test_slippage_is_charged_at_the_fill():
    ledger = Ledger(10000)
    spec = specs().get("A", "2023-02-01")
    ledger.enter(signal(), "2023-02-06", 2, 110, 100, 109.99, 100.01, spec, 0)
    assert ledger.equity == pytest.approx(9999.6)
    assert ledger.fills[0]["slippage_pnl"] == pytest.approx(-.4)


def test_cash_sufficient_keeps_non_donor_quantity_unchanged():
    # Equity includes gains, but no implicit capitalization into additional contracts.
    old = [FundingLeg("OLD", 10, 0, 4)]
    reductions, quantities, audit = cash_sweep(100, old, [FundingLeg("NEW", 10, 0)],
                                               configuration(cash_buffer=0))
    assert reductions == {}
    assert quantities["NEW"] == 5


def test_largest_donor_only_funds_shortage():
    old = [FundingLeg("A", 1, 0, 45), FundingLeg("B", 1, 0, 45)]
    reductions, quantities, _ = cash_sweep(100, old, [FundingLeg("C", 1, 0)], configuration(cash_buffer=0))
    assert reductions == {"A": pytest.approx(100 / 3 - 10)}
    assert quantities["C"] == pytest.approx(100 / 3)


def test_integer_sizing_accounts_for_entry_and_resize_costs():
    cfg = configuration(integer_lots=True, cash_buffer=.1)
    old = [FundingLeg("A", 10, 1, 8)]
    new = [FundingLeg("B", 10, 1)]
    reductions, additions, _ = cash_sweep(100, old, new, cfg)
    new_margin = (8 - reductions.get("A", 0) + additions.get("B", 0)) * 10
    costs = sum(reductions.values()) + sum(additions.values())
    assert new_margin + costs <= 90 + 1e-6
    assert all(float(v).is_integer() for v in list(reductions.values()) + list(additions.values()))


def test_no_pointless_old_resize_when_new_trade_cannot_buy_one_lot():
    reductions, additions, _ = cash_sweep(100, [FundingLeg("A", 10, 0, 4)],
        [FundingLeg("B", 1000, 0)], configuration(integer_lots=True))
    assert reductions == additions == {}


def test_existing_floor_is_respected():
    cfg = configuration(cash_buffer=0, minimum_existing_weight=.4)
    reductions, _, _ = cash_sweep(100, [FundingLeg("A", 1, 0, 90)], [FundingLeg("B", 1, 0)], cfg)
    assert 90 - reductions.get("A", 0) >= 40 - 1e-8


def test_metric_baseline_includes_first_evaluation_event():
    engine = replay(cost=2)
    start = engine.data.days[3]
    metrics = summarize(engine, start, engine.config.end_exclusive)
    assert metrics["baseline_equity"] == engine.daily[2]["equity"]
    assert metrics["fees_currency"] > 0
    assert metrics["market_days"] == len(engine.daily) - 3
    assert metrics["max_drawdown_open_close"] <= metrics["max_drawdown_close"] + 1e-12
