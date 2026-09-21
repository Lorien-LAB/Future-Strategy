"""Runtime parallelism must preserve causal calibration and failure diagnostics."""
from dataclasses import asdict, replace
import json
import pickle
from concurrent.futures import ProcessPoolExecutor
import pytest
import backtest.training as training
from backtest.__main__ import parser
from backtest.config import Config
from backtest.data import MarketData, read_market, read_specs
from backtest.demo import generate
from backtest.engine import Engine, SimulationError
from backtest.pipeline import calibrate, calibration_config_hash, run
from backtest.training import fit_schedule
from conftest import configuration, market, model, specs


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    root = generate(tmp_path_factory.mktemp("parallel") / "demo")
    return (read_market(root / "market"), read_specs(root / "specs.csv"),
            replace(Config.load(root / "config.json"), charts=False))


def test_real_spawn_calibration_exact_nonempty_demo(demo, capsys):
    data, book, cfg = demo
    sequential = calibrate(data, book, cfg, workers=1, progress=False)
    parallel = calibrate(data, book, cfg, workers=2, progress=False)
    assert parallel == sequential
    assert any(parallel["models"].values())
    assert any(row.get("vr_observations", 0) > 0 for row in parallel["training_audit"])
    assert "workers" not in asdict(cfg)
    assert parallel["calibration_config_hash"] == calibration_config_hash(cfg)
    assert capsys.readouterr().out == ""


def test_product_view_keeps_full_calendar_and_only_product(demo):
    data, _, _ = demo
    view = data.product_view("SIMX")
    assert view.days == data.days
    assert view.products == ("SIMX",)
    assert view.rows("0001-01-01", "9999-01-01") == data.rows("0001-01-01", "9999-01-01", "SIMX")
    assert view.prefix_hash("2020-01-01", "SIMX") == data.prefix_hash("2020-01-01", "SIMX")


def test_missing_product_sessions_preserve_global_calendar(demo):
    data, book, cfg = demo
    # All gaps precede the first possible position; full calendar still sets history indices.
    rows = [bar for bar in data.rows("0001-01-01", "9999-01-01")
            if not (bar.product == "SIMX" and bar.day in data.days[:3])]
    rows.append(replace(rows[0], day="2018-01-06"))
    changed = MarketData(rows)
    view = changed.product_view("SIMX")
    assert view.by_day["2018-01-06"] == {}
    assert view.days == changed.days
    assert calibrate(changed, book, cfg, workers=2, progress=False) == calibrate(changed, book, cfg, workers=1, progress=False)


def test_simulation_error_pickle_keeps_partial_account():
    data = market(edits={(3, "A202801"): {"close": None, "settlement": None}})
    with pytest.raises(SimulationError, match="missing mark") as caught:
        Engine(data, specs(), configuration(), {2023: {"A": model()}}, cost_bps=2).run()
    restored = pickle.loads(pickle.dumps(caught.value))
    assert str(restored) == str(caught.value)
    assert restored.engine.state() == caught.value.engine.state()
    assert restored.engine.ledger.fills == caught.value.engine.ledger.fills
    assert restored.engine.ledger.fills


def test_parallel_failure_keeps_training_fills_for_pipeline(demo, tmp_path):
    data, book, cfg = demo
    # Remove every close/settlement after sufficient warmup, retaining executable opens.
    broken = MarketData(replace(bar, close=None, settlement=None) if bar.day >= "2018-02-01" else bar
                        for bar in data.rows("0001-01-01", "9999-01-01"))
    with pytest.raises(SimulationError, match="missing mark") as caught:
        calibrate(broken, book, cfg, workers=2, progress=False)
    assert caught.value.engine.ledger.fills
    with pytest.raises(RuntimeError, match="diagnostic artifacts retained"):
        run(broken, book, cfg, tmp_path / "bad", workers=2, progress=False)
    failure = next(tmp_path.glob("bad.failed-*"))
    assert json.loads((failure / "partial_fills.json").read_text()) == caught.value.engine.ledger.fills


@pytest.mark.parametrize("workers", [True, False, 0, -1, 1.5, "2", None])
def test_invalid_workers_rejected_at_all_public_boundaries(workers, tmp_path):
    args = (market(), specs(), configuration())
    for function in (fit_schedule, calibrate):
        with pytest.raises(ValueError, match="workers"):
            function(*args, workers=workers, progress=False)
    with pytest.raises(ValueError, match="workers"):
        run(*args, tmp_path / "unused", bundle={}, workers=workers, progress=False)
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("command", ["calibrate", "run"])
def test_cli_worker_option_validation(command):
    args = [command, "--data-root", "data", "--specs", "specs.csv", "--output", "out"]
    assert parser().parse_args(args).workers == 1
    assert parser().parse_args(args + ["--workers", "2"]).workers == 2
    for value in ("0", "-2", "1.5", "true"):
        with pytest.raises(SystemExit):
            parser().parse_args(args + ["--workers", value])


@pytest.mark.parametrize("workers", [1, 2])
def test_product_progress_is_runtime_only(workers, capsys):
    result = fit_schedule(market(), specs(), configuration(), workers=workers, progress=True)
    output = capsys.readouterr().out
    assert "1/1" in output and "elapsed" in output and "2023" in output
    assert result == fit_schedule(market(), specs(), configuration(), workers=workers, progress=False)


def test_spawn_is_real_and_worker_count_is_bounded(monkeypatch):
    sizes = []
    class ObservedPool(ProcessPoolExecutor):
        def __init__(self, *args, **kwargs):
            sizes.append((kwargs["max_workers"], kwargs["mp_context"].get_start_method()))
            super().__init__(*args, **kwargs)
    def parent_only_failure(*args, **kwargs):
        raise AssertionError("fit_product executed in parent")
    monkeypatch.setattr(training, "ProcessPoolExecutor", ObservedPool)
    monkeypatch.setattr(training, "fit_product", parent_only_failure)
    result = fit_schedule(market(), specs(), configuration(), workers=8, progress=False)
    assert sizes == [(1, "spawn")]
    assert result[1][0]["status"] == "too_few_train_segments"
    with pytest.raises(AssertionError, match="parent"):
        fit_schedule(market(), specs(), configuration(), workers=1, progress=False)


def test_real_two_family_fits_preserve_audit_and_vr_order(demo):
    data, book, cfg = demo
    cfg = replace(cfg, router="training_only", mr_only_from="2023-01-01", cost_edge_enabled=False,
                  trend_breakout_grid=(10,), trend_efficiency_grid=(0.25,), trend_buffer_grid=(0.0,),
                  trend_hard_grid=(1.5,), trend_trailing_grid=(2.0,), trend_exit_grid=(5,))
    expected = calibrate(data, book, cfg, workers=1, progress=False)
    actual = calibrate(data, book, cfg, workers=2, progress=False)
    assert actual == expected
    assert [(row["product"], row["family"]) for row in actual["training_audit"][:4]] == [
        ("SIMX", "mean_reversion"), ("SIMX", "trend_following"),
        ("SIMY", "mean_reversion"), ("SIMY", "trend_following")]


def tied_families(data, book, cfg, name, year):
    # Controlled equal scores isolate the schedule's ordered family tie contract.
    return [(model(name, family=family), [], {"product": name, "family": family, "annualized_return": 1.0})
            for family in ("mean_reversion", "trend_following")]


def test_equal_family_scores_choose_first_in_serial_and_spawn(monkeypatch):
    monkeypatch.setattr(training, "_fit_families", tied_families)
    serial = fit_schedule(market(), specs(), configuration(), workers=1, progress=False)
    parallel = fit_schedule(market(), specs(), configuration(), workers=2, progress=False)
    assert serial == parallel
    assert serial[0][2023]["A"].family == "mean_reversion"
