"""Calibration, verified model reuse, immutable run output and comparison."""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import uuid
import pandas as pd
from . import __version__
from .config import Config
from .data import MarketData, SpecBook, file_hashes
from .domain import Model, digest
from .engine import Engine, SimulationError
from .reporting import charts, report, write_csv, write_engine, write_json
from .training import fit_cost_schedule, fit_schedule

SOURCE_REPOSITORY = "Lorien-LAB/Future-Spread-Trader"
SOURCE_COMMIT = "f7bff2b90087ea85a38449aec878c53e3d0bbe68"
SOURCE_PATH = "backtest/strategy/v1-10_Final"
SCHEMA = "calendar-spread-models-v2"


def source_hash() -> str:
    root = Path(__file__).resolve().parent
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob("*.py"))})


def calibration_config_hash(config: Config) -> str:
    values = asdict(config)
    for key in ("end_exclusive", "cost_bps_values", "primary_cost_bps", "charts"):
        values.pop(key)
    return digest(values)


def calibrate(data: MarketData, specs: SpecBook, config: Config, *, progress: bool = True) -> dict:
    models, training_audit = fit_schedule(data, specs, config, progress=progress)
    thresholds, cost_audit = fit_cost_schedule(data, specs, config, models, progress=progress)
    return {"schema": SCHEMA, "version": __version__, "source_hash": source_hash(),
            "calibration_config_hash": calibration_config_hash(config),
            "models": {str(year): {name: asdict(model) for name, model in sorted(values.items())}
                       for year, values in sorted(models.items())},
            "cost_thresholds": {str(year): value for year, value in sorted(thresholds.items())},
            "data_prefix_hashes": {str(year): data.prefix_hash(f"{year}-01-01") for year in models},
            "spec_prefix_hashes": {str(year): specs.fingerprint(f"{year}-01-01") for year in models},
            "training_audit": training_audit, "cost_training_audit": cost_audit}


def unpack_models(bundle: dict, data: MarketData, specs: SpecBook, config: Config):
    if bundle.get("schema") != SCHEMA or bundle.get("source_hash") != source_hash():
        raise ValueError("model bundle schema or code hash mismatch; recalibrate explicitly")
    if bundle.get("calibration_config_hash") != calibration_config_hash(config):
        raise ValueError("model calibration configuration mismatch")
    models = {}
    for raw_year, entries in bundle["models"].items():
        year = int(raw_year)
        cutoff = f"{year}-01-01"
        if bundle["data_prefix_hashes"].get(raw_year) != data.prefix_hash(cutoff):
            raise ValueError(f"historical market prefix changed for {year}")
        if bundle["spec_prefix_hashes"].get(raw_year) != specs.fingerprint(cutoff):
            raise ValueError(f"historical specification prefix changed for {year}")
        values = {name: Model(**parameters) for name, parameters in entries.items()}
        for name, model in values.items():
            if model.product != name or model.fit_end > cutoff:
                raise ValueError(f"invalid model deployment identity/cutoff for {year}/{name}")
        models[year] = values
    required = {int(day[:4]) for day in data.days if config.source_start <= day < config.end_exclusive}
    if required - models.keys():
        raise ValueError(f"model schedule missing years: {sorted(required - models.keys())}")
    thresholds = {int(year): value for year, value in bundle["cost_thresholds"].items()}
    if required - thresholds.keys():
        raise ValueError("cost schedule does not cover the run")
    return models, thresholds


def limitations(config: Config, data: MarketData) -> list[str]:
    output = [
        "Corrected v2 research engine, not an exact numerical reproduction of the old 43.0781% CAGR reference.",
        "Daily trading-date input only. Session timestamps, night-session clocks and asynchronous exchange opens are not modeled.",
        "Both legs use an atomic-fill approximation. No depth, partial fills, legging risk or exchange spread-order matching is simulated.",
        "0/1/2bp is per-leg/per-side proportional friction sensitivity, not a historical exchange/broker commission schedule.",
        "Margin shortfall stops a portfolio run and saves diagnostic state; broker forced liquidation is not modeled.",
        "Parameter fitting uses a fully-invested single-product margin-return index, not a broker account; portfolio replay uses explicit collateral limits.",
        "OPEN positions are marked and retained at the end. Closed-trade statistics alone are not the account return.",
        "The specification catalog must be supplied externally. Unknown specifications never use guessed multipliers.",
        "Strategy design and hyperparameters were developed with historical data; implementation causality is not independent future validation.",
        "Exported state is diagnostic, not a resume checkpoint; deterministic full-prefix replay is the supported restart method.",
    ]
    output.append("MRE/CER observe D+1 open, then submit for a later D+2 market open without rechecking at the fill price. This is a conservative daily execution scenario, not a latency model."
                  if config.execution_mode == "delayed_open" else
                  "NON-LIVE same_open_proxy: observes official opens and assumes fills at the same opens; unsuitable as an executable live performance claim.")
    output.append("F25 scores use all valid candidate OHLC, differing from the old sparse registry. History is added only once upstream open-time eligibility is observed."
                  if config.f25_coverage == "all_valid" else
                  "F25 eligibility depends on an externally supplied retrospective origin registry; future uncovered records follow the explicit missing-value policy.")
    if config.router == "fixed_empirical_research":
        output.append("RETROSPECTIVE routing: the legacy product list was selected using 2021-2026 performance. This run is development evidence, not independent OOS.")
    if config.allow_retro_specs:
        output.append("RETROSPECTIVE specifications permitted: known_at is not enforced. Historical collateral realism is not certified.")
    if any(bar.identity_source == "legacy_month_series_calendar_assumption" for bar in data.rows("0001-01-01", config.end_exclusive)):
        output.append("Month-series identities use a disclosed calendar assumption, not verified vendor rollover metadata.")
    output.append("Cash-sweep keeps non-donor quantities unchanged; old PnL is not implicitly reinvested. Warm-up active_equal rebalancing is explicit and fee-bearing.")
    return output


def run(data: MarketData, specs: SpecBook, config: Config, output: str | Path, *,
        bundle: dict | None = None, origins: set | None = None,
        extra_inputs=(), progress: bool = True) -> dict:
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"output exists; select a new run directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    working = output.parent / f".{output.name}.working-{uuid.uuid4().hex}"
    working.mkdir()
    inputs = tuple(data.paths) + tuple(Path(p).resolve() for p in extra_inputs)
    before = file_hashes(inputs)
    code_before = source_hash()
    try:
        if specs.allow_retrospective != config.allow_retro_specs:
            raise ValueError("SpecBook retrospective policy differs from Config")
        if not any(config.evaluation_start <= day < config.end_exclusive for day in data.days):
            raise ValueError("no observations in the requested evaluation interval")
        bundle = calibrate(data, specs, config, progress=progress) if bundle is None else bundle
        models, thresholds = unpack_models(bundle, data, specs, config)
        write_json(working / "models.json", bundle)
        write_csv(working / "training_selection.csv", bundle["training_audit"])
        write_csv(working / "cost_training_selection.csv", bundle["cost_training_audit"])
        engines, metrics = {}, []
        for cost in config.cost_bps_values:
            engine = Engine(data, specs, config, models, cost_bps=cost,
                            cost_thresholds=thresholds, origins=origins).run()
            engines[cost] = engine
            metrics.append({"cost_bps": cost, **write_engine(working / f"cost_{cost:g}bp", engine, config)})
        if file_hashes(inputs) != before or source_hash() != code_before:
            raise RuntimeError("input data, configuration or code changed while running")
        assumptions = limitations(config, data)
        manifest = {"schema": "calendar-spread-run-v2", "version": __version__,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source_hash": code_before, "input_hashes": before, "model_bundle_hash": digest(bundle),
                    "origin_registry_hash": digest(sorted(origins)) if origins is not None else None,
                    "config": asdict(config), "python": platform.python_version(), "pandas": pd.__version__,
                    "source_reference": {"repository": SOURCE_REPOSITORY, "commit": SOURCE_COMMIT, "path": SOURCE_PATH},
                    "primary_cost_directory": f"cost_{config.primary_cost_bps:g}bp",
                    "legacy_performance_reproduced": False, "limitations": assumptions}
        write_json(working / "manifest.json", manifest)
        write_csv(working / "metrics_by_cost.csv", metrics)
        figures = charts(working, engines, config) if config.charts else []
        (working / "REPORT.md").write_text(report(config, metrics, assumptions, figures), encoding="utf-8")
        working.rename(output)
        return {"output": str(output), "metrics": metrics, "legacy_performance_reproduced": False}
    except Exception as error:
        if isinstance(error, SimulationError):
            write_json(working / "partial_state.json", error.engine.state())
            write_json(working / "partial_ledger.json", error.engine.ledger.events)
            write_json(working / "partial_fills.json", error.engine.ledger.fills)
        write_json(working / "FAILED.json", {"status": "FAILED", "error": str(error), "config": asdict(config),
                                             "input_hashes": before, "source_hash": code_before})
        failed = output.parent / f"{output.name}.failed-{uuid.uuid4().hex[:8]}"
        working.rename(failed)
        raise RuntimeError(f"run failed; diagnostic artifacts retained at {failed}: {error}") from error


def compare(left: str | Path, right: str | Path) -> dict:
    left, right = Path(left), Path(right)
    manifests = [json.loads((path / "manifest.json").read_text(encoding="utf-8")) for path in (left, right)]
    a, b = manifests
    keys = set(a["config"]) | set(b["config"])
    result = {"left": str(left), "right": str(right),
              "config_changes": {key: [a["config"].get(key), b["config"].get(key)] for key in sorted(keys)
                                 if a["config"].get(key) != b["config"].get(key)},
              "code_changed": a["source_hash"] != b["source_hash"],
              "inputs_changed": a["input_hashes"] != b["input_hashes"],
              "models_changed": a["model_bundle_hash"] != b["model_bundle_hash"]}
    differences = {}
    for name in ("orders.csv", "fills.csv", "daily_portfolio.csv"):
        paths = [root / manifest["primary_cost_directory"] / name for root, manifest in zip((left, right), manifests)]
        records = []
        for path in paths:
            import csv
            with path.open(encoding="utf-8", newline="") as handle:
                records.append(list(csv.DictReader(handle)))
        x, y = records
        index = next((i for i in range(min(len(x), len(y))) if x[i] != y[i]), min(len(x), len(y)))
        differences[name] = None if x == y else {"row": index, "left": x[index] if index < len(x) else None,
                                                "right": y[index] if index < len(y) else None}
    result["first_primary_differences"] = differences
    return result
