"""Performance derives from the same ledger, using observed market dates."""
from __future__ import annotations
from datetime import date
import math
from statistics import fmean, stdev


def max_drawdown(values: list[float]) -> float:
    peak, worst = 1.0, 0.0
    for value in values:
        if value <= 0 or not math.isfinite(value):
            raise ValueError("NAV must be positive and finite")
        peak = max(peak, value)
        worst = min(worst, value / peak - 1)
    return worst


def summarize(engine, start: str, end: str) -> dict:
    before = [row for row in engine.daily if row["date"] < start]
    baseline = before[-1]["equity"] if before else engine.ledger.initial_equity
    rows = [row for row in engine.daily if start <= row["date"] < end]
    nav = [row["equity"] / baseline for row in rows]
    returns = [b / a - 1 for a, b in zip([1.0] + nav, nav)]
    terminal = nav[-1] if nav else 1.0
    elapsed = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if elapsed <= 0:
        raise ValueError("empty metric interval")
    sd = stdev(returns) if len(returns) >= 2 else 0.0
    sharpe = math.sqrt(252) * fmean(returns) / sd if sd > 0 else None
    phase_nav = [row["equity"] / baseline for row in engine.phases
                 if start <= row["date"] < end and not row.get("stale_products")]
    fills = [row for row in engine.ledger.fills if start <= row["date"] < end]
    return {"start": start, "end_exclusive": end, "baseline_equity": baseline,
            "baseline": "last_observed_close_before_start_or_initial_equity",
            "terminal_nav": terminal, "total_return": terminal - 1,
            "annualized_return": terminal ** (365.2425 / elapsed) - 1,
            "max_drawdown_close": max_drawdown([1.0] + nav),
            "max_drawdown_open_close": max_drawdown([1.0] + phase_nav),
            "sharpe_252": sharpe, "market_days": len(rows),
            "entry_count": sum(row["kind"] == "entry" for row in fills),
            "existing_resize_count": sum(row["kind"] in {"funding_resize", "rebalance_increase"} for row in fills),
            "fees_currency": sum(row["fee"] for row in fills),
            "slippage_loss_currency": -sum(row.get("slippage_pnl", 0) for row in fills),
            "open_positions_at_run_end": len(engine.ledger.positions)}


def yearly_returns(engine, start: str, end: str) -> dict[str, float]:
    result = {}
    for year in range(int(start[:4]), int(end[:4]) + 1):
        left, right = max(start, f"{year}-01-01"), min(end, f"{year + 1}-01-01")
        if left < right:
            result[str(year)] = summarize(engine, left, right)["total_return"]
    return result
