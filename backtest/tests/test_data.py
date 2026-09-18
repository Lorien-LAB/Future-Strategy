from dataclasses import replace
import pandas as pd
import pytest
from backtest.contracts import PairSelector, delivery_guard
from backtest.data import MarketData, SpecBook, contract_identity, day_string, read_market, read_specs
from backtest.domain import Spec
from conftest import configuration, market, specs


@pytest.mark.parametrize("raw", ["20230201", 20230201, 20230201.0, "2023-02-01"])
def test_date_encodings(raw):
    assert day_string(raw) == "2023-02-01"


def test_actual_contract_and_opt_in_month_series():
    assert contract_identity("a2801", "2023-02-01", False)[1] == "A202801"
    assert contract_identity("A-202801", "2023-02-01", False)[1] == "A202801"
    with pytest.raises(ValueError):
        contract_identity("a01", "2023-02-01", False)
    assert contract_identity("a01", "2023-02-01", True)[1] == "A202401"
    assert delivery_guard("A202301", "2023-01-15", 15)
    assert not delivery_guard("A202401", "2023-01-15", 15)
    with pytest.raises(ValueError):
        contract_identity("SR301", "2023-02-01", False)


def test_missing_later_mark_does_not_remove_valid_open(tmp_path):
    path = tmp_path / "market.csv"
    pd.DataFrame([{"trading_date": "2023-02-01", "contract": "A202801", "open": 101,
                   "close": None, "settlement": None}]).to_csv(path, index=False)
    data = read_market(path)
    bar = data.get("2023-02-01", "A", "A202801")
    assert bar.open == 101 and bar.close is None and bar.settlement is None


def test_duplicate_contract_days_fail(tmp_path):
    rows = [{"date": "2023-02-01", "contract": "A202801", "open": 101}] * 2
    path = tmp_path / "market.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    with pytest.raises(ValueError, match="duplicate"):
        read_market(path)


def test_pit_specs_unknown_and_future_known():
    future = Spec("A", "2020-01-01", "2026-01-01", 10, 0.2, 0.01, "LATE_REVISION")
    book = SpecBook([future])
    with pytest.raises(ValueError, match="point-in-time"):
        book.get("A", "2023-01-01")
    assert SpecBook([future], allow_retrospective=True).get("A", "2023-01-01") == future
    with pytest.raises(ValueError):
        specs().get("UNKNOWN", "2023-01-01")


def test_spec_revision_only_effective_after_announcement():
    old = specs().get("A", "2023-01-01")
    new = replace(old, effective_from="2023-02-06", known_at="2023-02-03", margin_rate=0.3)
    book = SpecBook([old, new])
    assert book.get("A", "2023-02-03").margin_rate == 0.2
    assert book.get("A", "2023-02-06").margin_rate == 0.3


def test_selector_future_suffix_invariance():
    data = market()
    first, second = PairSelector(configuration()), PairSelector(configuration())
    prefix = []
    for day in data.days[:4]:
        prefix.append(first.update(day, "A", data.by_day[day]["A"]))
    full = [second.update(day, "A", data.by_day[day]["A"]) for day in data.days]
    assert prefix == full[:4]


def test_parquet_input_roundtrip(tmp_path):
    pytest.importorskip("pyarrow")
    path = tmp_path / "market.parquet"
    pd.DataFrame([{"date": "2023-02-01", "contract": "A202801", "open": 101, "close": 102,
                   "open_interest": 3000, "volume": 200}]).to_parquet(path)
    assert read_market(path).get("2023-02-01", "A", "A202801").close == 102
