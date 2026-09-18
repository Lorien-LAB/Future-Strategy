"""Immutable market/model/signal contracts. Outcomes never enter admission APIs."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import date
import hashlib
import json
import math
from typing import Any


def digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


@dataclass(frozen=True, slots=True)
class Bar:
    day: str
    product: str
    contract: str
    open: float | None
    close: float | None
    high: float | None = None
    low: float | None = None
    settlement: float | None = None
    volume: float = 0.0
    open_interest: float = 0.0
    can_buy_open: bool = True
    can_sell_open: bool = True
    identity_source: str = "actual_contract"

    def mark(self, fallback: bool) -> tuple[float | None, str]:
        if positive(self.settlement):
            return self.settlement, "settlement"
        return (self.close, "close_fallback") if fallback and positive(self.close) else (None, "missing")


@dataclass(frozen=True, slots=True)
class Pair:
    product: str
    main: str
    secondary: str

    @property
    def near(self) -> str:
        return min((self.main, self.secondary), key=lambda c: c[-6:])

    @property
    def far(self) -> str:
        return max((self.main, self.secondary), key=lambda c: c[-6:])

    @property
    def key(self) -> str:
        return f"{self.product}|{self.main}|{self.secondary}"


@dataclass(frozen=True, slots=True)
class Spec:
    product: str
    effective_from: str
    known_at: str
    multiplier: float
    margin_rate: float
    tick_size: float
    source: str

    def __post_init__(self) -> None:
        date.fromisoformat(self.effective_from)
        date.fromisoformat(self.known_at)
        if not all(positive(v) for v in (self.multiplier, self.margin_rate, self.tick_size)):
            raise ValueError("spec fields must be positive and finite")
        if self.margin_rate >= 1 or not self.source.strip():
            raise ValueError("invalid margin fraction or missing specification source")


@dataclass(frozen=True, slots=True)
class Model:
    product: str
    fit_start: str
    fit_end: str
    family: str = "mean_reversion"
    window: int = 20
    sigma: float = 0.5
    target: float = 0.5
    stop: float = 1.5
    breakout: int = 20
    efficiency: float = 0.5
    buffer: float = 0.0
    hard_stop: float = 2.0
    trailing_stop: float = 3.0
    exit_lookback: int = 10
    vr_threshold: float | None = None
    vr_training_count: int = 0
    vr_max_date: str | None = None
    selection_source: str = "training_only"
    training_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.family not in {"mean_reversion", "trend_following"}:
            raise ValueError("unsupported model family")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 2 for v in (self.window, self.breakout, self.exit_lookback)):
            raise ValueError("model windows must be integers >= 2")
        if not (0 < self.target <= 1 < self.stop) or not positive(self.sigma):
            raise ValueError("invalid MR parameters")
        if not math.isfinite(self.stop) or not (0 <= self.efficiency <= 1) or not math.isfinite(self.buffer) or self.buffer < 0:
            raise ValueError("invalid model parameters")
        if not positive(self.hard_stop) or not positive(self.trailing_stop):
            raise ValueError("invalid trend stop parameters")
        if date.fromisoformat(self.fit_start) >= date.fromisoformat(self.fit_end):
            raise ValueError("empty model training interval")
        if self.vr_threshold is not None and (not math.isfinite(self.vr_threshold) or self.vr_threshold < 0):
            raise ValueError("invalid VR threshold")
        if self.vr_max_date is not None and self.vr_max_date >= self.fit_end:
            raise ValueError("VR calibration crosses fit_end")

    @property
    def model_id(self) -> str:
        return digest(asdict(self))[:20]


@dataclass(frozen=True, slots=True)
class Features:
    day: str
    pair: Pair
    segment_id: str
    mean: float
    std: float
    residual: float
    zscore: float
    spread: float
    normalized_spread: float
    vr: float | None
    f25: float | None
    direction: int
    trend_scale: float = 0.0
    path_efficiency: float | None = None

    @property
    def signal_id(self) -> str:
        return digest({"day": self.day, "pair": asdict(self.pair), "segment": self.segment_id})[:24]


@dataclass(frozen=True, slots=True)
class Signal:
    features: Features
    model: Model
    f25_threshold: float | None
    f25_history_count: int
    f25_passed: bool
    f25_reason: str

    @property
    def signal_id(self) -> str:
        return self.features.signal_id


@dataclass(slots=True)
class LifeCycle:
    """Actual or virtual occupancy; survives model-year boundaries."""
    signal: Signal
    entered_index: int
    win_streak: int = 0
    loss_streak: int = 0
    favorable: float | None = None
    pending_exit: str | None = None
    trend_history: list[float] = field(default_factory=list)


@dataclass(slots=True)
class PendingEntry:
    signal: Signal
    created_index: int
    ready_index: int | None = None


@dataclass(slots=True)
class Position:
    signal: Signal
    quantity: float
    multiplier: float
    initial_margin: float
    entry_date: str
    near_mark: float
    far_mark: float
    margin_rate: float
    pnl: float = 0.0
    fees: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0

    @property
    def margin(self) -> float:
        return self.quantity * self.multiplier * self.margin_rate * (self.near_mark + self.far_mark)
