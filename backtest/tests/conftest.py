from dataclasses import replace
import pandas as pd
from backtest.config import Config
from backtest.data import MarketData, SpecBook
from backtest.domain import Bar, Features, Model, Pair, Signal, Spec
from backtest.engine import Engine


def configuration(**overrides):
    base = Config(source_start="2023-02-01", evaluation_start="2023-02-01", end_exclusive="2023-03-01",
                  router="mr_only", vr_enabled=False, mre_enabled=False, cost_edge_enabled=False,
                  f25_enabled=False, integer_lots=False, cash_buffer=0.2,
                  warmup_allocation="cash_sweep", charts=False)
    return replace(base, **overrides)


def model(name="A", **overrides):
    return replace(Model(name, "2017-01-01", "2023-01-01", window=2), **overrides)


def specs():
    return SpecBook([Spec("A", "2000-01-01", "2000-01-01", 10, 0.2, 0.01, "SYNTHETIC")])


def market(spreads=(1, 2, 5, 4, 3, 4, 8, 7, 6), *, days=None, edits=None):
    days = days or [d.date().isoformat() for d in pd.bdate_range("2023-02-01", periods=len(spreads))]
    edits = edits or {}
    bars = []
    for index, (day, spread) in enumerate(zip(days, spreads, strict=True)):
        for contract, price, oi in (("A202801", 100 + spread, 3000), ("A202805", 100, 2000)):
            bar = Bar(day, "A", contract, price, price, price + 1, price - 1, price, 1000, oi)
            bars.append(replace(bar, **edits.get((index, contract), {})))
    return MarketData(bars)


def replay(data=None, cfg=None, *, models=None, cost=0, gates=True):
    data = market() if data is None else data
    cfg = configuration() if cfg is None else cfg
    schedule = models or {int(d[:4]): {"A": model()} for d in data.days}
    return Engine(data, specs(), cfg, schedule, cost_bps=cost, gates_enabled=gates).run()


def signal(day="2023-02-03", score=0.5, direction=-1):
    feature = Features(day, Pair("A", "A202801", "A202805"), "segment-a", 0, 2, 10, 5, 10, 10,
                       1.2, score, direction)
    return Signal(feature, model(vr_threshold=1), 0.9, 100, score is None or score <= 0.9, "test")
