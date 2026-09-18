from dataclasses import asdict, replace
import json
from pathlib import Path
import pytest
from backtest.config import Config
from backtest.data import MarketData, read_market, read_specs
from backtest.demo import generate
from backtest.pipeline import calibrate, calibration_config_hash, compare, run, source_hash, unpack_models
from backtest.training import fit_cost_schedule, fit_schedule
from conftest import configuration, market, model, specs


def bundle_for(data, cfg):
    return {"schema": "calendar-spread-models-v2", "source_hash": source_hash(),
        "calibration_config_hash": calibration_config_hash(cfg),
        "models": {"2023": {"A": asdict(model())}}, "cost_thresholds": {"2023": None},
        "data_prefix_hashes": {"2023": data.prefix_hash("2023-01-01")},
        "spec_prefix_hashes": {"2023": specs().fingerprint("2023-01-01")},
        "training_audit": [], "cost_training_audit": []}


def test_model_reuse_rejects_changed_config_and_code():
    data, cfg = market(), configuration()
    bundle = bundle_for(data, cfg)
    unpack_models(bundle, data, specs(), cfg)
    with pytest.raises(ValueError, match="configuration"):
        unpack_models(bundle, data, specs(), replace(cfg, vr_window=9))
    with pytest.raises(ValueError, match="code hash"):
        unpack_models({**bundle, "source_hash": "wrong"}, data, specs(), cfg)


def test_model_prefix_hash_rejects_historical_change_not_future_append():
    cfg = configuration()
    data = market()
    bundle = bundle_for(data, cfg)
    future = market((1, 2, 5, 4, 3, 4, 8, 7, 6, 100))
    unpack_models(bundle, future, specs(), cfg)
    historical = [replace(data.rows("0001-01-01", "9999-01-01")[0], day="2022-12-30")]
    changed = MarketData(data.rows("0001-01-01", "9999-01-01") + historical)
    with pytest.raises(ValueError, match="historical market prefix"):
        unpack_models(bundle, changed, specs(), cfg)


def test_run_outputs_are_immutable_and_comparable(tmp_path):
    data, cfg = market(), configuration()
    bundle = bundle_for(data, cfg)
    first, second = tmp_path / "one", tmp_path / "two"
    result = run(data, specs(), cfg, first, bundle=bundle, progress=False)
    assert result["legacy_performance_reproduced"] is False
    assert (first / "cost_2bp" / "ledger.jsonl").is_file()
    assert (first / "cost_2bp" / "state.json").is_file()
    run(data, specs(), cfg, second, bundle=bundle, progress=False)
    difference = compare(first, second)
    assert all(value is None for value in difference["first_primary_differences"].values())
    with pytest.raises(FileExistsError):
        run(data, specs(), cfg, first, bundle=bundle, progress=False)


def test_failed_run_retains_partial_ledger_and_fill(tmp_path):
    data = market(edits={(3, "A202801"): {"close": None, "settlement": None}})
    cfg = configuration()
    with pytest.raises(RuntimeError, match="diagnostic artifacts retained"):
        run(data, specs(), cfg, tmp_path / "bad", bundle=bundle_for(data, cfg), progress=False)
    failed = list(tmp_path.glob("bad.failed-*"))
    assert len(failed) == 1
    fills = json.loads((failed[0] / "partial_fills.json").read_text())
    assert fills and fills[0]["kind"] == "entry"
    assert not (tmp_path / "bad").exists()


def test_synthetic_end_to_end_all_stages(tmp_path):
    root = generate(tmp_path / "demo")
    cfg = replace(Config.load(root / "config.json"), charts=False)
    data, book = read_market(root / "market"), read_specs(root / "specs.csv")
    bundle = calibrate(data, book, cfg, progress=False)
    assert len(bundle["models"]) == 4
    assert all(m["fit_end"] <= f"{year}-01-01" for year, items in bundle["models"].items() for m in items.values())
    assert sum(row.get("deployed", False) for row in bundle["cost_training_audit"]) == 2
    result = run(data, book, cfg, root / "run", bundle=bundle, progress=False)
    metrics = {row["cost_bps"]: row for row in result["metrics"]}
    assert all(row["entry_count"] > 0 for row in metrics.values())
    assert metrics[0]["fees_currency"] == 0
    assert metrics[2]["fees_currency"] > 0
    for cost in (0, 1, 2):
        state = json.loads((root / "run" / f"cost_{cost}bp" / "state.json").read_text())
        events = [json.loads(line) for line in (root / "run" / f"cost_{cost}bp" / "ledger.jsonl").read_text().splitlines()]
        assert state["equity"] == pytest.approx(cfg.initial_equity + sum(row["net_pnl"] for row in events))


def test_no_runtime_import_from_original_strategy():
    import ast
    import backtest
    for path in Path(backtest.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "v1_10_final" not in (node.module or "")
                assert "v10_combine" not in (node.module or "")
