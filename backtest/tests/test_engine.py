from dataclasses import replace
import pytest
from backtest.data import MarketData
from backtest.engine import SimulationError
from conftest import configuration, market, model, replay


def test_append_future_data_preserves_past_fills_and_ledger():
    data = market()
    cutoff = data.days[4]
    prefix = replay(data, configuration(end_exclusive=cutoff))
    full = replay(data)
    assert prefix.ledger.fills == [r for r in full.ledger.fills if r["date"] < cutoff]
    assert prefix.ledger.events == [r for r in full.ledger.events if r["date"] < cutoff]
    assert prefix.daily == [r for r in full.daily if r["date"] < cutoff]
    assert prefix.orders == [r for r in full.orders if r["date"] < cutoff]
    assert "A" in prefix.ledger.positions


def test_end_of_sample_exports_unfinished_order_and_position():
    data = market()
    pending_only = replay(data, configuration(end_exclusive=data.days[3]))
    assert pending_only.state()["pending_entries"]
    assert pending_only.ledger.fills == []
    entered = replay(data, configuration(end_exclusive=data.days[4]))
    assert len(entered.state()["open_positions"]) == 1
    assert entered.status == "COMPLETE_WITH_OPEN_STATE"


def test_missing_future_mark_preserves_historical_fills():
    data = market(edits={(3, "A202801"): {"close": None, "settlement": None}})
    with pytest.raises(SimulationError) as failure:
        replay(data)
    engine = failure.value.engine
    assert len(engine.ledger.fills) == 1
    assert engine.ledger.fills[0]["date"] == data.days[3]
    assert engine.state()["open_positions"]
    assert engine.status == "FAILED"


def test_missing_exit_open_keeps_exit_pending_and_then_executes():
    data = market(edits={(5, "A202801"): {"open": None}})
    prefix = replay(data, configuration(end_exclusive=data.days[6]))
    assert "A" in prefix.ledger.positions
    assert prefix.lives["A"].pending_exit == "convergence"
    assert any(row["action"] == "exit_pending" for row in prefix.orders)
    full = replay(data)
    assert full.ledger.closed[0]["exit_date"] == data.days[6]


def test_execution_day_final_oi_does_not_cancel_prior_open():
    data = market(edits={(3, "A202801"): {"open_interest": 0, "volume": 0}})
    result = replay(data)
    assert result.ledger.fills[0]["date"] == data.days[3]
    assert result.ledger.fills[0]["kind"] == "entry"


def test_observed_open_gate_and_actual_fill_are_different_dates():
    data = market()
    cfg = configuration(mre_enabled=True, execution_mode="delayed_open")
    result = replay(data, cfg)
    first = result.decisions[0]
    entry = result.ledger.fills[0]
    assert first["observed_date"] == data.days[3]
    assert entry["date"] == data.days[4]
    assert first["signal_id"] == entry["signal_id"]


def test_explicit_same_open_proxy_is_separate():
    data = market()
    result = replay(data, configuration(mre_enabled=True, execution_mode="same_open_proxy"))
    assert result.decisions[0]["observed_date"] == result.ledger.fills[0]["date"]


def test_cross_year_preserves_old_position_and_frozen_model():
    days = ["2022-12-27", "2022-12-28", "2022-12-29", "2022-12-30", "2023-01-03", "2023-01-04"]
    data = market((1, 2, 5, 4, 4, 4), days=days)
    old = model(fit_end="2022-01-01")
    new = model(sigma=100)
    cfg = configuration(source_start=days[0], evaluation_start=days[0], end_exclusive="2023-01-05")
    result = replay(data, cfg, models={2022: {"A": old}, 2023: {"A": new}})
    assert result.ledger.positions["A"].signal.model.model_id == old.model_id
    assert result.ledger.positions["A"].entry_date == "2022-12-30"
    assert len([row for row in result.ledger.fills if row["kind"] == "entry"]) == 1


def test_closed_trade_mae_does_not_read_post_exit_prices():
    normal = replay(market())
    changed = replay(market((1, 2, 5, 4, 3, 4, 100, 100, 100)))
    assert normal.ledger.closed[0] == changed.ledger.closed[0]


def test_shadow_and_actual_rejection_states_are_explicit():
    data = market()
    rejecting_model = model(vr_threshold=1e6)
    schedule = {2023: {"A": rejecting_model}}
    shadow = replay(data, configuration(vr_enabled=True, state_policy="shadow"), models=schedule)
    actual = replay(data, configuration(vr_enabled=True, state_policy="actual"), models=schedule)
    assert shadow.ledger.fills == actual.ledger.fills == []
    assert len(actual.candidates) > len(shadow.candidates)


def test_signal_cannot_use_a_future_year_model():
    future = model(fit_end="2024-01-01")
    with pytest.raises(SimulationError, match="before its training cutoff"):
        replay(models={2023: {"A": future}})
