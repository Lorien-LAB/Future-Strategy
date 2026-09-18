"""One command line: validate, calibrate, run, compare, demo, reproduce."""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from .config import Config
from .data import file_hashes, read_market, read_origins, read_specs
from .demo import generate
from .pipeline import SOURCE_COMMIT, SOURCE_PATH, calibrate, compare, run, source_hash
from .reporting import write_json


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    cli = argparse.ArgumentParser(description="Daily commodity calendar-spread research; outputs are not live-trading certification.")
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("validate", "calibrate", "run"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=root / "configs" / "default.json")
        command.add_argument("--data-root", type=Path, required=True)
        command.add_argument("--specs", type=Path, required=True)
        command.add_argument("--quiet", action="store_true")
        if name != "validate":
            command.add_argument("--output", type=Path, required=True)
        if name == "run":
            command.add_argument("--models", type=Path)
            command.add_argument("--origins", type=Path)
            command.add_argument("--no-charts", action="store_true")
    demonstration = commands.add_parser("demo")
    demonstration.add_argument("--output", type=Path, required=True)
    demonstration.add_argument("--run", action="store_true")
    comparison = commands.add_parser("compare")
    comparison.add_argument("left", type=Path)
    comparison.add_argument("right", type=Path)
    archive = commands.add_parser("reproduce", help="run the pinned external legacy checkout, not the corrected engine")
    archive.add_argument("--legacy-root", type=Path, required=True, help="root of the original Future-Spread-Trader git checkout")
    archive.add_argument("--data-root", type=Path, required=True)
    archive.add_argument("--output", type=Path, required=True)
    return cli


def reproduce(args) -> dict:
    root = args.legacy_root.resolve()
    if args.output.exists():
        raise FileExistsError(args.output)
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--", SOURCE_PATH], text=True).strip()
    if revision != SOURCE_COMMIT or dirty:
        raise ValueError("legacy reproduction requires the exact pinned commit and a clean strategy directory")
    entry = root / SOURCE_PATH / "run_backtest.py"
    registry = root / SOURCE_PATH / "assets" / "f25_origin_registry.parquet"
    if not entry.is_file() or not registry.is_file():
        raise FileNotFoundError("legacy entrypoint or frozen F25 registry is absent")
    subprocess.run([sys.executable, str(entry), "--config", str(entry.parent / "config" / "default.json"),
                    "--data-root", str(args.data_root.resolve()), "--output-dir", str(args.output.resolve())],
                   cwd=entry.parent, check=True)
    return {"mode": "external_legacy_reproduction", "commit": revision, "output": str(args.output.resolve())}


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "compare":
            result = compare(args.left, args.right)
        elif args.command == "reproduce":
            result = reproduce(args)
        elif args.command == "demo":
            root = generate(args.output)
            result = {"synthetic_data": str(root)}
            if args.run:
                config = Config.load(root / "config.json")
                result = run(read_market(root / "market"), read_specs(root / "specs.csv"), config, root / "run",
                             extra_inputs=(root / "config.json", root / "specs.csv"))
        else:
            config = Config.load(args.config)
            if getattr(args, "no_charts", False):
                config = replace(config, charts=False)
            data = read_market(args.data_root, allow_month_series=config.allow_month_series)
            specs = read_specs(args.specs, allow_retrospective=config.allow_retro_specs)
            if args.command == "validate":
                checked = set()
                for day in data.days:
                    if not config.source_start <= day < config.end_exclusive:
                        continue
                    for product in data.by_day[day]:
                        specs.get(product, day)
                        checked.add(product)
                result = {"status": "INPUT_SCHEMA_VALID", "products": sorted(checked), "market_days": len(data.days),
                          "note": "Schema/spec validation does not guarantee every held-contract mark or fill is available."}
            elif args.command == "calibrate":
                if args.output.exists():
                    raise FileExistsError(args.output)
                paths = (*data.paths, args.specs, args.config)
                before, code = file_hashes(paths), source_hash()
                result = calibrate(data, specs, config, progress=not args.quiet)
                if file_hashes(paths) != before or source_hash() != code:
                    raise RuntimeError("calibration inputs changed during execution")
                write_json(args.output, result)
                result = {"models": str(args.output.resolve()), "years": sorted(result["models"])}
            else:
                bundle = json.loads(args.models.read_text(encoding="utf-8")) if args.models else None
                inputs = [args.config, args.specs] + ([args.models] if args.models else []) + ([args.origins] if args.origins else [])
                result = run(data, specs, config, args.output, bundle=bundle, origins=read_origins(args.origins),
                             extra_inputs=inputs, progress=not args.quiet)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
