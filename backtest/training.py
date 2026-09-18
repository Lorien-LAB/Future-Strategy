"""Annual parameter fitting and cost calibration on strictly historical replays."""
from __future__ import annotations
from dataclasses import asdict, replace
from itertools import product as cartesian_product
import math
from .config import Config
from .contracts import adaptive_window, training_segment_lengths
from .data import MarketData, SpecBook
from .domain import Model
from .engine import Engine, SimulationError
from .features import quantile
from .metrics import summarize, yearly_returns
from .strategy import families


def parameter_grid(family: str, config: Config):
    if family == "mean_reversion":
        for sigma, target, stop in cartesian_product(config.sigma_grid, config.target_grid, config.stop_grid):
            yield {"sigma": sigma, "target": target, "stop": stop}
    elif family == "trend_following":
        names = ("breakout", "efficiency", "buffer", "hard_stop", "trailing_stop", "exit_lookback")
        grids = (config.trend_breakout_grid, config.trend_efficiency_grid, config.trend_buffer_grid,
                 config.trend_hard_grid, config.trend_trailing_grid, config.trend_exit_grid)
        for values in cartesian_product(*grids):
            yield dict(zip(names, values, strict=True))
    else:
        raise ValueError(f"unsupported family: {family}")


def _risk_failure(error: Exception) -> bool:
    text = str(error)
    return any(term in text for term in ("non-positive account equity", "margin shortfall"))


def fit_product(data: MarketData, specs: SpecBook, config: Config, name: str, year: int, family: str):
    start, end = f"{year - config.train_years}-01-01", f"{year}-01-01"
    lengths = training_segment_lengths(data, name, start, end, config)
    required = max(1, math.ceil(config.minimum_train_segments * config.train_years / 6))
    audit = {"product": name, "test_year": year, "family": family, "fit_start": start,
             "fit_end": end, "natural_segments": len(lengths), "required_segments": required,
             "selection_source": config.router}
    if len(lengths) < required:
        return None, [], {**audit, "status": "too_few_train_segments"}
    window = adaptive_window(lengths)
    fit_config = replace(config, source_start=start, evaluation_start=start, end_exclusive=end,
                         f25_enabled=False, f25_coverage="all_valid", warmup_allocation="cash_sweep",
                         integer_lots=False, execution_mode="same_open_proxy", charts=False)
    best_model = best_engine = best_metrics = None
    best_score = -math.inf
    attempts = failures = 0
    fingerprint = data.prefix_hash(end, name)
    for parameters in parameter_grid(family, config):
        attempts += 1
        model = Model(name, start, end, family=family,
                      window=window if family == "mean_reversion" else parameters["breakout"],
                      selection_source=config.router, training_fingerprint=fingerprint, **parameters)
        schedule = {y: {name: model} for y in range(year - config.train_years, year)}
        engine = Engine(data, specs, fit_config, schedule, cost_bps=config.training_cost_bps,
                        training=True, gates_enabled=False, product=name)
        try:
            engine.run()
        except SimulationError as error:
            if _risk_failure(error):
                failures += 1
                continue
            raise
        metrics = summarize(engine, start, end)
        if metrics["entry_count"] < config.minimum_train_trades:
            continue
        score = metrics["annualized_return"]
        if score > best_score:
            best_score, best_model, best_engine, best_metrics = score, model, engine, metrics
    audit.update({"window": window, "grid_attempts": attempts, "risk_failed_parameters": failures})
    if best_model is None:
        return None, [], {**audit, "status": "no_trainable_parameter"}
    entry_ids = {row["signal_id"] for row in best_engine.ledger.fills if row["kind"] == "entry"}
    observations = [row for row in best_engine.candidates if row["signal_id"] in entry_ids]
    effective_segments = len({row["segment_id"] for row in observations})
    multiplier = min(2.0, max(0.5, 0.5 + 1.5 * best_model.sigma)) if family == "mean_reversion" else 1.0
    effective_score = effective_segments * multiplier
    annual = yearly_returns(best_engine, start, end)
    worst_year = min(annual.values(), default=0.0)
    required_effective = max(1, math.ceil(config.low_segment_threshold * config.train_years / 6))
    minimum_score = config.minimum_effective_score * config.train_years / 6
    reason = ("train_ar_below_minimum" if best_score < config.minimum_train_ar else
              "too_few_effective_segments" if effective_segments < required_effective and effective_score < minimum_score else
              "worst_year_below_minimum" if worst_year < config.minimum_worst_year else "selected")
    audit.update({"status": reason, "annualized_return": best_score, "worst_year": worst_year,
                  "yearly_returns": annual, "entry_count": best_metrics["entry_count"],
                  "effective_segments": effective_segments, "effective_score": effective_score,
                  "open_positions_at_fit_end": len(best_engine.ledger.positions),
                  "parameters": asdict(best_model)})
    return (best_model, observations, audit) if reason == "selected" else (None, [], audit)


def fit_schedule(data: MarketData, specs: SpecBook, config: Config, *, progress: bool = True):
    models: dict[int, dict[str, Model]] = {}
    audit = []
    years = range(int(config.source_start[:4]), int(config.end_exclusive[:4]) + 1)
    for year in years:
        deployment = f"{year}-01-01"
        if deployment >= config.end_exclusive:
            continue
        selected, observations = {}, []
        for name in data.products:
            candidates = []
            for family in families(name, deployment, config):
                model, features, row = fit_product(data, specs, config, name, year, family)
                audit.append(row)
                if model is not None:
                    candidates.append((model, features, row))
            if not candidates:
                continue
            model, features, row = max(candidates, key=lambda item: item[2]["annualized_return"])
            selected[name] = model
            if model.family == "mean_reversion":
                observations.extend(features)
        valid = [row for row in observations if row["day"] < deployment and row["vr"] is not None
                 and math.isfinite(row["vr"])]
        if any(m.family == "mean_reversion" for m in selected.values()) and config.vr_enabled and not valid:
            raise ValueError(f"{year}: no finite causal VR calibration observations")
        threshold = quantile([row["vr"] for row in valid], config.vr_quantile) if valid else None
        models[year] = {name: replace(model, vr_threshold=threshold, vr_training_count=len(valid),
                                     vr_max_date=max((row["day"] for row in valid), default=None))
                        if model.family == "mean_reversion" else model for name, model in selected.items()}
        audit.append({"test_year": year, "status": "annual_pool", "selected_products": sorted(selected),
                      "vr_threshold": threshold, "vr_observations": len(valid),
                      "vr_max_date": max((row["day"] for row in valid), default=None)})
        if progress:
            print(f"[calibrate] {year}: {len(selected)} products; {len(valid)} VR observations", flush=True)
    return models, audit


def fit_cost_schedule(data: MarketData, specs: SpecBook, config: Config, models, *, progress: bool = True):
    schedule: dict[int, float | None] = {}
    audit = []
    previous = None
    for year in sorted(models):
        if not config.cost_edge_enabled or year < config.cost_selection_start_year:
            schedule[year] = None
            continue
        end = f"{year}-01-01"
        if end <= config.source_start:
            raise ValueError("cost calibration requires a prior-year replay interval")
        prefix_config = replace(config, evaluation_start=config.source_start, end_exclusive=end,
                                mr_only_from=config.source_start, f25_enabled=False, f25_coverage="all_valid",
                                warmup_allocation="cash_sweep", charts=False)
        prefix_models = {y: {p: m for p, m in values.items() if m.family == "mean_reversion"}
                         for y, values in models.items() if y < year}
        rows = []
        for threshold in config.cost_grid:
            row = {"test_year": year, "fit_start": config.source_start, "fit_end": end,
                   "threshold": threshold, "deployed": False}
            try:
                engine = Engine(data, specs, prefix_config, prefix_models, cost_bps=config.assumed_cost_bps,
                                cost_thresholds={y: threshold for y in prefix_models}).run()
                metrics = summarize(engine, config.source_start, end)
                allowed = (metrics["entry_count"] > 0 and metrics["max_drawdown_close"] >= config.cost_drawdown_floor
                           and (previous is None or not config.non_decreasing_cost_threshold or threshold >= previous))
                row.update({**metrics, "eligible": allowed, "status": "evaluated"})
            except SimulationError as error:
                if not _risk_failure(error):
                    raise
                row.update({"eligible": False, "status": "risk_failed", "error": str(error)})
            rows.append(row)
        eligible = [row for row in rows if row["eligible"]]
        if not eligible:
            raise ValueError(f"{year}: no historical cost threshold passes risk and minimum-trading checks")
        winner = max(eligible, key=lambda r: (r["annualized_return"], r["max_drawdown_close"],
                                            -r["existing_resize_count"], -r["threshold"]))
        winner["deployed"] = True
        previous = winner["threshold"]
        schedule[year] = previous
        audit.extend(rows)
        if progress:
            print(f"[cost calibration] {year}: deployed {previous:g}; cutoff {end}", flush=True)
    return schedule, audit
