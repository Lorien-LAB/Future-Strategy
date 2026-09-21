"""Compare the same real product/year/grid through separate engine checkouts."""
import argparse
import cProfile
from dataclasses import asdict, replace
import json
from pathlib import Path
import pstats
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--specs", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--product", default="RB")
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--family", default="mean_reversion")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--single", action="store_true")
    parser.add_argument("--one-stop", action="store_true")
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--products", help="explicit comma-separated product batch below --market")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.workers < 1 or (args.workers != 1 and not args.schedule):
        parser.error("positive --workers is supported only with --schedule; individual fits use one process")
    if args.profile and args.workers != 1:
        parser.error("cProfile measures this process only; profile with --workers 1")
    sys.path.insert(0, str(args.source.resolve()))
    from backtest.config import Config
    from backtest.data import MarketData, read_market, read_specs
    from backtest.training import fit_product, fit_schedule

    args.output.mkdir(parents=True, exist_ok=False)
    config = Config.load(args.config)
    if args.single:
        config = replace(config, sigma_grid=(0.5,), target_grid=(0.5,), stop_grid=(1.3,))
    elif args.one_stop:
        config = replace(config, stop_grid=(1.3,))
    if args.schedule:
        config = replace(config, source_start=f"{args.year}-01-01", evaluation_start=f"{args.year}-01-01",
                         end_exclusive=f"{args.year + 1}-01-01")
    start = time.perf_counter()
    if args.products:
        parts = [read_market(args.market / f"{name}.parquet") for name in args.products.split(",")]
        data = MarketData(bar for part in parts for bar in part.rows("0001-01-01", "9999-12-31"))
    else:
        data = read_market(args.market)
    specs = read_specs(args.specs, allow_retrospective=config.allow_retro_specs)
    load_seconds = time.perf_counter() - start
    profiler = cProfile.Profile()
    if args.profile:
        profiler.enable()
    start = time.perf_counter()
    if args.schedule:
        options = {"workers": args.workers} if args.workers != 1 else {}
        models, rows = fit_schedule(data, specs, config, progress=False, **options)
        result = {"models": {y: {p: asdict(m) for p, m in batch.items()} for y, batch in models.items()}, "audit": rows}
        audit = {"status": "schedule", "grid_attempts": sum(r.get("grid_attempts", 0) for r in rows),
                 "entry_count": sum(r.get("entry_count", 0) for r in rows)}
    else:
        model, features, audit = fit_product(data, specs, config, args.product, args.year, args.family)
        result = {"model": asdict(model) if model else None, "features": features, "audit": audit}
    elapsed = time.perf_counter() - start
    if args.profile:
        profiler.disable()
        profiler.dump_stats(str(args.output / "profile.pstats"))
        with (args.output / "profile.txt").open("w") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(45)
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("tottime").print_stats(30)
    (args.output / "result.json").write_text(json.dumps(result, sort_keys=True, allow_nan=False), encoding="utf-8")
    timing = {"product": args.product, "year": args.year, "family": args.family,
              "single": args.single, "profile": args.profile, "load_seconds": load_seconds,
              "fit_seconds": elapsed, "source": str(args.source), "audit_status": audit["status"],
              "schedule": args.schedule, "products": args.products, "workers": args.workers,
              "grid_attempts": audit.get("grid_attempts"), "entries": audit.get("entry_count")}
    (args.output / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    print(json.dumps(timing), flush=True)


if __name__ == "__main__":
    main()
