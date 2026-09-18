"""One chronological daily engine for calibration and deployment replay.

Valid opens are never removed because later marks are missing. Unfinished
positions, orders and virtual occupancy are exported rather than discarded.
"""
from __future__ import annotations
from dataclasses import asdict
from typing import Mapping
from .allocation import FundingLeg, active_equal, cash_sweep
from .config import Config
from .contracts import PairSelector, delivery_guard, expired
from .data import MarketData, SpecBook
from .domain import LifeCycle, Model, Pair, PendingEntry, Signal, positive
from .features import PairHistory
from .gates import F25History, at_open
from .ledger import Ledger
from .strategy import observe_exit


class SimulationError(RuntimeError):
    def __init__(self, message: str, engine: "Engine") -> None:
        super().__init__(message)
        self.engine = engine


class Engine:
    def __init__(self, data: MarketData, specs: SpecBook, config: Config,
                 models: Mapping[int, Mapping[str, Model]], *, cost_bps: float,
                 cost_thresholds: Mapping[int, float | None] | None = None,
                 origins: set | None = None, training: bool = False,
                 gates_enabled: bool = True, product: str | None = None) -> None:
        self.data, self.specs, self.config = data, specs, config
        self.models, self.cost_bps = models, cost_bps
        self.thresholds = dict(cost_thresholds or {})
        self.training, self.gates_enabled = training, gates_enabled
        self.products = (product,) if product else data.products
        self.selectors = {p: PairSelector(config) for p in self.products}
        self.histories = {p: PairHistory() for p in self.products}
        self.ledger = Ledger(config.initial_equity)
        self.factor = F25History(config, origins)
        self.pending: dict[str, PendingEntry] = {}
        self.lives: dict[str, LifeCycle] = {}
        self.cooldown_until: dict[str, int] = {}
        self.daily: list[dict] = []
        self.phases: list[dict] = []
        self.orders: list[dict] = []
        self.decisions: list[dict] = []
        self.candidates: list[dict] = []
        self.allocations: list[dict] = []
        self.current_day = ""
        self.status = "RUNNING"

    def _audit_order(self, day: str, signal: Signal, action: str, reason: str) -> None:
        self.orders.append({"date": day, "signal_id": signal.signal_id,
                            "signal_date": signal.features.day, "product": signal.features.pair.product,
                            "action": action, "reason": reason})

    def _prices(self, pair: Pair, day: str, direction: int, *, closing: bool = False):
        near = self.data.get(day, pair.product, pair.near)
        far = self.data.get(day, pair.product, pair.far)
        if near is None or far is None or not positive(near.open) or not positive(far.open):
            return None
        sign = -direction if closing else direction
        allowed = (near.can_buy_open and far.can_sell_open) if sign > 0 else (near.can_sell_open and far.can_buy_open)
        if not allowed:
            return None
        spec = self.specs.get(pair.product, day)
        slip = self.config.slippage_ticks * spec.tick_size
        fill_near, fill_far = near.open + sign * slip, far.open - sign * slip
        if not positive(fill_near) or not positive(fill_far):
            raise ValueError("slippage produced a non-positive fill price")
        return near.open, far.open, fill_near, fill_far, spec

    def _observe_marks(self, day: str, phase: str) -> set[str]:
        stale = set()
        for product, position in sorted(self.ledger.positions.items()):
            pair = position.signal.features.pair
            near = self.data.get(day, product, pair.near)
            far = self.data.get(day, product, pair.far)
            if phase == "open":
                if near is None or far is None or not positive(near.open) or not positive(far.open):
                    stale.add(product)
                    continue
                near_mark, far_mark, source = near.open, far.open, "open"
            else:
                if near is None or far is None:
                    raise ValueError(f"missing held-contract closing observation: {product} {day}")
                near_mark, near_source = near.mark(self.config.settlement_fallback)
                far_mark, far_source = far.mark(self.config.settlement_fallback)
                if near_mark is None or far_mark is None:
                    raise ValueError(f"missing mark after an actual entry: {product} {day}")
                source = f"near:{near_source}|far:{far_source}"
            self.ledger.mark(product, day, phase, near_mark, far_mark, self.specs.get(product, day), source)
        return stale

    def _exits(self, day: str, index: int) -> None:
        for product, life in list(sorted(self.lives.items())):
            if not life.pending_exit:
                continue
            signal = life.signal
            if product in self.pending:
                self._audit_order(day, self.pending[product].signal, "cancel", "exit_before_delayed_entry")
                del self.pending[product]
            quote = self._prices(signal.features.pair, day, signal.features.direction, closing=True)
            if quote is None:
                self._audit_order(day, signal, "exit_pending", "both_legs_not_executable")
                continue
            if product in self.ledger.positions:
                quantity = self.ledger.positions[product].quantity
                self.ledger.reduce(product, day, quantity, quote[2], quote[3], self.cost_bps, life.pending_exit)
            self._audit_order(day, signal, "exit", life.pending_exit)
            del self.lives[product]
            self.cooldown_until[product] = index + self.config.cooldown_days

    def _pending_entries(self, day: str, index: int) -> list[tuple[Signal, tuple]]:
        executable = []
        for product, pending in list(sorted(self.pending.items())):
            if index <= pending.created_index:
                continue
            signal = pending.signal
            if not self.training and day >= self.config.mr_only_from and signal.model.family != "mean_reversion":
                self._audit_order(day, signal, "cancel", "target_period_trend_excluded")
                del self.pending[product]
                continue
            pair, direction = signal.features.pair, signal.features.direction
            guarded = any(delivery_guard(c, day, self.config.delivery_guard_days) or expired(c, day)
                          for c in (pair.near, pair.far))
            quote = None if guarded else self._prices(pair, day, direction)
            if quote is None:
                self._audit_order(day, signal, "cancel", "delivery_guard" if guarded else "entry_open_not_executable")
                del self.pending[product]
                continue
            if pending.ready_index is not None:
                if index > pending.ready_index:
                    executable.append((signal, quote))
                    del self.pending[product]
                continue
            if self.config.state_policy == "shadow":
                self.lives[product] = LifeCycle(signal, index, trend_history=[signal.features.normalized_spread])
            threshold = self.thresholds.get(int(day[:4]))
            if (self.gates_enabled and self.config.cost_edge_enabled and
                    int(day[:4]) >= self.config.cost_selection_start_year and threshold is None):
                raise ValueError(f"missing deployed cost threshold for {day[:4]}")
            passed, before_factor, decision = at_open(signal, quote[0], quote[1],
                config=self.config, cost_threshold=threshold, enabled=self.gates_enabled)
            decision["observed_date"] = day
            self.decisions.append(decision)
            if before_factor:
                self.factor.observe(signal, day)
            if not passed:
                self._audit_order(day, signal, "reject", "admission")
                del self.pending[product]
                continue
            needs_open = self.gates_enabled and (self.config.mre_enabled or self.config.cost_edge_enabled)
            if self.config.execution_mode == "delayed_open" and needs_open:
                pending.ready_index = index
                self._audit_order(day, signal, "ready", "observed_open_submit_for_next_open")
            else:
                executable.append((signal, quote))
                del self.pending[product]
        return executable

    def _allocate(self, day: str, index: int, entries: list[tuple[Signal, tuple]], stale: set[str]) -> None:
        if not entries:
            return
        if stale or self.ledger.free_collateral < -1e-6:
            for signal, _ in entries:
                self._audit_order(day, signal, "reject", "stale_marks_or_margin_shortfall")
            return
        current_quotes = {}
        existing = []
        for product, position in sorted(self.ledger.positions.items()):
            quote = self._prices(position.signal.features.pair, day, position.signal.features.direction, closing=True)
            current_quotes[product] = quote
            spec = self.specs.get(product, day)
            margin = (position.near_mark + position.far_mark) * spec.multiplier * spec.margin_rate
            cost = ((position.near_mark + position.far_mark) * spec.multiplier * self.cost_bps / 10000
                    + 2 * self.config.slippage_ticks * spec.tick_size * spec.multiplier)
            existing.append(FundingLeg(product, margin, cost, position.quantity, quote is not None))
        new = []
        for signal, quote in entries:
            spec = quote[4]
            margin = (quote[0] + quote[1]) * spec.multiplier * spec.margin_rate
            cost = ((quote[2] + quote[3]) * spec.multiplier * self.cost_bps / 10000
                    + 2 * self.config.slippage_ticks * spec.tick_size * spec.multiplier)
            new.append(FundingLeg(signal.features.pair.product, margin, cost))
        warmup_equal = (not self.training and day < self.config.evaluation_start
                        and self.config.warmup_allocation == "active_equal")
        if warmup_equal and any(not leg.reducible for leg in existing):
            for signal, _ in entries:
                self._audit_order(day, signal, "reject", "warmup_rebalance_leg_unavailable")
            return
        if warmup_equal:
            reductions, additions, audit = active_equal(self.ledger.equity, existing, new, self.config)
        else:
            reductions, additions, audit = cash_sweep(self.ledger.equity, existing, new, self.config,
                                                     single_product_training=self.training)
        # Validate requested old additions before performing any batch fills.
        for product in additions:
            if product in self.ledger.positions:
                signal = self.ledger.positions[product].signal
                if self._prices(signal.features.pair, day, signal.features.direction) is None:
                    for candidate, _ in entries:
                        self._audit_order(day, candidate, "reject", "rebalance_addition_not_executable")
                    return
        self.allocations.append({"date": day, "equity_before": self.ledger.equity,
                                 "reductions": reductions, "additions": additions, **audit})
        for product, quantity in sorted(reductions.items()):
            quote = current_quotes[product]
            self.ledger.reduce(product, day, quantity, quote[2], quote[3], self.cost_bps, "funding_resize")
            if product not in self.ledger.positions and self.config.state_policy == "actual":
                self.lives.pop(product, None)
                self.cooldown_until[product] = index + self.config.cooldown_days
        for product, quantity in sorted(additions.items()):
            if product in current_quotes:
                position = self.ledger.positions.get(product)
                if position is None:
                    raise RuntimeError("rebalance both closed and reopened an old position")
                quote = self._prices(position.signal.features.pair, day, position.signal.features.direction)
                self.ledger.increase(product, day, quantity, quote[2], quote[3], self.cost_bps)
        for signal, quote in entries:
            product = signal.features.pair.product
            quantity = additions.get(product, 0)
            if quantity <= 0:
                self._audit_order(day, signal, "reject", "insufficient_collateral_or_integer_lot")
                continue
            self.ledger.enter(signal, day, quantity, quote[0], quote[1], quote[2], quote[3], quote[4], self.cost_bps)
            if product not in self.lives:
                self.lives[product] = LifeCycle(signal, index, trend_history=[signal.features.normalized_spread])
            self._audit_order(day, signal, "fill", self.config.execution_mode)
        if self.ledger.free_collateral < -1e-5:
            raise RuntimeError("allocation violated collateral budget")

    def _close(self, day: str, index: int, *, active: bool) -> None:
        pairs = {}
        for product in self.products:
            bars = self.data.by_day[day].get(product, {})
            pair = self.selectors[product].update(day, product, bars)
            pairs[product] = pair
            self.histories[product].update(day, pair, bars)
        if not active:
            return
        for product, life in sorted(self.lives.items()):
            pair = life.signal.features.pair
            near = self.data.get(day, product, pair.near)
            far = self.data.get(day, product, pair.far)
            if pair != pairs.get(product):
                life.pending_exit = life.pending_exit or "pair_boundary"
            elif near is None or far is None or not positive(near.close) or not positive(far.close):
                life.pending_exit = life.pending_exit or "missing_signal_close"
            else:
                observe_exit(life, near.close - far.close,
                             200 * (near.close - far.close) / (near.close + far.close), self.config)
        for product, pending in list(self.pending.items()):
            if pending.ready_index is not None and pending.signal.features.pair != pairs.get(product):
                self._audit_order(day, pending.signal, "cancel", "pair_changed_before_delayed_fill")
                del self.pending[product]
        year_models = self.models.get(int(day[:4]), {})
        for product in self.products:
            if (product in self.lives or product in self.pending or product in self.ledger.positions
                    or index < self.cooldown_until.get(product, -1)):
                continue
            model = year_models.get(product)
            if model is None:
                continue
            if not self.training and model.fit_end > day:
                raise ValueError("model deployed before its training cutoff")
            if not self.training and day >= self.config.mr_only_from and model.family != "mean_reversion":
                continue
            snapshot = self.histories[product].snapshot(model, self.data.by_day[day].get(product, {}), self.config)
            if snapshot is None:
                continue
            snapshot = self.factor.feature(snapshot)
            threshold, count, passed, reason = self.factor.decision(snapshot, enabled=self.gates_enabled)
            signal = Signal(snapshot, model, threshold, count, passed, reason)
            self.pending[product] = PendingEntry(signal, index)
            self.candidates.append({"signal_id": signal.signal_id, "model_id": model.model_id,
                                    "family": model.family, **asdict(snapshot)})
            self._audit_order(day, signal, "signal", "close_observable")

    def run(self) -> "Engine":
        try:
            for index, day in enumerate(d for d in self.data.days if d < self.config.end_exclusive):
                self.current_day = day
                active = day >= self.config.source_start
                if active:
                    before_fees = self.ledger.total_fees
                    before_resize = self.ledger.resize_count
                    stale = self._observe_marks(day, "open")
                    self._exits(day, index)
                    self._allocate(day, index, self._pending_entries(day, index), stale)
                    self.phases.append({"date": day, "phase": "open", "equity": self.ledger.equity,
                                        "stale_products": sorted(stale)})
                    self._observe_marks(day, "close")
                    self.phases.append({"date": day, "phase": "close", "equity": self.ledger.equity,
                                        "stale_products": []})
                    self.ledger.assert_reconciled()
                    # Calibration is a fully-invested margin-return index, not a broker account.
                    if not self.training and self.ledger.free_collateral < -1e-5:
                        raise RuntimeError("margin shortfall: broker liquidation is not modeled; stop with state")
                    self.daily.append({"date": day, "equity": self.ledger.equity,
                                       "nav": self.ledger.equity / self.ledger.initial_equity,
                                       "margin_used": self.ledger.margin,
                                       "free_collateral": self.ledger.free_collateral,
                                       "positions": len(self.ledger.positions),
                                       "fees": self.ledger.total_fees - before_fees,
                                       "existing_resize_count": self.ledger.resize_count - before_resize})
                self._close(day, index, active=active)
            self.status = "COMPLETE_WITH_OPEN_STATE" if self.ledger.positions or self.pending else "COMPLETE"
            return self
        except Exception as error:
            self.status = "FAILED"
            raise SimulationError(f"{self.current_day}: {error}", self) from error

    def state(self) -> dict:
        return {"as_of": self.current_day, "status": self.status,
                "equity": self.ledger.equity, "open_positions": self.ledger.open_positions(),
                "pending_entries": {p: asdict(v) for p, v in sorted(self.pending.items())},
                "strategy_lifecycles": {p: asdict(v) for p, v in sorted(self.lives.items())},
                "cooldown_until_global_day_index": self.cooldown_until,
                "f25_observations": self.factor.observations}
