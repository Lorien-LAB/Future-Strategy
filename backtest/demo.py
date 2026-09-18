"""Deterministic synthetic integration fixture; not market data or an alpha claim."""
from __future__ import annotations
from dataclasses import asdict
import math
from pathlib import Path
import random
import pandas as pd
from .config import Config
from .reporting import write_csv, write_json


def generate(root: str | Path, *, seed: int = 17) -> Path:
    root = Path(root).resolve()
    if root.exists():
        raise FileExistsError(f"demo output already exists: {root}")
    root.mkdir(parents=True)
    rng = random.Random(seed)
    rows = []
    products = ("SIMX", "SIMY")
    for index, timestamp in enumerate(pd.bdate_range("2018-01-02", "2023-12-29")):
        day = timestamp.date().isoformat()
        for product_index, name in enumerate(products):
            active = (index // 45 + product_index) % 3
            for leg, month in enumerate((1, 5, 9)):
                base = 180 + 20 * product_index + 0.005 * index
                shock = (leg + 1) * 0.40 * math.sin(index / 5 + 0.7 * product_index) + rng.uniform(-0.06, 0.06)
                close = base + 2 * leg + shock
                open_ = close + rng.uniform(-0.04, 0.04)
                rows.append({"trading_date": day, "product": name, "contract": f"{name}2028{month:02d}",
                             "open": open_, "close": close, "high": max(open_, close) + rng.uniform(0.05, 0.2),
                             "low": min(open_, close) - rng.uniform(0.05, 0.2),
                             "settlement": close + rng.uniform(-0.015, 0.015), "volume": 5000 + 10 * leg,
                             "open_interest": 10000 if leg == active else 5000 if leg == (active + 1) % 3 else 3000})
    write_csv(root / "market" / "synthetic.csv", rows)
    write_csv(root / "specs.csv", [{"product": name, "effective_from": "2000-01-01", "known_at": "2000-01-01",
        "multiplier": 10, "margin_rate": 0.20, "tick_size": 0.01, "source": "SYNTHETIC_ONLY"} for name in products])
    config = Config(source_start="2020-01-01", evaluation_start="2022-01-01", end_exclusive="2024-01-01",
                    train_years=2, router="mr_only", mr_only_from="2020-01-01", cost_selection_start_year=2022,
                    cash_buffer=0.20, sigma_grid=(0.5, 1.0), target_grid=(0.4, 0.7), stop_grid=(1.5,),
                    cost_grid=(2.0, 3.0), minimum_train_segments=2, minimum_train_trades=2,
                    minimum_effective_score=1.0, low_segment_threshold=2, minimum_train_ar=-0.99,
                    minimum_worst_year=-0.90, cost_drawdown_floor=-0.90, f25_minimum_history=10)
    write_json(root / "config.json", asdict(config))
    write_json(root / "DATA_NOTICE.json", {"synthetic": True, "seed": seed, "rows": len(rows),
                                           "purpose": "deterministic integration tests; not profitability validation"})
    return root
