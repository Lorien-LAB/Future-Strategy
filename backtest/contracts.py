"""Close-observable dominant pair selection, isolated from order execution."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from statistics import median
from .config import Config
from .data import MarketData
from .domain import Bar, Pair, positive


def delivery_guard(contract: str, day: str, guard_days: int) -> bool:
    observation = date.fromisoformat(day)
    maturity = contract[-6:]
    return (int(maturity[:4]) == observation.year and int(maturity[-2:]) == observation.month
            and observation.day <= guard_days)


def expired(contract: str, day: str) -> bool:
    return contract[-6:] < day[:4] + day[5:7]


@dataclass
class PairSelector:
    config: Config
    current_main: str | None = None
    previous_main: str | None = None
    current_secondary: str | None = None
    main_candidate: str | None = None
    main_count: int = 0
    secondary_candidate: str | None = None
    secondary_count: int = 0

    def update(self, day: str, product: str, bars: dict[str, Bar]) -> Pair | None:
        active = {contract: bar for contract, bar in bars.items() if positive(bar.close)
                  and bar.open_interest >= self.config.minimum_oi and bar.volume > 0
                  and not delivery_guard(contract, day, self.config.delivery_guard_days)
                  and not expired(contract, day)}
        if not active:
            self.main_candidate = self.secondary_candidate = None
            self.main_count = self.secondary_count = 0
            return None
        ranked = sorted(active, key=lambda c: (-active[c].open_interest, c))
        top = ranked[0]
        if self.current_main not in active:
            self.previous_main, self.current_main = self.current_main, top
            self.main_candidate, self.main_count = None, 0
        elif top != self.current_main:
            self.main_count = self.main_count + 1 if self.main_candidate == top else 1
            self.main_candidate = top
            if self.main_count >= self.config.main_confirm:
                self.previous_main, self.current_main = self.current_main, top
                self.main_candidate, self.main_count = None, 0
        else:
            self.main_candidate, self.main_count = None, 0
        excluded = {self.current_main}
        if self.config.exclude_previous_main:
            excluded.add(self.previous_main)
        candidate = next((c for c in ranked if c not in excluded), None)
        invalid = self.current_secondary not in active or self.current_secondary in excluded
        if invalid or candidate is None:
            self.current_secondary = candidate
            self.secondary_candidate, self.secondary_count = None, 0
        elif candidate != self.current_secondary:
            self.secondary_count = self.secondary_count + 1 if self.secondary_candidate == candidate else 1
            self.secondary_candidate = candidate
            if self.secondary_count >= self.config.secondary_confirm:
                self.current_secondary = candidate
                self.secondary_candidate, self.secondary_count = None, 0
        else:
            self.secondary_candidate, self.secondary_count = None, 0
        return Pair(product, self.current_main, self.current_secondary) if self.current_secondary else None


def training_segment_lengths(data: MarketData, product: str, start: str, end: str, config: Config) -> list[int]:
    selector = PairSelector(config)
    previous = None
    lengths: list[int] = []
    length = 0
    for day in data.days:
        if not start <= day < end:
            continue
        pair = selector.update(day, product, data.by_day[day].get(product, {}))
        if pair != previous:
            if length:
                lengths.append(length)
            length = 0
        if pair is not None:
            length += 1
        previous = pair
    if length:
        lengths.append(length)
    return lengths


def adaptive_window(lengths: list[int]) -> int:
    valid = [length for length in lengths if length > 2]
    return max(5, int(median(valid) * 0.5)) if valid else 20
