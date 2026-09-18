"""Output serializers and plots consume engine records; no alternative PnL path."""
from __future__ import annotations
import csv
import json
from pathlib import Path
from .metrics import summarize, yearly_returns


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path: Path, rows) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, names)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, allow_nan=False)
                             if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})


def write_engine(root: Path, engine, config) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    for name, rows in {
        "daily_portfolio": engine.daily, "phase_equity": engine.phases, "fills": engine.ledger.fills,
        "closed_trades": engine.ledger.closed, "orders": engine.orders,
        "gate_decisions": engine.decisions, "candidates": engine.candidates, "allocation_events": engine.allocations,
    }.items():
        write_csv(root / f"{name}.csv", rows)
    with (root / "ledger.jsonl").open("w", encoding="utf-8") as handle:
        for row in engine.ledger.events:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    write_json(root / "state.json", engine.state())
    write_json(root / "open_positions.json", engine.ledger.open_positions())
    metrics = summarize(engine, config.evaluation_start, config.end_exclusive)
    write_json(root / "metrics.json", metrics)
    write_json(root / "yearly_returns.json", yearly_returns(engine, config.evaluation_start, config.end_exclusive))
    return metrics


def charts(root: Path, engines: dict, config) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import date
    directory = root / "charts"
    directory.mkdir()
    outputs = []
    for field, label in (("nav", "Close NAV"), ("drawdown", "Drawdown")):
        fig, ax = plt.subplots(figsize=(10, 4.5))
        for cost, engine in sorted(engines.items()):
            baseline = summarize(engine, config.evaluation_start, config.end_exclusive)["baseline_equity"]
            rows = [r for r in engine.daily if config.evaluation_start <= r["date"] < config.end_exclusive]
            nav = [r["equity"] / baseline for r in rows]
            peak = 1.0
            values = []
            for value in nav:
                peak = max(peak, value)
                values.append(value if field == "nav" else value / peak - 1)
            ax.plot([date.fromisoformat(r["date"]) for r in rows], values, label=f"{cost:g} bp per leg/side")
        ax.set(title=label + " — research replay", ylabel=label, xlabel="Trading date")
        ax.legend(); ax.grid(alpha=0.2)
        fig.tight_layout()
        path = directory / f"{field}.png"
        fig.savefig(path, dpi=150); plt.close(fig)
        outputs.append(path.relative_to(root).as_posix())
    return outputs


def report(config, metric_rows, limitations, figures) -> str:
    lines = ["# Calendar-spread backtest — corrected research engine", "",
             "This is not an exact numerical reproduction of v1-10 Final. Synthetic tests do not validate market profitability.", "",
             f"Evaluation: `{config.evaluation_start} <= trading_date < {config.end_exclusive}`.", "",
             "|Cost bp/leg/side|CAGR|Close MDD|Open/close MDD|Sharpe 252|Entries|Open at run end|",
             "|---:|---:|---:|---:|---:|---:|---:|"]
    for row in metric_rows:
        sharpe = "N/A" if row["sharpe_252"] is None else f"{row['sharpe_252']:.4f}"
        lines.append(f"|{row['cost_bps']:g}|{row['annualized_return']:.2%}|{row['max_drawdown_close']:.2%}|"
                     f"{row['max_drawdown_open_close']:.2%}|{sharpe}|{row['entry_count']}|{row['open_positions_at_run_end']}|")
    lines.extend(["", "## Interpretation and limitations", ""] + [f"- {item}" for item in limitations])
    lines.extend(["", "## Files", "", "Each `cost_*bp/` directory contains the ledger, fills, orders, gates, daily equity, metrics, and unfinished state.",
                  "`models.json`, `training_selection.csv`, and `cost_training_selection.csv` retain calibration provenance.",
                  "The metric baseline is the last observed close before the evaluation start; the first evaluation day's PnL is included.", ""])
    lines.extend(f"![{Path(path).stem}]({path})" for path in figures)
    return "\n".join(lines) + "\n"
