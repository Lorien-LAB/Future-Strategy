"""Exact contracts for local calibration caches and append-only reconciliation."""
from dataclasses import asdict, replace
import json
import pytest
from backtest import features, training
from backtest.config import Config
from backtest.contracts import PairSelector
from backtest.data import MarketData, read_market, read_specs
from backtest.demo import generate
from backtest.domain import Bar, digest
from backtest.engine import Engine, SimulationError
from backtest.ledger import Ledger
from conftest import configuration, market, model, signal, specs


def test_immutable_identity_cache_keeps_serialization_and_replace_contract():
    original = model()
    feature = signal().features
    before = asdict(original), asdict(feature)
    assert original.model_id == digest(before[0])[:20]
    assert feature.signal_id == digest({"day": feature.day, "pair": asdict(feature.pair),
                                        "segment": feature.segment_id})[:24]
    assert original.model_id is original.model_id
    assert feature.signal_id is feature.signal_id
    assert (asdict(original), asdict(feature)) == before
    changed = replace(original, sigma=0.7)
    changed_feature = replace(feature, day="2023-02-04")
    assert changed.model_id == digest(asdict(changed))[:20] != original.model_id
    assert changed_feature.signal_id != feature.signal_id


def test_prefix_hash_matches_original_nested_serialization():
    data = market(edits={(0, "A202801"): {"open": None, "can_buy_open": False}})
    for product in (None, "A", "missing"):
        for end in (data.days[2], "2030-01-01"):
            assert data.prefix_hash(end, product) == digest([
                asdict(bar) for bar in data.rows("0001-01-01", end, product)])


def test_incremental_reconciliation_append_full_mutation_and_truncation():
    ledger = Ledger(10000)
    for i in range(1000):
        ledger._book("2023-01-01", "close", "test", "A", (-1) ** i * 0.123, 0.001)
        ledger.assert_reconciled(incremental=True)
    ledger.assert_reconciled()
    ledger.events[0]["net_pnl"] += 1
    with pytest.raises(RuntimeError, match="reconcile"):
        ledger.assert_reconciled()
    ledger.events[0]["net_pnl"] -= 1
    ledger.events.pop()
    with pytest.raises(RuntimeError, match="truncated"):
        ledger.assert_reconciled(incremental=True)


@pytest.mark.parametrize("incremental", [False, True])
def test_reconciliation_equity_failures(incremental):
    ledger = Ledger(100)
    ledger.equity += 1
    with pytest.raises(RuntimeError, match="reconcile"):
        ledger.assert_reconciled(incremental=incremental)
    ledger = Ledger(100)
    ledger._book("2023-01-01", "close", "test", "A", -100)
    with pytest.raises(RuntimeError, match="non-positive"):
        ledger.assert_reconciled(incremental=incremental)


def output(engine):
    return json.dumps({"events": engine.ledger.events, "fills": engine.ledger.fills,
        "closed": engine.ledger.closed, "daily": engine.daily, "phases": engine.phases,
        "orders": engine.orders, "decisions": engine.decisions, "candidates": engine.candidates,
        "allocations": engine.allocations, "state": engine.state()}, sort_keys=True, allow_nan=False)


@pytest.mark.parametrize("policy", ["actual", "shadow"])
@pytest.mark.parametrize("execution", ["same_open_proxy", "delayed_open"])
@pytest.mark.parametrize("family", ["mean_reversion", "trend_following"])
def test_prepared_replay_exact_outputs(policy, execution, family):
    cfg = configuration(state_policy=policy, execution_mode=execution, mre_enabled=True,
                        mr_only_from="2030-01-01", source_start="2023-02-03", evaluation_start="2023-02-03")
    data = market((1, 2, 5, 4, 3, 4, 8, 7, 6, 10, 12, 9, 7, 3))
    selected = model(family=family, breakout=2, efficiency=0, exit_lookback=2)
    schedule = {2023: {"A": selected}}
    ordinary = Engine(data, specs(), cfg, schedule, cost_bps=2, product="A").run()
    prepared = features.PreparedHistory(data, cfg, "A")
    cached = Engine(data, specs(), cfg, schedule, cost_bps=2, product="A", prepared=prepared).run()
    assert ordinary.ledger.fills
    assert output(cached) == output(ordinary)
    # A second parameter candidate must not reuse model identity or exit settings.
    schedule = {2023: {"A": replace(selected, target=0.8, hard_stop=1.7)}}
    ordinary = Engine(data, specs(), cfg, schedule, cost_bps=2, product="A").run()
    cached = Engine(data, specs(), cfg, schedule, cost_bps=2, product="A", prepared=prepared).run()
    assert output(cached) == output(ordinary)


def test_prepared_cross_year_and_failure_state():
    days = ["2022-12-27", "2022-12-28", "2022-12-29", "2022-12-30", "2023-01-03", "2023-01-04"]
    cfg = configuration(source_start=days[0], evaluation_start=days[0], end_exclusive="2023-01-05")
    schedule = {2022: {"A": model(fit_end="2022-01-01")}, 2023: {"A": model(sigma=100)}}
    for edits in ({}, {(4, "A202801"): {"close": None, "settlement": None}}):
        data = market((1, 2, 5, 4, 4, 4), days=days, edits=edits)
        engines = [Engine(data, specs(), cfg, schedule, cost_bps=0, product="A", prepared=prepared)
                   for prepared in (None, features.PreparedHistory(data, cfg, "A"))]
        for engine in engines:
            if edits:
                with pytest.raises(SimulationError, match="missing mark"):
                    engine.run()
            else:
                engine.run()
                assert engine.ledger.positions["A"].entry_date == "2022-12-30"
        assert output(engines[0]) == output(engines[1])


def test_prepared_rejects_different_inputs():
    data, cfg = market(), configuration()
    prepared = features.PreparedHistory(data, cfg, "A")
    for other_data, other_cfg, name in [(market(), cfg, "A"), (data, replace(cfg, vr_window=3), "A"),
                                       (data, cfg, "B"), (data, replace(cfg, end_exclusive="2024-01-01"), "A")]:
        with pytest.raises(ValueError, match="prepared history"):
            Engine(other_data, specs(), other_cfg, {}, cost_bps=0, product=name, prepared=prepared)


def test_prepared_causal_prefixes_ties_segments_empty_days_and_snapshot_keys():
    base = market((1, 2, 5, 4, 3, 4, 8, 7, 6, 10, 12, 9, 7, 3, 4, 8, 3, 1, 7, 9))
    rows = []
    for i, day in enumerate(base.days):
        if i == 9:  # Empty A date is still a global observation, resetting its segment.
            rows.append(Bar(day, "B", "B202801", 100, 100))
            continue
        for bar in base.by_day[day]["A"].values():
            oi = 3000 if i < 6 else (4000 if bar.contract == "A202805" else 2000)
            rows.append(replace(bar, open_interest=oi))
        rows.append(replace(rows[-1], contract="A202809", open_interest=1500))
    data, cfg = MarketData(rows), configuration()
    prepared = features.PreparedHistory(data, cfg, "A")
    history, selector = features.PairHistory(), PairSelector(cfg)
    models = [model(), model(sigma=10), model(window=3), model(family="trend_following", breakout=2,
               efficiency=0), model(family="trend_following", breakout=3, efficiency=0.9, buffer=2)]
    for day in data.days:
        bars = data.by_day[day].get("A", {})
        pair = selector.update(day, "A", bars)
        history.update(day, pair, bars)
        assert prepared.pair(day) == pair
        for selected in models:
            assert prepared.snapshot(day, selected) == history.snapshot(selected, bars, cfg)
    first = prepared._histories[data.days[0]]
    assert list(first.days) == [data.days[0]]
    assert first.days[:] == [data.days[0]]
    with pytest.raises(IndexError):
        first.days[1]
    with pytest.raises(TypeError):
        first.days[0] = "2099-01-01"
    # Missing held contracts and segment exits preserve exact partial accounting.
    engines = [Engine(data, specs(), cfg, {2023: {"A": model()}}, cost_bps=2, product="A", prepared=p)
               for p in (None, prepared)]
    errors = []
    for engine in engines:
        try:
            engine.run()
            errors.append(None)
        except SimulationError as error:
            errors.append(str(error))
    assert errors[0] == errors[1]
    assert output(engines[0]) == output(engines[1])


def test_engine_daily_append_audit_and_final_full_audit(monkeypatch):
    original = Ledger.assert_reconciled
    modes = []
    def audit(self, *, incremental=False):
        modes.append(incremental)
        return original(self, incremental=incremental)
    monkeypatch.setattr(Ledger, "assert_reconciled", audit)
    data, cfg = market(), configuration()
    Engine(data, specs(), cfg, {2023: {"A": model()}}, cost_bps=0).run()
    assert modes == [True] * len(data.days) + [False]


def test_incremental_audit_reads_only_appended_events():
    reads = []
    class CountedEvent(dict):
        def __getitem__(self, key):
            if key == "net_pnl":
                reads.append(key)
            return super().__getitem__(key)
    ledger = Ledger(1000)
    for _ in range(100):
        ledger._book("2023-01-01", "close", "test", "A", 0.25)
        ledger.events[-1] = CountedEvent(ledger.events[-1])
        ledger.assert_reconciled(incremental=True)
    assert len(reads) == 100
    ledger.assert_reconciled()
    assert len(reads) == 200


def test_engine_final_audit_catches_historical_mutation(monkeypatch):
    data, cfg = market(), configuration()
    engine = Engine(data, specs(), cfg, {2023: {"A": model()}}, cost_bps=0)
    close = engine._close
    def change_after_daily_audit(day, index, *, active):
        close(day, index, active=active)
        if day == data.days[-1]:
            engine.ledger.events[0]["net_pnl"] += 1
    monkeypatch.setattr(engine, "_close", change_after_daily_audit)
    with pytest.raises(SimulationError, match="reconcile"):
        engine.run()
    assert engine.status == "FAILED"


@pytest.mark.parametrize("family", ["mean_reversion", "trend_following"])
def test_fit_product_all_candidate_outputs_match_ordinary_engine(tmp_path, monkeypatch, family):
    root = generate(tmp_path / "synthetic")
    data, book = read_market(root / "market"), read_specs(root / "specs.csv")
    cfg = replace(Config.load(root / "config.json"), trend_breakout_grid=(10,),
                  trend_efficiency_grid=(0.25,), trend_buffer_grid=(0.0,),
                  trend_hard_grid=(1.5,), trend_trailing_grid=(2.0,), trend_exit_grid=(5, 10))
    cached_outputs, original_outputs = [], []
    class CapturedEngine(Engine):
        def run(self):
            result = super().run()
            cached_outputs.append(output(result))
            return result
    monkeypatch.setattr(training, "Engine", CapturedEngine)
    cached = training.fit_product(data, book, cfg, "SIMX", 2023, family)
    class OrdinaryEngine(Engine):
        def __init__(self, *args, **kwargs):
            kwargs.pop("prepared", None)
            super().__init__(*args, **kwargs)
        def run(self):
            result = super().run()
            original_outputs.append(output(result))
            return result
    monkeypatch.setattr(training, "Engine", OrdinaryEngine)
    ordinary = training.fit_product(data, book, cfg, "SIMX", 2023, family)
    assert cached == ordinary
    assert cached_outputs == original_outputs
    assert cached_outputs and any(json.loads(row)["fills"] for row in cached_outputs)
