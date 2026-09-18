"""One validated configuration; every research option is explicitly versioned."""
from __future__ import annotations
from dataclasses import asdict, dataclass, fields
from datetime import date
import json
import math
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Config:
    source_start: str = "2021-01-01"
    evaluation_start: str = "2023-01-01"
    end_exclusive: str = "2026-07-11"
    train_years: int = 6
    initial_equity: float = 1_000_000.0
    router: str = "training_only"
    allow_retrospective: bool = False
    mr_only_from: str = "2023-01-01"
    main_confirm: int = 2
    secondary_confirm: int = 2
    minimum_oi: float = 1000.0
    exclude_previous_main: bool = True
    delivery_guard_days: int = 15
    allow_month_series: bool = False
    settlement_fallback: bool = True
    allow_retro_specs: bool = False
    take_profit_confirm: int = 1
    stop_loss_confirm: int = 3
    cooldown_days: int = 1
    barrier_mode: str = "absolute_residual"
    state_policy: str = "shadow"
    execution_mode: str = "delayed_open"
    slippage_ticks: float = 0.0
    integer_lots: bool = True
    vr_enabled: bool = True
    vr_window: int = 10
    vr_minimum_returns: int = 2
    vr_quantile: float = 0.5
    mre_enabled: bool = True
    cost_edge_enabled: bool = True
    assumed_cost_bps: float = 2.0
    cost_grid: tuple[float, ...] = (2, 2.5, 3, 3.5, 4, 4.5, 5)
    cost_selection_start_year: int = 2023
    cost_drawdown_floor: float = -0.10
    non_decreasing_cost_threshold: bool = True
    f25_enabled: bool = True
    f25_quantile: float = 0.9
    f25_minimum_history: int = 80
    f25_coverage: str = "all_valid"
    f25_missing: str = "neutral_keep"
    f25_warmup: str = "keep"
    new_target_fraction: float = 1.0
    hard_max_weight: float = 0.8
    minimum_existing_weight: float = 0.05
    cash_buffer: float = 0.05
    warmup_allocation: str = "active_equal"
    cost_bps_values: tuple[float, ...] = (0, 1, 2)
    primary_cost_bps: float = 2.0
    training_cost_bps: float = 0.0
    sigma_grid: tuple[float, ...] = tuple(round(i / 10, 1) for i in range(1, 11))
    target_grid: tuple[float, ...] = tuple(round(i / 10, 1) for i in range(1, 11))
    stop_grid: tuple[float, ...] = (1.1, 1.2, 1.3, 1.4, 1.5)
    trend_breakout_grid: tuple[int, ...] = (10, 20, 40)
    trend_efficiency_grid: tuple[float, ...] = (0.25, 0.5)
    trend_buffer_grid: tuple[float, ...] = (0.0, 0.5)
    trend_hard_grid: tuple[float, ...] = (1.5, 2.5)
    trend_trailing_grid: tuple[float, ...] = (2.0, 3.0)
    trend_exit_grid: tuple[int, ...] = (5, 10)
    minimum_train_segments: int = 5
    minimum_train_trades: int = 3
    minimum_train_ar: float = 0.0
    minimum_worst_year: float = -0.12
    low_segment_threshold: int = 10
    minimum_effective_score: float = 8.0
    charts: bool = True

    def __post_init__(self) -> None:
        for item in fields(self):
            if isinstance(item.default, bool) and not isinstance(getattr(self, item.name), bool):
                raise ValueError(f"{item.name} must be a JSON boolean")
        if not math.isfinite(self.minimum_train_ar):
            raise ValueError("minimum_train_ar must be finite")
        for name in ("source_start", "evaluation_start", "end_exclusive", "mr_only_from"):
            value = getattr(self, name)
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError(f"{name}: expected YYYY-MM-DD")
        if not self.source_start <= self.evaluation_start < self.end_exclusive:
            raise ValueError("require source_start <= evaluation_start < end_exclusive")
        enums = {
            "router": {"training_only", "fixed_empirical_research", "mr_only"},
            "barrier_mode": {"absolute_residual", "signed_residual"},
            "state_policy": {"shadow", "actual"},
            "execution_mode": {"delayed_open", "same_open_proxy"},
            "f25_coverage": {"all_valid", "registry"},
            "f25_missing": {"neutral_keep", "reject"},
            "f25_warmup": {"keep", "reject"},
            "warmup_allocation": {"active_equal", "cash_sweep"},
        }
        for name, allowed in enums.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"{name} must be one of {sorted(allowed)}")
        if self.router == "fixed_empirical_research" and not self.allow_retrospective:
            raise ValueError("empirical 2021-2026 routing requires allow_retrospective=true")
        for name in ("train_years", "main_confirm", "secondary_confirm", "take_profit_confirm",
                     "stop_loss_confirm", "vr_window", "vr_minimum_returns", "f25_minimum_history",
                     "minimum_train_segments", "minimum_train_trades", "low_segment_threshold", "cost_selection_start_year"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.cooldown_days, int) or isinstance(self.cooldown_days, bool) or self.cooldown_days < 0:
            raise ValueError("cooldown_days must be a nonnegative integer")
        if not isinstance(self.delivery_guard_days, int) or not 0 <= self.delivery_guard_days <= 31:
            raise ValueError("delivery_guard_days must be an integer in [0,31]")
        if not 2 <= self.vr_minimum_returns <= self.vr_window:
            raise ValueError("VR requires 2 <= minimum_returns <= window")
        for name in ("initial_equity", "minimum_oi", "assumed_cost_bps", "minimum_effective_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name in ("slippage_ticks", "training_cost_bps"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative and finite")
        if not (0 < self.vr_quantile < 1 and 0 < self.f25_quantile < 1):
            raise ValueError("quantiles must be in (0,1)")
        if not (0 < self.new_target_fraction <= 1 and 0.5 <= self.hard_max_weight < 1):
            raise ValueError("invalid entry allocation limits")
        if not (0 <= self.minimum_existing_weight < 1 and 0 <= self.cash_buffer < 1):
            raise ValueError("invalid collateral reserve / old-position floor")
        if not -1 < self.cost_drawdown_floor <= 0 or not -1 < self.minimum_worst_year <= 0:
            raise ValueError("drawdown and worst-year floors use fractional negative returns")
        for name in ("cost_grid", "sigma_grid", "target_grid", "stop_grid", "cost_bps_values",
                     "trend_breakout_grid", "trend_efficiency_grid", "trend_buffer_grid",
                     "trend_hard_grid", "trend_trailing_grid", "trend_exit_grid"):
            sequence = tuple(getattr(self, name))
            if not sequence or len(sequence) != len(set(sequence)) or any(not math.isfinite(v) for v in sequence):
                raise ValueError(f"{name} must be nonempty, unique, and finite")
            object.__setattr__(self, name, tuple(sorted(sequence)))
        if any(v <= 0 for v in self.cost_grid + self.sigma_grid):
            raise ValueError("cost and sigma grids must be positive")
        if any(not 0 < v <= 1 for v in self.target_grid) or any(v <= 1 for v in self.stop_grid):
            raise ValueError("invalid target / stop grid")
        if any(v < 2 or int(v) != v for v in self.trend_breakout_grid + self.trend_exit_grid):
            raise ValueError("trend windows require integers >=2")
        if any(not 0 <= v <= 1 for v in self.trend_efficiency_grid):
            raise ValueError("trend efficiency must be in [0,1]")
        if any(v < 0 for v in self.trend_buffer_grid) or any(v <= 0 for v in self.trend_hard_grid + self.trend_trailing_grid):
            raise ValueError("invalid trend buffer / stop grids")
        if any(v < 0 for v in self.cost_bps_values) or self.primary_cost_bps not in self.cost_bps_values:
            raise ValueError("costs must be nonnegative and include primary_cost_bps")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, dict):
            raise ValueError("config must be a JSON object")
        unknown = set(values) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown configuration fields: {sorted(unknown)}")
        return cls(**values)
