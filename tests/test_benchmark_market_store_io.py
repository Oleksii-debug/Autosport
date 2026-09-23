from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from benchmarks.benchmark_market_store_io import (
    MarketStoreIOProfile,
    _current_projection_snapshot,
    _positive_elapsed,
    _positive_int,
    _sqlite_footprint,
    run_profile,
)
from autosport.storage import SQLiteMarketStore


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000"])
def test_profile_rejects_non_positive_integer_workload_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), True])
def test_profile_rejects_invalid_elapsed_measurements(value: object) -> None:
    with pytest.raises(ValueError, match="finite positive number"):
        _positive_elapsed("elapsed", value)  # type: ignore[arg-type]


def test_sqlite_footprint_counts_database_wal_and_shm_without_invented_io_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.db"
    path.write_bytes(b"db")
    Path(f"{path}-wal").write_bytes(b"wal!")
    Path(f"{path}-shm").write_bytes(b"shm")

    files, total = _sqlite_footprint(path)

    assert files == (("profile.db", 2), ("profile.db-wal", 4), ("profile.db-shm", 3))
    assert total == 9


def test_sqlite_footprint_requires_a_real_durable_database(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not durably published"):
        _sqlite_footprint(tmp_path / "missing.db")


def test_small_profile_proves_closed_durable_counts_and_restart_projection() -> None:
    result = run_profile(count=7, batch_size=3)

    assert result.requested == 7
    assert result.received == 7
    assert result.accepted == 7
    assert result.rejected == 0
    assert result.history_events == 7
    assert result.current_quotes == 7
    assert result.durable_footprint_bytes > 0
    assert result.sqlite_file_sizes
    assert result.sqlite_file_sizes[0][0] == "market-store-io.db"
    assert all(size >= 0 for _name, size in result.sqlite_file_sizes)
    assert result.accepted_per_second > 0
    assert result.bytes_per_accepted_event > 0
    for elapsed in (
        result.ingest_elapsed_seconds,
        result.reopen_elapsed_seconds,
        result.history_read_elapsed_seconds,
        result.current_read_elapsed_seconds,
    ):
        assert math.isfinite(elapsed)
        assert elapsed > 0




def test_projection_snapshot_is_order_independent_and_content_exact() -> None:
    original = {
        ("source-b", "quote-b"): run_profile.__globals__["_build_quotes"](2)[1],
        ("source-a", "quote-a"): run_profile.__globals__["_build_quotes"](2)[0],
    }
    # The helper also proves key/event identity, so use canonical normalized
    # MarketEvent values rather than provider-side fixture DTOs.
    from autosport.providers import CanonicalNormalizer

    normalizer = CanonicalNormalizer()
    quotes = run_profile.__globals__["_build_quotes"](2)
    current = {
        ("benchmark-market-store-io", normalizer.normalize(
            "benchmark-market-store-io", quotes[0]
        ).quote_key): normalizer.normalize("benchmark-market-store-io", quotes[0]),
        ("benchmark-market-store-io", normalizer.normalize(
            "benchmark-market-store-io", quotes[1]
        ).quote_key): normalizer.normalize("benchmark-market-store-io", quotes[1]),
    }
    reverse = dict(reversed(tuple(current.items())))

    assert _current_projection_snapshot(current) == _current_projection_snapshot(reverse)

    key = next(iter(current))
    changed = dict(current)
    changed[key] = replace(
        changed[key],
        decimal_odds=changed[key].decimal_odds + Decimal("0.01"),
    )
    assert _current_projection_snapshot(changed) != _current_projection_snapshot(current)


def test_profile_rejects_same_cardinality_projection_corruption_after_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SQLiteMarketStore.current_by_source
    first_store: SQLiteMarketStore | None = None

    def corrupt_only_reopened(
        self: SQLiteMarketStore,
    ):
        nonlocal first_store
        current = original(self)
        if first_store is None:
            first_store = self
            return current
        if self is first_store or not current:
            return current

        changed = dict(current)
        key = next(iter(sorted(changed)))
        changed[key] = replace(
            changed[key],
            decimal_odds=changed[key].decimal_odds + Decimal("0.01"),
        )
        return changed

    monkeypatch.setattr(
        SQLiteMarketStore,
        "current_by_source",
        corrupt_only_reopened,
    )

    with pytest.raises(RuntimeError, match="projection content changed"):
        run_profile(count=7, batch_size=3)


@pytest.mark.parametrize(
    ("accepted", "durable_bytes", "message"),
    [
        (0, 1, "accepted must be positive"),
        (1, 0, "durable_footprint_bytes must be positive"),
    ],
)
def test_bytes_per_event_rejects_non_evidence_denominators(
    accepted: int, durable_bytes: int, message: str
) -> None:
    profile = MarketStoreIOProfile(
        requested=1,
        received=1,
        accepted=accepted,
        rejected=0,
        history_events=accepted,
        current_quotes=accepted,
        sqlite_file_sizes=(("profile.db", durable_bytes),),
        durable_footprint_bytes=durable_bytes,
        ingest_elapsed_seconds=1.0,
        reopen_elapsed_seconds=1.0,
        history_read_elapsed_seconds=1.0,
        current_read_elapsed_seconds=1.0,
    )

    with pytest.raises(ValueError, match=message):
        _ = profile.bytes_per_accepted_event
