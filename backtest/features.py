"""Causal left-window features and fit-local history reuse; no outcome labels."""
from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass, field
import math
from statistics import fmean, pstdev
from .config import Config
from .contracts import PairSelector
from .data import MarketData
from .domain import Bar, Features, Model, Pair, positive


def quantile(values: list[float], q: float) -> float:
    if not values or not 0 <= q <= 1 or any(not math.isfinite(v) for v in values):
        raise ValueError("quantile requires finite observations and q in [0,1]")
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low = int(index)
    fraction = index - low
    return ordered[low] * (1 - fraction) + ordered[min(low + 1, len(ordered) - 1)] * fraction


def close_location(bar: Bar) -> float | None:
    if not all(positive(v) for v in (bar.close, bar.high, bar.low)):
        return None
    if not bar.low <= bar.close <= bar.high:
        return None
    width = bar.high - bar.low
    return 0.0 if width <= 1e-12 else (2 * bar.close - bar.high - bar.low) / width


def volatility_ratio(near: list[float], far: list[float], window: int, minimum: int) -> float | None:
    def returns(prices: list[float]) -> list[float]:
        return [math.log(b / a) for a, b in zip(prices, prices[1:]) if positive(a) and positive(b)]
    rn, rf = returns(near[-window - 1:]), returns(far[-window - 1:])
    if min(len(rn), len(rf)) < minimum:
        return None
    vn, vf = pstdev(rn), pstdev(rf)
    return vn / vf if vf > 0 else None


@dataclass
class PairHistory:
    pair: Pair | None = None
    segment_id: str = ""
    days: list[str] = field(default_factory=list)
    near: list[float] = field(default_factory=list)
    far: list[float] = field(default_factory=list)
    spread: list[float] = field(default_factory=list)
    normalized: list[float] = field(default_factory=list)

    def update(self, day: str, pair: Pair | None, bars: dict[str, Bar]) -> None:
        if pair != self.pair:
            self.pair = pair
            self.segment_id = f"{pair.key}|{day}" if pair else ""
            self.days.clear(); self.near.clear(); self.far.clear()
            self.spread.clear(); self.normalized.clear()
        if pair is None:
            return
        n, f = bars.get(pair.near), bars.get(pair.far)
        if n is None or f is None or not positive(n.close) or not positive(f.close):
            raise ValueError("selected pair must have valid close observations")
        self.days.append(day); self.near.append(n.close); self.far.append(f.close)
        spread = n.close - f.close
        self.spread.append(spread)
        self.normalized.append(200.0 * spread / (n.close + f.close))

    def snapshot(self, model: Model, bars: dict[str, Bar], config: Config) -> Features | None:
        if self.pair is None or len(self.spread) <= model.window:
            return None
        history = self.spread[-model.window - 1:-1]
        mean, std = fmean(history), pstdev(history)
        if std <= 0:
            return None
        residual = self.spread[-1] - mean
        zscore = residual / std
        direction = -1 if residual > 0 else 1 if residual < 0 else 0
        scale = 0.0
        efficiency = None
        if model.family == "mean_reversion":
            if not direction or abs(zscore) <= model.sigma:
                return None
        else:
            if len(self.normalized) <= model.breakout:
                return None
            path = self.normalized[-model.breakout - 1:]
            channel, current = path[:-1], path[-1]
            distance = sum(abs(b - a) for a, b in zip(path, path[1:]))
            scale = distance / model.breakout
            if scale <= 0:
                return None
            efficiency = abs(path[-1] - path[0]) / distance
            direction = 1 if current > max(channel) + model.buffer * scale else -1 if current < min(channel) - model.buffer * scale else 0
            if not direction or efficiency < model.efficiency or direction * (current - path[0]) <= 0:
                return None
        ncl, fcl = close_location(bars[self.pair.near]), close_location(bars[self.pair.far])
        return Features(self.days[-1], self.pair, self.segment_id, mean, std, residual, zscore,
                        self.spread[-1], self.normalized[-1],
                        volatility_ratio(self.near, self.far, config.vr_window, config.vr_minimum_returns),
                        abs(ncl - fcl) if ncl is not None and fcl is not None else None,
                        direction, scale, efficiency)


@dataclass(frozen=True, slots=True)
class _HistoryPrefix(Sequence):
    """Read-only causal view: retain a segment once, never expose its later rows."""
    values: list
    length: int

    def __len__(self):
        return self.length

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self.values[i] for i in range(*index.indices(self.length))]
        if index < 0:
            index += self.length
        if not 0 <= index < self.length:
            raise IndexError(index)
        return self.values[index]


class PreparedHistory:
    """One fit's product/calendar histories, sharing append-only segment storage.

    Selection and numerical features use the ordinary implementations. Each day's
    view is length-bounded even while its segment continues to grow. Storage is
    linear in observations, plus lazily requested (day, feature-parameter) results;
    target/stop choices share results. The owning fit releases the whole cache.
    MarketData must remain unchanged during a fit, as for its prefix hash cache.
    """
    def __init__(self, data: MarketData, config: Config, product: str) -> None:
        self.data, self.config, self.product = data, config, product
        self._histories: dict[str, PairHistory] = {}
        self._snapshots: dict[tuple, Features | None] = {}
        selector, history = PairSelector(config), PairHistory()
        for day in data.days:
            if day >= config.end_exclusive:
                break
            bars = data.by_day[day].get(product, {})
            pair = selector.update(day, product, bars)
            if pair != history.pair:
                # update() clears lists on a boundary; retain older segment buffers.
                history = PairHistory()
            history.update(day, pair, bars)
            self._histories[day] = PairHistory(history.pair, history.segment_id,
                *(_HistoryPrefix(values, len(values)) for values in
                  (history.days, history.near, history.far, history.spread, history.normalized)))

    def validate(self, data: MarketData, config: Config, product: str | None) -> None:
        if data is not self.data or config != self.config or product != self.product:
            raise ValueError("prepared history does not match data/config/product/cutoff")

    def pair(self, day: str) -> Pair | None:
        return self._histories[day].pair

    def snapshot(self, day: str, model: Model) -> Features | None:
        # All model fields consumed by PairHistory.snapshot, including MR sigma.
        key = (day, model.family, model.window, model.sigma, model.breakout,
               model.efficiency, model.buffer)
        if key not in self._snapshots:
            self._snapshots[key] = self._histories[day].snapshot(
                model, self.data.by_day[day].get(self.product, {}), self.config)
        return self._snapshots[key]
