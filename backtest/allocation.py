"""Simultaneous cash-sweep funding; no implicit reinvestment of existing PnL."""
from __future__ import annotations
from dataclasses import dataclass
import math
from .config import Config


@dataclass(frozen=True)
class FundingLeg:
    product: str
    margin_per_lot: float
    cost_per_lot: float
    quantity: float = 0.0
    reducible: bool = True


def adaptive_cap(products: int, hard: float) -> float:
    if products < 1:
        raise ValueError("positive product count required")
    return min(hard, 0.5 + 0.1 * max(0, products - 2))


def cash_sweep(equity: float, existing: list[FundingLeg], new: list[FundingLeg],
               config: Config, *, single_product_training: bool = False) -> tuple[dict, dict, dict]:
    """Return old reductions, new quantities and audit. Account values are not quantities."""
    if not new:
        return {}, {}, {"reason": "no_new_orders"}
    if len({v.product for v in existing + new}) != len(existing) + len(new):
        raise ValueError("one live position per product is required")
    if any(v.margin_per_lot <= 0 or v.cost_per_lot < 0 for v in existing + new):
        raise ValueError("invalid funding inputs")
    reserve = 0.0 if single_product_training else config.cash_buffer * equity
    old_margin = sum(v.quantity * v.margin_per_lot for v in existing)
    free = equity - reserve - old_margin
    cap = 1.0 if single_product_training else adaptive_cap(len(existing) + len(new), config.hard_max_weight)
    desired = min(equity * cap, max(equity / (len(existing) + len(new)) * config.new_target_fraction,
                                    max(0.0, free) / len(new)))
    old_floor = 0.0 if single_product_training else config.minimum_existing_weight * equity

    def floor_qty(quantity: float) -> float:
        return float(math.floor(quantity + 1e-10)) if config.integer_lots and not single_product_training else quantity

    def attempt(target: float):
        quantities = {v.product: floor_qty(target / v.margin_per_lot) for v in new}
        required = sum(quantities[v.product] * (v.margin_per_lot + v.cost_per_lot) for v in new)
        need = max(0.0, required - free)
        reductions = {}
        for leg in sorted(existing, key=lambda x: (-x.quantity * x.margin_per_lot, x.product)):
            net_release = leg.margin_per_lot - leg.cost_per_lot
            if need <= 1e-8 or not leg.reducible or net_release <= 0:
                continue
            maximum = floor_qty(max(0.0, leg.quantity - old_floor / leg.margin_per_lot))
            quantity = need / net_release
            if config.integer_lots and not single_product_training:
                quantity = math.ceil(quantity - 1e-10)
            quantity = min(maximum, quantity)
            if quantity > 0:
                reductions[leg.product] = quantity
                need -= quantity * net_release
        return need <= 1e-6, reductions, quantities, required

    feasible, reductions, quantities, required = attempt(desired)
    if not feasible:
        low, high = 0.0, desired
        for _ in range(80):
            midpoint = (low + high) / 2
            if attempt(midpoint)[0]:
                low = midpoint
            else:
                high = midpoint
        feasible, reductions, quantities, required = attempt(low)
    if not feasible:
        return {}, {}, {"reason": "insufficient_collateral_even_after_allowed_funding"}
    quantities = {key: value for key, value in quantities.items() if value > 1e-9}
    if quantities and len(quantities) < len(new):
        dropped = [v.product for v in new if v.product not in quantities]
        reductions, quantities, audit = cash_sweep(equity, existing,
            [v for v in new if v.product in quantities], config, single_product_training=single_product_training)
        audit["integer_lot_rejections"] = dropped + audit.get("integer_lot_rejections", [])
        return reductions, quantities, audit
    if not quantities:
        reductions = {}
    return reductions, quantities, {"target_per_new_before_cost": desired,
        "effective_max_weight": cap, "cash_buffer": reserve, "free_before": free,
        "new_required_with_cost": required, "old_reduction_count": len(reductions), "new_fill_count": len(quantities)}


def active_equal(equity: float, existing: list[FundingLeg], new: list[FundingLeg],
                 config: Config) -> tuple[dict, dict, dict]:
    """Explicit warm-up rebalancing, including old-position additions and their costs."""
    legs = existing + new
    old = {v.product: v.quantity for v in existing}
    available = equity * (1 - config.cash_buffer)

    def evaluate(target: float):
        quantities = {v.product: (float(math.floor(target / v.margin_per_lot + 1e-10))
                      if config.integer_lots else target / v.margin_per_lot) for v in legs}
        used = sum(quantities[v.product] * v.margin_per_lot +
                   abs(quantities[v.product] - old.get(v.product, 0)) * v.cost_per_lot for v in legs)
        return quantities, used

    low, high = 0.0, available / max(len(legs), 1)
    if evaluate(low)[1] > available:
        raise RuntimeError("not enough equity even to liquidate old positions")
    for _ in range(80):
        midpoint = (low + high) / 2
        if evaluate(midpoint)[1] <= available + 1e-8:
            low = midpoint
        else:
            high = midpoint
    quantities, used = evaluate(low)
    reductions = {v.product: v.quantity - quantities[v.product] for v in existing if v.quantity - quantities[v.product] > 1e-9}
    additions = {v.product: quantities[v.product] - old.get(v.product, 0) for v in legs if quantities[v.product] - old.get(v.product, 0) > 1e-9}
    if not any(additions.get(v.product, 0) > 0 for v in new):
        return {}, {}, {"reason": "new_batch_below_one_lot"}
    return reductions, additions, {"funding_mode": "explicit_active_equal", "target_margin": low,
                                   "budget_with_cost": used, "available_budget": available}
