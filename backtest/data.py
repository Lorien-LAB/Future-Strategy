"""Only module reading market data. Opens and later marks validate independently."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict
from datetime import date
import hashlib
import math
from pathlib import Path
import re
from typing import Iterable
import pandas as pd
from .domain import Bar, Spec, digest


def day_string(value: object) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d{8}(?:\.0)?", text):
        return pd.to_datetime(text[:8], format="%Y%m%d").date().isoformat()
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float("nan")
    if math.isfinite(number):
        if number < 1e8:
            raise ValueError(f"not a trading date: {value!r}")
        unit = "ns" if number > 1e17 else "us" if number > 1e14 else "ms" if number > 1e11 else "s"
        return pd.to_datetime(number, unit=unit, utc=True).tz_convert("Asia/Shanghai").date().isoformat()
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("missing trading date")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Shanghai")
    return timestamp.date().isoformat()


def contract_identity(raw: str, day: str, allow_month_series: bool) -> tuple[str, str, str]:
    """Return product, YYYYMM identity and lineage; never use future rollover prices."""
    raw = re.sub(r"[-_]", "", str(raw)).upper().split(".")[0]
    match = re.fullmatch(r"([A-Z]+)(\d+)", raw)
    if not match:
        raise ValueError(f"unrecognized contract: {raw!r}")
    product, digits = match.groups()
    observation = date.fromisoformat(day)
    if len(digits) == 6:
        year, month = int(digits[:4]), int(digits[-2:])
        lineage = "actual_YYYYMM"
    elif len(digits) == 4:
        year, month = 2000 + int(digits[:2]), int(digits[-2:])
        lineage = "actual_YYMM_assume_2000_century"
    elif len(digits) == 2 and allow_month_series:
        month = int(digits)
        year = observation.year + int(month < observation.month or (month == observation.month and observation.day > 15))
        lineage = "legacy_month_series_calendar_assumption"
    else:
        raise ValueError(f"{raw}: require YYYYMM/YYMM, or explicitly enable month-series adapter")
    if not 1 <= month <= 12 or not 1900 <= year <= 2200:
        raise ValueError(f"invalid contract maturity: {raw}")
    return product, f"{product}{year:04d}{month:02d}", lineage


def number(value: object, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return default
    return result if math.isfinite(result) else default


def flag(value: object, default: bool = True) -> bool:
    if value is None or pd.isna(value):
        return default
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    raise ValueError(f"invalid executable-side flag: {value!r}")


class MarketData:
    def __init__(self, bars: Iterable[Bar], *, paths: Iterable[Path] = ()) -> None:
        self.by_day: dict[str, dict[str, dict[str, Bar]]] = {}
        for bar in bars:
            date.fromisoformat(bar.day)
            mapping = self.by_day.setdefault(bar.day, {}).setdefault(bar.product, {})
            if bar.contract in mapping:
                raise ValueError(f"duplicate contract/day (intraday input is not accepted): {bar.contract} {bar.day}")
            mapping[bar.contract] = bar
        self.days = tuple(sorted(self.by_day))
        self.products = tuple(sorted({p for rows in self.by_day.values() for p in rows}))
        self.paths = tuple(Path(p).resolve() for p in paths)
        self._prefix_hashes: dict[tuple[str, str | None], str] = {}

    def get(self, day: str, product: str, contract: str) -> Bar | None:
        return self.by_day.get(day, {}).get(product, {}).get(contract)

    def rows(self, start: str, end: str, product: str | None = None) -> list[Bar]:
        return [bar for day in self.days if start <= day < end
                for name, contracts in sorted(self.by_day[day].items()) if product is None or name == product
                for _, bar in sorted(contracts.items())]

    def prefix_hash(self, end: str, product: str | None = None) -> str:
        key = (end, product)
        if key not in self._prefix_hashes:
            self._prefix_hashes[key] = digest([asdict(bar) for bar in self.rows("0001-01-01", end, product)])
        return self._prefix_hashes[key]


def read_market(root: str | Path, *, allow_month_series: bool = False) -> MarketData:
    root = Path(root).resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.suffix.lower() in {".csv", ".parquet"})
    if not paths:
        raise ValueError("no market CSV/Parquet files")
    bars: list[Bar] = []
    accepted: list[Path] = []
    for path in paths:
        stem = path.stem.split("_")[0]
        if re.fullmatch(r"[A-Za-z]+00", stem) or "jq" in stem.lower():
            continue
        frame = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
        if frame.empty:
            continue
        columns = set(frame.columns)
        date_col = next((c for c in ("trading_date", "date", "time") if c in columns), None)
        if date_col is None or "open" not in columns:
            raise ValueError(f"{path}: requires trading_date/date/time and open columns")
        if "contract" not in columns and not re.fullmatch(r"[A-Za-z]+\d{2,6}", stem):
            raise ValueError(f"{path}: add a contract column or use a contract filename")
        accepted.append(path)
        for row in frame.to_dict("records"):
            day = day_string(row[date_col])
            product, contract, lineage = contract_identity(row.get("contract", stem), day, allow_month_series)
            if str(row.get("product", product)).upper() != product:
                raise ValueError(f"{path}: product/contract mismatch")
            # Later close/settlement validity must never erase this day's valid open.
            bars.append(Bar(day, product, contract, number(row.get("open")), number(row.get("close")),
                            number(row.get("high")), number(row.get("low")),
                            number(row.get("settlement", row.get("settle"))),
                            number(row.get("volume"), 0.0) or 0.0,
                            number(row.get("open_interest", row.get("oi")), 0.0) or 0.0,
                            flag(row.get("can_buy_open")), flag(row.get("can_sell_open")), lineage))
    result = MarketData(bars, paths=accepted)
    if not result.days:
        raise ValueError("no accepted contract observations")
    return result


class SpecBook:
    def __init__(self, specs: Iterable[Spec], *, allow_retrospective: bool = False) -> None:
        self.by_product: dict[str, list[Spec]] = defaultdict(list)
        self.allow_retrospective = allow_retrospective
        for spec in specs:
            self.by_product[spec.product.upper()].append(spec)
        for product, rows in self.by_product.items():
            rows.sort(key=lambda s: (s.effective_from, s.known_at))
            if len({(s.effective_from, s.known_at) for s in rows}) != len(rows):
                raise ValueError(f"duplicate spec version: {product}")

    def get(self, product: str, day: str) -> Spec:
        eligible = [s for s in self.by_product.get(product.upper(), ())
                    if s.effective_from <= day and (self.allow_retrospective or s.known_at <= day)]
        if not eligible:
            raise ValueError(f"no point-in-time spec for {product} at {day}; no fallback multiplier")
        return max(eligible, key=lambda s: (s.effective_from, s.known_at))

    def fingerprint(self, end: str) -> str:
        return digest({p: [asdict(s) for s in rows if s.effective_from < end and
                           (self.allow_retrospective or s.known_at < end)]
                       for p, rows in sorted(self.by_product.items())})


def read_specs(path: str | Path, *, allow_retrospective: bool = False) -> SpecBook:
    frame = pd.read_csv(path, dtype={"effective_from": str, "known_at": str})
    required = {"product", "effective_from", "known_at", "multiplier", "margin_rate", "tick_size", "source"}
    if not required <= set(frame.columns) or frame[list(required)].isna().any().any():
        raise ValueError("spec CSV requires all seven complete specification fields")
    return SpecBook((Spec(str(row.product).upper(), str(row.effective_from), str(row.known_at),
                          float(row.multiplier), float(row.margin_rate), float(row.tick_size), str(row.source))
                     for row in frame.itertuples()), allow_retrospective=allow_retrospective)


def read_origins(path: str | Path | None) -> set[tuple[str, str, str, str]] | None:
    if path is None:
        return None
    path = Path(path)
    frame = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    fields = {"product", "date", "main_contract", "secondary_contract"}
    if not fields <= set(frame.columns) or frame[list(fields)].isna().any().any():
        raise ValueError("origin registry requires four complete identity columns")
    normalize = lambda value: re.sub(r"[-_]", "", str(value)).upper()
    result = {(str(row.product).upper(), day_string(row.date), normalize(row.main_contract),
               normalize(row.secondary_contract)) for row in frame.itertuples()}
    if len(result) != len(frame):
        raise ValueError("duplicate origin identities")
    return result


def file_hashes(paths: Iterable[Path]) -> dict[str, str]:
    output = {}
    for path in sorted(set(map(Path, paths))):
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        output[str(path)] = hasher.hexdigest()
    return output
