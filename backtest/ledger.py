"""Quantity-based futures equity ledger. Every size change has an explicit fill."""
from __future__ import annotations
import math
from .domain import Position, Signal, Spec


class Ledger:
    def __init__(self, initial_equity: float) -> None:
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.positions: dict[str, Position] = {}
        self.events: list[dict] = []
        self.fills: list[dict] = []
        self.closed: list[dict] = []
        self.total_fees = 0.0
        self.resize_count = 0

    @property
    def margin(self) -> float:
        return sum(p.margin for p in self.positions.values())

    @property
    def free_collateral(self) -> float:
        return self.equity - self.margin

    def _book(self, day: str, phase: str, kind: str, product: str,
              gross_pnl: float, fee: float = 0.0, **extra) -> None:
        if not all(math.isfinite(v) for v in (gross_pnl, fee)) or fee < 0:
            raise ValueError("invalid ledger cash flow")
        self.equity += gross_pnl - fee
        self.total_fees += fee
        self.events.append({"date": day, "phase": phase, "kind": kind, "product": product,
                            "gross_pnl": gross_pnl, "fee": fee, "net_pnl": gross_pnl - fee,
                            "equity_after": self.equity, **extra})
        if product in self.positions:
            position = self.positions[product]
            position.pnl += gross_pnl - fee
            position.fees += fee
            position.mae = min(position.mae, position.pnl)
            position.mfe = max(position.mfe, position.pnl)

    def mark(self, product: str, day: str, phase: str, near: float, far: float,
             spec: Spec, source: str = "open") -> None:
        position = self.positions[product]
        if not math.isclose(position.multiplier, spec.multiplier):
            raise ValueError("multiplier changed for an open actual contract; explicit conversion required")
        pnl = position.quantity * position.signal.features.direction * position.multiplier * (
            (near - far) - (position.near_mark - position.far_mark))
        position.near_mark, position.far_mark, position.margin_rate = near, far, spec.margin_rate
        self._book(day, phase, "mark", product, pnl, mark_source=source,
                   signal_id=position.signal.signal_id, quantity=position.quantity)

    def enter(self, signal: Signal, day: str, quantity: float, raw_near: float,
              raw_far: float, fill_near: float, fill_far: float, spec: Spec, cost_bps: float) -> None:
        product = signal.features.pair.product
        if product in self.positions or not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("entry requires a flat product and positive quantity")
        margin = quantity * spec.multiplier * spec.margin_rate * (raw_near + raw_far)
        self.positions[product] = Position(signal, quantity, spec.multiplier, margin, day,
                                            raw_near, raw_far, spec.margin_rate)
        fee = quantity * spec.multiplier * (fill_near + fill_far) * cost_bps / 10000
        slippage = quantity * spec.multiplier * signal.features.direction * (
            (raw_near - raw_far) - (fill_near - fill_far))
        self._book(day, "open", "entry", product, slippage, fee, signal_id=signal.signal_id, quantity=quantity)
        self.fills.append({"date": day, "kind": "entry", "product": product,
                           "signal_id": signal.signal_id, "quantity": quantity,
                           "near_contract": signal.features.pair.near, "far_contract": signal.features.pair.far,
                           "near_price": fill_near, "far_price": fill_far, "fee": fee,
                           "slippage_pnl": slippage, "direction": signal.features.direction})

    def increase(self, product: str, day: str, quantity: float, fill_near: float,
                 fill_far: float, cost_bps: float) -> None:
        position = self.positions[product]
        if quantity <= 0 or not math.isfinite(quantity):
            raise ValueError("invalid addition quantity")
        fee = quantity * position.multiplier * (fill_near + fill_far) * cost_bps / 10000
        slippage = quantity * position.multiplier * position.signal.features.direction * (
            (position.near_mark - position.far_mark) - (fill_near - fill_far))
        position.quantity += quantity
        self._book(day, "open", "rebalance_increase", product, slippage, fee,
                   signal_id=position.signal.signal_id, quantity=quantity)
        self.fills.append({"date": day, "kind": "rebalance_increase", "product": product,
                           "signal_id": position.signal.signal_id, "quantity": quantity,
                           "near_contract": position.signal.features.pair.near,
                           "far_contract": position.signal.features.pair.far,
                           "near_price": fill_near, "far_price": fill_far, "fee": fee,
                           "slippage_pnl": slippage, "direction": position.signal.features.direction})
        self.resize_count += 1

    def reduce(self, product: str, day: str, quantity: float, fill_near: float,
               fill_far: float, cost_bps: float, reason: str) -> None:
        position = self.positions[product]
        if quantity <= 0 or quantity > position.quantity + 1e-8:
            raise ValueError("invalid reduction quantity")
        quantity = min(quantity, position.quantity)
        fee = quantity * position.multiplier * (fill_near + fill_far) * cost_bps / 10000
        slippage = quantity * position.multiplier * position.signal.features.direction * (
            (fill_near - fill_far) - (position.near_mark - position.far_mark))
        self._book(day, "open", reason, product, slippage, fee,
                   signal_id=position.signal.signal_id, quantity=-quantity)
        self.fills.append({"date": day, "kind": reason, "product": product,
                           "signal_id": position.signal.signal_id, "quantity": -quantity,
                           "near_contract": position.signal.features.pair.near,
                           "far_contract": position.signal.features.pair.far,
                           "near_price": fill_near, "far_price": fill_far, "fee": fee,
                           "slippage_pnl": slippage, "direction": position.signal.features.direction})
        position.quantity -= quantity
        if reason == "funding_resize":
            self.resize_count += 1
        if position.quantity <= 1e-9:
            self.closed.append({"signal_id": position.signal.signal_id, "product": product,
                                "entry_date": position.entry_date, "exit_date": day,
                                "reason": reason, "net_pnl": position.pnl, "fees": position.fees,
                                "return_on_initial_margin": position.pnl / position.initial_margin,
                                "mae_currency": position.mae, "mfe_currency": position.mfe,
                                "model_id": position.signal.model.model_id})
            del self.positions[product]

    def assert_reconciled(self) -> None:
        expected = self.initial_equity + sum(event["net_pnl"] for event in self.events)
        if not math.isclose(self.equity, expected, rel_tol=1e-10, abs_tol=1e-6):
            raise RuntimeError("account equity does not reconcile with the ledger")
        if not math.isfinite(self.equity) or self.equity <= 0:
            raise RuntimeError("non-positive account equity; history retained")

    def open_positions(self) -> list[dict]:
        return [{"product": product, "quantity": p.quantity, "entry_date": p.entry_date,
                 "signal_id": p.signal.signal_id, "model_id": p.signal.model.model_id,
                 "main_contract": p.signal.features.pair.main, "secondary_contract": p.signal.features.pair.secondary,
                 "near_contract": p.signal.features.pair.near, "far_contract": p.signal.features.pair.far,
                 "near_mark": p.near_mark, "far_mark": p.far_mark, "margin": p.margin,
                 "initial_margin": p.initial_margin, "net_pnl": p.pnl, "fees": p.fees,
                 "mae_currency": p.mae, "mfe_currency": p.mfe}
                for product, p in sorted(self.positions.items())]
