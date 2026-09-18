from dataclasses import replace
import math
import pytest
from backtest.config import Config
from backtest.contracts import adaptive_window
from backtest.domain import Bar, LifeCycle, Model
from backtest.features import PairHistory, close_location, quantile, volatility_ratio
from backtest.gates import F25History, at_open
from backtest.strategy import families, observe_exit
from backtest.training import parameter_grid
from conftest import configuration, market, model, signal


@pytest.mark.parametrize("q,expected", [(0, 0), (0.5, 5), (0.9, 9), (1, 10)])
def test_linear_quantile(q, expected):
    assert quantile([0, 10], q) == pytest.approx(expected)


@pytest.mark.parametrize("values", [{"vr_window": 1}, {"hard_max_weight": 1}, {"cash_buffer": 1},
    {"integer_lots": "false"}, {"cost_grid": ()}, {"sigma_grid": (float("nan"),)},
    {"minimum_train_ar": float("nan")}, {"stop_grid": (1,)}, {"target_grid": (2,)},
    {"cooldown_days": -1}, {"f25_quantile": 1}, {"train_years": True},
    {"router": "fixed_empirical_research"}, {"primary_cost_bps": 9}])
def test_configuration_rejects_invalid_values(values):
    with pytest.raises((ValueError, TypeError)):
        Config(**values)


def test_unknown_configuration_field(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"vr_widnow": 9}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        Config.load(path)


def test_full_parameter_grids():
    assert len(list(parameter_grid("mean_reversion", Config()))) == 500
    assert len(list(parameter_grid("trend_following", Config()))) == 96


def test_router_is_explicitly_retrospective():
    cfg = Config(router="fixed_empirical_research", allow_retrospective=True)
    assert families("B", "2022-01-01", cfg) == ()
    assert families("P", "2022-01-01", cfg) == ("trend_following",)
    assert families("P", "2023-01-01", cfg) == ()
    assert families("B", "2023-01-01", Config()) == ("mean_reversion",)


def test_strict_left_mean_and_population_std():
    data = market((1, 2, 5))
    history = PairHistory()
    pair = signal().features.pair
    for day in data.days:
        history.update(day, pair, data.by_day[day]["A"])
    snapshot = history.snapshot(model(), data.by_day[data.days[-1]]["A"], configuration())
    assert snapshot.mean == 1.5
    assert snapshot.std == 0.5
    assert snapshot.zscore == 7
    assert snapshot.direction == -1


def test_vr_partial_window_and_config_effect():
    near, far = [100, 110, 99, 115], [100, 105, 100, 102]
    assert volatility_ratio(near[:3], far[:3], 10, 2) is not None
    assert volatility_ratio(near, far, 2, 2) != volatility_ratio(near, far, 10, 2)
    assert volatility_ratio(near, [100] * 4, 10, 2) is None
    assert volatility_ratio([100] * 4, far, 10, 2) == 0
    assert model(vr_threshold=0).vr_threshold == 0


def test_close_location_zero_range_invalid_and_value():
    bar = Bar("2023-01-03", "A", "A202805", 10, 10, 10, 10)
    assert close_location(bar) == 0
    assert close_location(replace(bar, close=11, high=12, low=8)) == 0.5
    assert close_location(replace(bar, close=15)) is None
    assert close_location(replace(bar, low=None)) is None


def test_f25_same_close_and_availability_clock():
    cfg = configuration(f25_enabled=True, f25_minimum_history=2)
    history = F25History(cfg)
    history.observe(signal("2023-02-01", 0), "2023-02-02")
    history.observe(signal("2023-02-02", 1), "2023-02-03")
    a = signal("2023-02-06", 0.95)
    threshold, count, passed, _ = history.decision(a.features, enabled=True)
    assert threshold == pytest.approx(0.9) and count == 2 and not passed
    history.observe(a, "2023-02-07")
    assert history.decision(a.features, enabled=True)[:2] == (threshold, count)
    assert history.decision(signal("2023-02-08").features, enabled=True)[1] == 3


def test_f25_warmup_missing_and_registry():
    cfg = configuration(f25_enabled=True, f25_minimum_history=1, f25_warmup="reject", f25_missing="reject")
    history = F25History(cfg)
    assert not history.decision(signal().features, enabled=True)[2]
    history.observe(signal("2023-02-01", 0.5), "2023-02-02")
    assert not history.decision(signal(score=None).features, enabled=True)[2]
    with pytest.raises(ValueError, match="registry"):
        F25History(replace(cfg, f25_coverage="registry"))
    with pytest.raises(ValueError, match="retrospective"):
        F25History(replace(cfg, f25_coverage="registry"), set())
    registered = F25History(replace(cfg, f25_coverage="registry", allow_retrospective=True), set())
    assert registered.feature(signal().features).f25 is None


def test_mre_center_crossing_rejects():
    cfg = configuration(mre_enabled=True)
    passed, _, audit = at_open(signal(), 99, 100, config=cfg, cost_threshold=None, enabled=True)
    assert not passed and not audit["mre_passed"]


def test_absolute_and_signed_exit_are_different_rules():
    absolute, signed = LifeCycle(signal(), 0), LifeCycle(signal(), 0)
    cfg = configuration()
    assert observe_exit(signed, -20, -20, replace(cfg, barrier_mode="signed_residual")) == "convergence"
    for value in (-20, -21):
        assert observe_exit(absolute, value, value, cfg) is None
    assert observe_exit(absolute, -22, -22, cfg) == "divergence"


def test_first_confirmed_exit_is_frozen():
    life = LifeCycle(signal(), 0)
    assert observe_exit(life, 4, 4, configuration()) == "convergence"
    assert observe_exit(life, 100, 100, configuration()) == "convergence"


def test_adaptive_window():
    assert adaptive_window([4, 20, 40]) == 10
    assert adaptive_window([3, 4]) == 5
    assert adaptive_window([]) == 20
